package trust

import (
	"errors"
	"strings"
)

type Grant struct {
	ActivityID string   `json:"activity_id"`
	Actions    []string `json:"actions"`
}

type ClientContext struct {
	Subject           string `json:"subject"`
	Session           string `json:"session"`
	ContextID         string `json:"context_id"`
	ContextGeneration int64  `json:"context_generation"`
	AuthzRevision     int64  `json:"authz_revision"`
}

type Claims struct {
	Issuer string `json:"issuer"`
	KeyID  string `json:"key_id"`
	ClientContext
	NotBefore     int64    `json:"not_before_unix_ms"`
	Expires       int64    `json:"expires_unix_ms"`
	Grants        []Grant  `json:"grants"`
	GlobalActions []string `json:"global_actions"`
}

type Verifier struct {
	Keys   Keys
	Issuer string
}

// Verify uses the uncertainty interval of verified UTC, never local wall time.
func (v *Verifier) Verify(authorization string, stamp Stamp) (Claims, error) {
	var claims Claims
	if v == nil {
		return claims, errors.New("authorization verifier is not configured")
	}
	if !strings.HasPrefix(authorization, "Bearer ") {
		return claims, errors.New("signed backend identity required")
	}
	if err := verify(strings.TrimPrefix(authorization, "Bearer "), v.Issuer, v.Keys, &claims); err != nil {
		return claims, err
	}
	if claims.Subject == "" || claims.Session == "" || claims.ContextID == "" || claims.ContextGeneration < 1 || claims.AuthzRevision < 1 || claims.NotBefore < 1 || claims.Expires <= claims.NotBefore || claims.Expires-claims.NotBefore > 86400000 {
		return Claims{}, errors.New("invalid identity context or validity interval")
	}
	if !stamp.Verified || stamp.Now-stamp.Uncertainty < claims.NotBefore || stamp.Now+stamp.Uncertainty >= claims.Expires {
		return Claims{}, errors.New("identity context is not currently valid")
	}
	return claims, nil
}

func (c Claims) Allows(activity, action string) bool {
	for _, grant := range c.Grants {
		if grant.ActivityID != activity {
			continue
		}
		for _, allowed := range grant.Actions {
			if allowed == action {
				return true
			}
		}
	}
	return false
}

func (c Claims) AllowsGlobal(action string) bool {
	for _, allowed := range c.GlobalActions {
		if allowed == action {
			return true
		}
	}
	return false
}
