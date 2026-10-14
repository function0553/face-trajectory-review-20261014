package trust

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func signed(t *testing.T, value any, private ed25519.PrivateKey) string {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return base64.RawURLEncoding.EncodeToString(data) + "." + base64.RawURLEncoding.EncodeToString(ed25519.Sign(private, data))
}

func testClock(t *testing.T) (*Clock, ed25519.PrivateKey, *time.Time) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	c, err := NewClock(Keys{"test": public}, "utc-test", filepath.Join(t.TempDir(), "proof.txt"))
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	c.now = func() time.Time { return now }
	c.challengeAt = now
	return c, private, &now
}

func putProof(t *testing.T, c *Clock, key ed25519.PrivateKey, value ClockProof) {
	t.Helper()
	if err := os.WriteFile(c.path, []byte(signed(t, value, key)), 0600); err != nil {
		t.Fatal(err)
	}
}
func proof(c *Clock) ClockProof {
	return ClockProof{Issuer: "utc-test", KeyID: "test", Challenge: c.challenge, Now: 1791626400000, Uncertainty: 5, ValidFor: 120000, SourceVerified: true, Source: "SYNTHETIC_TEST", EvidenceID: "test-only"}
}

func TestClockChallengeLeaseAndMonotonicUTC(t *testing.T) {
	c, key, now := testClock(t)
	if _, err := c.Now(); err == nil {
		t.Fatal("missing proof trusted")
	}
	oldChallenge := c.challenge
	input := proof(c)
	putProof(t, c, key, input)
	stamp, err := c.Now()
	if err != nil {
		t.Fatal(err)
	}
	if stamp.Now != input.Now || !stamp.Verified || c.challenge == oldChallenge {
		t.Fatal("accepted proof must rotate challenge")
	}
	*now = now.Add(10 * time.Second)
	stamp, err = c.Now()
	if err != nil || stamp.Now != input.Now+10000 {
		t.Fatalf("monotonic progression: %+v %v", stamp, err)
	}
	*now = now.Add(110 * time.Second)
	if _, err = c.Now(); err == nil {
		t.Fatal("expired lease trusted")
	}
	// A stale proof cannot renew on the next challenge or after process restart.
	input.EvidenceID = "replayed"
	putProof(t, c, key, input)
	if _, err = c.Now(); err == nil {
		t.Fatal("replay trusted")
	}
	next, err := NewClock(c.keys, c.issuer, c.path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = next.Now(); err == nil {
		t.Fatal("old startup proof trusted")
	}
}

func TestClockRejectsFalseSourceExcessUncertaintyAndRegression(t *testing.T) {
	for _, name := range []string{"source", "uncertainty", "duration", "challenge", "signature", "regression"} {
		t.Run(name, func(t *testing.T) {
			c, key, now := testClock(t)
			input := proof(c)
			if name == "regression" {
				putProof(t, c, key, input)
				if _, err := c.Now(); err != nil {
					t.Fatal(err)
				}
				*now = now.Add(time.Millisecond)
				input = proof(c)
				input.Now--
			}
			switch name {
			case "source":
				input.SourceVerified = false
			case "uncertainty":
				input.Uncertainty = 1001
			case "duration":
				input.ValidFor = 120001
			case "challenge":
				input.Challenge = "old"
			case "signature":
				_, key, _ = ed25519.GenerateKey(rand.Reader)
			}
			putProof(t, c, key, input)
			if _, err := c.Now(); err == nil {
				t.Fatal("invalid clock proof accepted")
			}
		})
	}
}

func TestChallengeRoundTripErrorIncluded(t *testing.T) {
	c, key, now := testClock(t)
	input := proof(c)
	*now = now.Add(996 * time.Millisecond)
	putProof(t, c, key, input)
	if _, err := c.Now(); err == nil {
		t.Fatal("round trip made UTC uncertainty exceed policy")
	}
}

func TestSignedAuthorizationIntervalAndScope(t *testing.T) {
	public, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	v := Verifier{Keys: Keys{"test": public}, Issuer: "backend-test"}
	stamp := Stamp{Verified: true, Now: 1791626400000, Uncertainty: 10}
	claims := Claims{Issuer: v.Issuer, KeyID: "test", ClientContext: ClientContext{Subject: "alice", Session: "login-1", ContextID: "selection-1", ContextGeneration: 1, AuthzRevision: 1}, NotBefore: stamp.Now - 100, Expires: stamp.Now + 10000, Grants: []Grant{{ActivityID: "A", Actions: []string{"query", "subscribe"}}}}
	got, err := v.Verify("Bearer "+signed(t, claims, key), stamp)
	if err != nil {
		t.Fatal(err)
	}
	if !got.Allows("A", "query") || got.Allows("B", "query") || got.Allows("A", "monitor") {
		t.Fatal("grant scope escaped")
	}
	for _, change := range []func(*Claims){func(c *Claims) { c.Expires = stamp.Now + stamp.Uncertainty }, func(c *Claims) { c.NotBefore = stamp.Now }, func(c *Claims) { c.Issuer = "other" }, func(c *Claims) { c.ContextGeneration = 0 }} {
		bad := claims
		change(&bad)
		if _, err := v.Verify("Bearer "+signed(t, bad, key), stamp); err == nil {
			t.Fatal("invalid claims accepted")
		}
	}
	if _, err := v.Verify("Bearer "+signed(t, claims, key), Stamp{Now: stamp.Now}); err == nil {
		t.Fatal("unverified wall UTC accepted")
	}
}

type captureWorker struct{ calls []map[string]json.RawMessage }

func (w *captureWorker) Request(_ context.Context, value json.RawMessage) (json.RawMessage, error) {
	var body map[string]json.RawMessage
	_ = json.Unmarshal(value, &body)
	w.calls = append(w.calls, body)
	return json.RawMessage(`{"ok":true}`), nil
}

func TestClockedOverwritesClientClockAndBoundsQueuedAuthorization(t *testing.T) {
	c, key, _ := testClock(t)
	putProof(t, c, key, proof(c))
	inner := &captureWorker{}
	adapter := Clocked{Worker: inner, Clock: c}
	_, err := adapter.Request(context.Background(), json.RawMessage(`{"op":"activity_get","_trusted_clock":{"verified":true,"now_unix_ms":1}}`))
	if err != nil {
		t.Fatal(err)
	}
	var stamp Stamp
	if json.Unmarshal(inner.calls[0]["_trusted_clock"], &stamp) != nil || stamp.Now != 1791626400000 {
		t.Fatal("client clock was trusted")
	}
	_, err = adapter.Request(context.Background(), json.RawMessage(`{"op":"activity_get","_authorization_validity":{"not_before_unix_ms":1,"expires_unix_ms":1791626400000}}`))
	if err == nil || len(inner.calls) != 1 {
		t.Fatal("expired queued authorization dispatched")
	}
}
