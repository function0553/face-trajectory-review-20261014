// Package trust verifies backend authorization and challenge-bound UTC proofs.
// Only Ed25519 public keys are loaded by this process.
package trust

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"strings"
)

type Keys map[string]ed25519.PublicKey

func LoadKeys(path string) (Keys, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, errors.New("cannot read verifier public key file")
	}
	var document struct {
		Keys map[string]string `json:"keys"`
	}
	if json.Unmarshal(data, &document) != nil || len(document.Keys) == 0 {
		return nil, errors.New("public key file requires a nonempty keys object")
	}
	keys := make(Keys)
	for id, encoded := range document.Keys {
		decoded, err := base64.StdEncoding.DecodeString(encoded)
		if err != nil {
			decoded, err = base64.RawURLEncoding.DecodeString(encoded)
		}
		if id == "" || err != nil || len(decoded) != ed25519.PublicKeySize {
			return nil, errors.New("invalid Ed25519 public key")
		}
		keys[id] = ed25519.PublicKey(decoded)
	}
	return keys, nil
}

func verify(token, issuer string, keys Keys, destination any) error {
	if issuer == "" || len(keys) == 0 {
		return errors.New("verifier is not configured")
	}
	if len(token) > 65536 {
		return errors.New("signed token is too large")
	}
	parts := strings.Split(token, ".")
	if len(parts) != 2 {
		return errors.New("invalid signed token")
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return errors.New("invalid signed token")
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return errors.New("invalid signed token")
	}
	var header struct {
		Issuer string `json:"issuer"`
		KeyID  string `json:"key_id"`
	}
	if json.Unmarshal(payload, &header) != nil || header.Issuer != issuer {
		return errors.New("untrusted issuer")
	}
	key := keys[header.KeyID]
	if len(key) != ed25519.PublicKeySize || !ed25519.Verify(key, payload, signature) {
		return errors.New("invalid signature")
	}
	if json.Unmarshal(payload, destination) != nil {
		return errors.New("invalid signed claims")
	}
	return nil
}
