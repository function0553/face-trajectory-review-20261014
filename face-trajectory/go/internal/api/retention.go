package api

import (
	"encoding/json"
	"face-trajectory/internal/trust"
	"net/http"
)

func (s *Server) retentionAuthorized(w http.ResponseWriter, r *http.Request) (trust.Claims, bool) {
	claims, _, ok := s.authorize(w, r)
	if !ok {
		return claims, false
	}
	if !claims.AllowsGlobal("retention") {
		writeError(w, http.StatusForbidden, "backend identity does not authorize retention administration", &claims)
		return claims, false
	}
	if s.retention == nil {
		writeError(w, http.StatusServiceUnavailable, "retention manager is not configured", &claims)
		return claims, false
	}
	return claims, true
}

func (s *Server) retentionRun(w http.ResponseWriter, r *http.Request) {
	body, err := readBody(w, r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error(), nil)
		return
	}
	for field := range body {
		if field != "dry_run" {
			writeError(w, http.StatusBadRequest, "retention accepts only dry_run; database paths are startup configuration", nil)
			return
		}
	}
	var selected *bool
	if json.Unmarshal(body["dry_run"], &selected) != nil || selected == nil {
		writeError(w, http.StatusBadRequest, "explicit dry_run boolean is required", nil)
		return
	}
	dryRun := *selected
	claims, ok := s.retentionAuthorized(w, r)
	if !ok {
		return
	}
	if !dryRun && !s.retention.Enabled() {
		writeError(w, http.StatusConflict, "destructive retention requires the explicit retention-enabled startup flag", &claims)
		return
	}
	result, err := s.retention.Run(r.Context(), dryRun, &claims)
	var reply map[string]json.RawMessage
	if len(result) > 0 && json.Unmarshal(result, &reply) == nil {
		reply["client_context"], _ = json.Marshal(claims.ClientContext)
		status := http.StatusOK
		if err != nil {
			status = http.StatusBadRequest
		}
		writeReply(w, status, reply)
		return
	}
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "retention worker or verified UTC is unavailable; inspect retention status", &claims)
		return
	}
	writeError(w, http.StatusBadGateway, "invalid retention response", &claims)
}

func (s *Server) retentionStatus(w http.ResponseWriter, r *http.Request) {
	claims, ok := s.retentionAuthorized(w, r)
	if !ok {
		return
	}
	reply := map[string]json.RawMessage{"ok": json.RawMessage("true")}
	reply["retention"], _ = json.Marshal(s.retention.Status())
	reply["client_context"], _ = json.Marshal(claims.ClientContext)
	writeReply(w, http.StatusOK, reply)
}
