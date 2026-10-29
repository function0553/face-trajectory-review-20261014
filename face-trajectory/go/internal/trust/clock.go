package trust

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"sync"
	"time"
)

const ChallengeLifetime = 30 * time.Second
const MaxProofLifetime = 120 * time.Second
const MaxUTCUncertaintyMS int64 = 1000

type Stamp struct {
	Verified    bool   `json:"verified"`
	Now         int64  `json:"now_unix_ms"`
	Uncertainty int64  `json:"uncertainty_ms"`
	ValidUntil  int64  `json:"valid_until_unix_ms"`
	EvidenceID  string `json:"evidence_id"`
}

type ClockProof struct {
	Issuer         string `json:"issuer"`
	KeyID          string `json:"key_id"`
	Challenge      string `json:"challenge"`
	Now            int64  `json:"now_unix_ms"`
	Uncertainty    int64  `json:"uncertainty_ms"`
	ValidFor       int64  `json:"valid_for_ms"`
	SourceVerified bool   `json:"source_verified"`
	Source         string `json:"source"`
	EvidenceID     string `json:"evidence_id"`
}

type Clock struct {
	mu           sync.Mutex
	keys         Keys
	issuer, path string
	challenge    string
	challengeAt  time.Time
	anchor       time.Time
	proof        ClockProof
	accepted     [32]byte
	now          func() time.Time // monotonic process time; injectable only inside unit tests
}

func NewClock(keys Keys, issuer, path string) (*Clock, error) {
	c := &Clock{keys: keys, issuer: issuer, path: path, now: time.Now}
	if err := c.rotate(c.now()); err != nil {
		return nil, err
	}
	return c, nil
}

func (c *Clock) rotate(now time.Time) error {
	buffer := make([]byte, 32)
	if _, err := rand.Read(buffer); err != nil {
		return errors.New("cannot create clock challenge")
	}
	c.challenge, c.challengeAt = hex.EncodeToString(buffer), now
	return nil
}

func (c *Clock) Challenge() map[string]any {
	if c == nil {
		return map[string]any{"trusted": false, "error": "trusted clock is not configured"}
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	now := c.now()
	if now.Sub(c.challengeAt) >= ChallengeLifetime {
		_ = c.rotate(now)
	}
	result := map[string]any{"challenge": c.challenge, "challenge_valid_for_ms": (ChallengeLifetime - now.Sub(c.challengeAt)).Milliseconds(), "max_proof_valid_for_ms": MaxProofLifetime.Milliseconds(), "trusted": false}
	if stamp, err := c.refresh(now); err == nil {
		result["trusted"] = true
		result["stamp"] = stamp
		result["challenge"] = c.challenge
		result["challenge_valid_for_ms"] = (ChallengeLifetime - now.Sub(c.challengeAt)).Milliseconds()
	}
	return result
}

func (c *Clock) Now() (Stamp, error) {
	if c == nil {
		return Stamp{}, errors.New("trusted clock is not configured")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.refresh(c.now())
}

func (c *Clock) refresh(now time.Time) (Stamp, error) {
	if len(c.keys) == 0 || c.issuer == "" || c.path == "" {
		return Stamp{}, errors.New("trusted UTC verifier is not configured")
	}
	data, err := os.ReadFile(c.path)
	if err != nil {
		return Stamp{}, errors.New("trusted UTC proof is unavailable")
	}
	data = []byte(strings.TrimSpace(string(data)))
	hash := sha256.Sum256(data)
	if hash != c.accepted || c.anchor.IsZero() {
		var proof ClockProof
		if err := verify(string(data), c.issuer, c.keys, &proof); err != nil {
			return Stamp{}, errors.New("trusted UTC proof signature rejected")
		}
		age := now.Sub(c.challengeAt)
		if proof.Challenge != c.challenge || age < 0 || age >= ChallengeLifetime {
			return Stamp{}, errors.New("trusted UTC proof challenge is stale or mismatched")
		}
		if !proof.SourceVerified || proof.Source == "" || proof.EvidenceID == "" || proof.Now < 604800000 || proof.Uncertainty < 0 || proof.Uncertainty > MaxUTCUncertaintyMS || proof.ValidFor < 1 || proof.ValidFor > MaxProofLifetime.Milliseconds() || proof.Now > 9223372036854775807-proof.ValidFor-MaxUTCUncertaintyMS {
			return Stamp{}, errors.New("trusted UTC proof source or duration rejected")
		}
		// Signed UTC refers to a point during the challenge round trip. Advancing
		// from receipt has uncertainty covering the entire challenge age.
		proof.Uncertainty += age.Milliseconds()
		if proof.Uncertainty > MaxUTCUncertaintyMS {
			return Stamp{}, errors.New("trusted UTC proof uncertainty exceeds policy")
		}
		anchor := now
		if !c.anchor.IsZero() {
			previous := c.proof.Now + now.Sub(c.anchor).Milliseconds()
			if proof.Now < previous {
				lag := previous - proof.Now
				// A renewal was signed before receipt. Its uncertainty already
				// includes the challenge round trip; a smaller delivery delay on
				// the previous proof must not make every later renewal regress.
				// Reconcile only overlapping evidence within that bounded budget.
				if lag > proof.Uncertainty || lag > MaxUTCUncertaintyMS-proof.Uncertainty {
					return Stamp{}, errors.New("trusted UTC watermark cannot move backwards")
				}
				if lag >= proof.ValidFor {
					return Stamp{}, errors.New("trusted UTC proof expired")
				}
				proof.Uncertainty += lag
				// Keep signed Now and ValidFor unchanged: backdating the monotonic
				// anchor preserves the watermark without extending signed expiry.
				anchor = now.Add(-time.Duration(lag) * time.Millisecond)
			}
		}
		c.proof, c.anchor, c.accepted = proof, anchor, hash
		if err := c.rotate(now); err != nil {
			return Stamp{}, err
		}
	}
	elapsed := now.Sub(c.anchor).Milliseconds()
	if elapsed < 0 || elapsed >= c.proof.ValidFor {
		return Stamp{}, errors.New("trusted UTC proof expired")
	}
	return Stamp{Verified: true, Now: c.proof.Now + elapsed, Uncertainty: c.proof.Uncertainty, ValidUntil: c.proof.Now + c.proof.ValidFor, EvidenceID: c.proof.EvidenceID}, nil
}

type Exchanger interface {
	Request(context.Context, json.RawMessage) (json.RawMessage, error)
}
type Clocked struct {
	Worker Exchanger
	Clock  *Clock
}

// Clocked is the exclusive service-to-worker pipe. HTTP input cannot inject
// either trust field; the API removes them before reaching this adapter.
func (c *Clocked) Request(ctx context.Context, request json.RawMessage) (json.RawMessage, error) {
	var body map[string]json.RawMessage
	if json.Unmarshal(request, &body) != nil {
		return nil, errors.New("invalid worker request")
	}
	var op string
	_ = json.Unmarshal(body["op"], &op)
	delete(body, "_trusted_clock")
	if op != "health" {
		stamp, err := c.Clock.Now()
		if err != nil {
			return nil, err
		}
		if validity, ok := body["_authorization_validity"]; ok {
			var interval struct {
				NotBefore int64 `json:"not_before_unix_ms"`
				Expires   int64 `json:"expires_unix_ms"`
			}
			if json.Unmarshal(validity, &interval) != nil || stamp.Now-stamp.Uncertainty < interval.NotBefore || stamp.Now+stamp.Uncertainty >= interval.Expires {
				return nil, errors.New("authorization expired before worker dispatch")
			}
		}
		delete(body, "_authorization_validity")
		body["_trusted_clock"], _ = json.Marshal(stamp)
	}
	encoded, _ := json.Marshal(body)
	return c.Worker.Request(ctx, encoded)
}
