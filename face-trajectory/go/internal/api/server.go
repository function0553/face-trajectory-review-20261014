package api

import (
	"context"
	"encoding/json"
	"errors"
	"face-trajectory/internal/retention"
	"face-trajectory/internal/trust"
	"face-trajectory/internal/worker"
	"io"
	"net/http"
	"strings"
	"time"
)

type Exchanger interface {
	Request(context.Context, json.RawMessage) (json.RawMessage, error)
}

const maxBody = 4*1024*1024 - 1024

type Server struct {
	exchanger     Exchanger
	verifier      *trust.Verifier
	clock         *trust.Clock
	subscriptions *subscriptions
	retention     *retention.Manager
}

// New preserves source compatibility and fails closed. The daemon supplies
// backend verification through NewAuthorized.
func New(exchanger Exchanger) http.Handler { return NewAuthorized(exchanger, nil, nil) }

func NewAuthorized(exchanger Exchanger, verifier *trust.Verifier, clock *trust.Clock, managers ...*retention.Manager) http.Handler {
	s := &Server{exchanger: exchanger, verifier: verifier, clock: clock, subscriptions: newSubscriptions()}
	if len(managers) > 0 {
		s.retention = managers[0]
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", s.health)
	mux.HandleFunc("GET /v1/push/status", s.get("push_status", "push_status", true))
	mux.HandleFunc("POST /v1/retention/run", s.retentionRun)
	mux.HandleFunc("GET /v1/retention/status", s.retentionStatus)
	mux.HandleFunc("POST /v1/activities/sync", s.post("activity_sync", "config", false))
	mux.HandleFunc("POST /v1/activities/bindings", s.post("activity_bindings", "config", false))
	mux.HandleFunc("POST /v1/activities/monitoring", s.post("activity_monitoring", "monitor", false))
	mux.HandleFunc("POST /v1/activities/get", s.post("activity_get", "query", false))
	mux.HandleFunc("POST /v1/subscriptions/open", s.subscription("open"))
	mux.HandleFunc("POST /v1/subscriptions/poll", s.subscription("poll"))
	mux.HandleFunc("POST /v1/subscriptions/close", s.subscription("close"))
	for _, route := range []struct {
		path, op, action string
		global           bool
	}{
		{"/v1/templates", "add_template", "identity", false},
		{"/v1/search", "search", "identity", false},
		{"/v1/associations/observe", "association_observe", "ingest", false},
		{"/v1/associations/correct", "association_correct", "identity", false},
		{"/v1/associations/rebuild", "association_rebuild", "identity", false},
		{"/v1/monitoring/persons", "monitoring_person_put", "monitor", false},
		{"/v1/monitoring/observe", "monitoring_observe", "ingest", false},
		{"/v1/monitoring/gate-event", "monitoring_gate_event", "ingest", false},
		{"/v1/monitoring/get", "monitoring_get", "query", false},
		{"/v1/monitoring/timeline/query", "monitoring_timeline_query", "query", false},
		{"/v1/visits/build", "build_visits", "ingest", false},
		{"/v1/trajectories/raw", "raw_trajectory", "ingest", false},
		{"/v1/trajectories/rebuild", "trajectory_rebuild", "ingest", false},
		{"/v1/trajectories/get", "trajectory_get", "query", false},
		{"/v1/timeline/query", "timeline_query", "query", false},
		{"/v1/geometry", "geometry_put", "global_config", true},
		{"/v1/site-scenes", "site_scene_put", "global_config", true},
		{"/v1/site-scenes/camera-context", "site_camera_context", "global_config", true},
		{"/v1/trajectories/geometry", "trajectory_geometry", "query", false},
		{"/v1/clocks/profiles", "clock_profile_put", "global_config", true},
		{"/v1/clocks/normalize", "clock_normalize", "global_config", true},
		{"/v1/point-graphs", "point_graph_put", "global_config", true},
		{"/v1/point-graphs/get", "point_graph_get", "global_config", true},
		{"/v1/point-trajectories/rebuild", "point_trajectory_rebuild", "ingest", false},
		{"/v1/point-trajectories/get", "point_trace", "query", false},
		{"/v1/point-trajectories/predict", "trajectory_predict", "query", false},
		{"/v1/point-trajectories/compare", "point_prediction_compare", "query", false},
	} {
		mux.HandleFunc("POST "+route.path, s.post(route.op, route.action, route.global))
	}
	mux.HandleFunc("DELETE /v1/templates/{id}", func(w http.ResponseWriter, r *http.Request) {
		body, err := readBody(w, r)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error(), nil)
			return
		}
		body["template_id"] = rawString(r.PathValue("id"))
		s.dispatch(w, r, body, "remove_template", "identity", false)
	})
	return mux
}

func rawString(value string) json.RawMessage { encoded, _ := json.Marshal(value); return encoded }

func readBody(w http.ResponseWriter, r *http.Request) (map[string]json.RawMessage, error) {
	r.Body = http.MaxBytesReader(w, r.Body, maxBody)
	decoder := json.NewDecoder(r.Body)
	var body map[string]json.RawMessage
	if err := decoder.Decode(&body); err != nil || body == nil {
		return nil, errors.New("invalid JSON object")
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		return nil, errors.New("one JSON object is required")
	}
	for key := range body {
		if strings.HasPrefix(key, "_") || key == "client_context" {
			return nil, errors.New("internal trust and client context fields are server controlled")
		}
	}
	return body, nil
}

func writeError(w http.ResponseWriter, status int, message string, claims *trust.Claims) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	result := map[string]any{"ok": false, "error": message}
	if claims != nil {
		result["client_context"] = claims.ClientContext
	}
	_ = json.NewEncoder(w).Encode(result)
}

func (s *Server) authorize(w http.ResponseWriter, r *http.Request) (trust.Claims, trust.Stamp, bool) {
	stamp, err := s.clock.Now()
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "verified UTC is unavailable; refresh the challenge-bound authority proof", nil)
		return trust.Claims{}, stamp, false
	}
	claims, err := s.verifier.Verify(r.Header.Get("Authorization"), stamp)
	if err != nil {
		writeError(w, http.StatusUnauthorized, err.Error(), nil)
		return claims, stamp, false
	}
	return claims, stamp, true
}

func activity(body map[string]json.RawMessage) string {
	var value string
	if json.Unmarshal(body["activity_id"], &value) != nil || value == "" || len(value) > 1024 {
		return ""
	}
	return value
}

func (s *Server) post(op, action string, global bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		body, err := readBody(w, r)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error(), nil)
			return
		}
		s.dispatch(w, r, body, op, action, global)
	}
}

func (s *Server) get(op, action string, global bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s.dispatch(w, r, map[string]json.RawMessage{}, op, action, global)
	}
}

func (s *Server) dispatch(w http.ResponseWriter, r *http.Request, body map[string]json.RawMessage, op, action string, global bool) {
	claims, _, ok := s.authorize(w, r)
	if !ok {
		return
	}
	id := activity(body)
	if !global && id == "" {
		writeError(w, http.StatusBadRequest, "explicit activity_id is required", &claims)
		return
	}
	if (!global && !claims.Allows(id, action)) || (global && !claims.AllowsGlobal(action)) {
		writeError(w, http.StatusForbidden, "backend identity does not authorize this activity action", &claims)
		return
	}
	body["op"] = rawString(op)
	body["_client_context"], _ = json.Marshal(claims.ClientContext)
	body["_authorization_validity"], _ = json.Marshal(map[string]int64{"not_before_unix_ms": claims.NotBefore, "expires_unix_ms": claims.Expires})
	reply, status, err := s.exchange(r.Context(), body)
	if err != nil {
		writeError(w, status, err.Error(), &claims)
		return
	}
	reply["client_context"], _ = json.Marshal(claims.ClientContext)
	if id != "" && reply["activity_id"] == nil {
		reply["activity_id"] = rawString(id)
	}
	writeReply(w, status, reply)
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	reply, status, err := s.exchange(r.Context(), map[string]json.RawMessage{"op": rawString("health")})
	if err != nil {
		writeError(w, status, err.Error(), nil)
		return
	}
	reply["clock"], _ = json.Marshal(s.clock.Challenge())
	reply["authorization_configured"], _ = json.Marshal(s.verifier != nil && s.verifier.Issuer != "" && len(s.verifier.Keys) > 0)
	writeReply(w, status, reply)
}

func writeReply(w http.ResponseWriter, status int, reply map[string]json.RawMessage) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(reply)
}

func (s *Server) exchange(parent context.Context, request map[string]json.RawMessage) (map[string]json.RawMessage, int, error) {
	encoded, _ := json.Marshal(request)
	ctx, cancel := context.WithTimeout(parent, 20*time.Second)
	defer cancel()
	response, err := s.exchanger.Request(ctx, encoded)
	if err != nil {
		if errors.Is(err, worker.ErrBusy) {
			return nil, http.StatusServiceUnavailable, errors.New("algorithm request queue is full")
		}
		if errors.Is(err, context.DeadlineExceeded) {
			return nil, http.StatusGatewayTimeout, errors.New("algorithm request timed out; retry with the same source/event ID")
		}
		return nil, http.StatusBadGateway, errors.New("C++ algorithm worker unavailable or trusted UTC lease expired")
	}
	var reply map[string]json.RawMessage
	var status struct {
		OK    *bool  `json:"ok"`
		Error string `json:"error"`
		Code  string `json:"error_code"`
	}
	if json.Unmarshal(response, &status) != nil || status.OK == nil || json.Unmarshal(response, &reply) != nil {
		return nil, http.StatusBadGateway, errors.New("C++ algorithm worker returned invalid JSON")
	}
	code := http.StatusOK
	if !*status.OK {
		switch {
		case status.Code == "VERSION_CONFLICT" || status.Code == "SOURCE_CONFLICT" || status.Code == "REBUILD_REQUIRED":
			code = http.StatusConflict
		case strings.HasPrefix(status.Error, "CAPACITY:"):
			code = http.StatusServiceUnavailable
		case status.Code == "CLOCK_UNTRUSTED" || strings.HasPrefix(status.Error, "CLOCK_UNTRUSTED:"):
			code = http.StatusServiceUnavailable
		default:
			code = http.StatusBadRequest
		}
	}
	return reply, code, nil
}
