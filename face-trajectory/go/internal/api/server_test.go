package api

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"face-trajectory/internal/retention"
	"face-trajectory/internal/trust"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

const fixtureUTC int64 = 1791626400000

type testWorker struct {
	mu       sync.Mutex
	calls    []map[string]json.RawMessage
	events   []map[string]any
	sequence uint64
}

func (w *testWorker) Request(_ context.Context, raw json.RawMessage) (json.RawMessage, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	var request map[string]json.RawMessage
	_ = json.Unmarshal(raw, &request)
	w.calls = append(w.calls, request)
	var op string
	_ = json.Unmarshal(request["op"], &op)
	if op == "activity_results" {
		id := activity(request)
		var after uint64
		_ = json.Unmarshal(request["after_sequence"], &after)
		result := map[string]any{"ok": true, "activity_id": id, "activity_num": "number-" + id, "config_version": 1, "binding_version": 1, "monitoring_version": 1, "latest_sequence": w.sequence, "events": []map[string]any{}}
		for _, event := range w.events {
			if event["activity_id"] == id && event["sequence"].(uint64) > after {
				result["events"] = append(result["events"].([]map[string]any), event)
			}
		}
		encoded, _ := json.Marshal(result)
		return encoded, nil
	}
	encoded, _ := json.Marshal(map[string]any{"ok": true, "activity_id": activity(request), "config_version": 1, "binding_version": 1, "monitoring_version": 1, "enabled": true})
	return encoded, nil
}

func token(t *testing.T, value any, key ed25519.PrivateKey) string {
	t.Helper()
	body, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return base64.RawURLEncoding.EncodeToString(body) + "." + base64.RawURLEncoding.EncodeToString(ed25519.Sign(key, body))
}

type apiFixture struct {
	handler http.Handler
	worker  *testWorker
	key     ed25519.PrivateKey
	claims  trust.Claims
	clock   *trust.Clock
}

func fixture(t *testing.T) *apiFixture {
	t.Helper()
	public, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	proof := trust.ClockProof{Issuer: "utc-test", KeyID: "test", Now: fixtureUTC, Uncertainty: 5, ValidFor: 120000, SourceVerified: true, Source: "SYNTHETIC_TEST", EvidenceID: "test-only"}
	// Test-owned signed synthetic reference, never a system-clock trust claim.
	path := filepath.Join(t.TempDir(), "clock-proof.txt")
	clock, err := trust.NewClock(trust.Keys{"test": public}, "utc-test", path)
	if err != nil {
		t.Fatal(err)
	}
	proof.Challenge = clock.Challenge()["challenge"].(string)
	if err = os.WriteFile(path, []byte(token(t, proof, key)), 0600); err != nil {
		t.Fatal(err)
	}
	worker := &testWorker{}
	claims := trust.Claims{Issuer: "backend-test", KeyID: "test", ClientContext: trust.ClientContext{Subject: "alice", Session: "login-a", ContextID: "selection-a", ContextGeneration: 1, AuthzRevision: 1}, NotBefore: fixtureUTC - 1000, Expires: fixtureUTC + 60000, Grants: []trust.Grant{{ActivityID: "A", Actions: []string{"query", "subscribe", "config", "monitor", "ingest", "identity"}}}, GlobalActions: []string{"global_config", "push_status"}}
	return &apiFixture{handler: NewAuthorized(worker, &trust.Verifier{Keys: trust.Keys{"test": public}, Issuer: claims.Issuer}, clock), worker: worker, key: key, claims: claims, clock: clock}
}

func TestRetentionHTTPRequiresAdminStartupGateAndRejectsPaths(t *testing.T) {
	f := fixture(t)
	manager, _ := retention.New(f.worker, time.Second, false)
	public := f.key.Public().(ed25519.PublicKey)
	f.handler = NewAuthorized(f.worker, &trust.Verifier{Keys: trust.Keys{"test": public}, Issuer: f.claims.Issuer}, f.clock, manager)
	status, _ := f.request(t, "/v1/retention/run", map[string]any{"dry_run": true}, &f.claims)
	if status != http.StatusForbidden {
		t.Fatal("nonadministrator invoked retention")
	}
	admin := f.claims
	admin.GlobalActions = append(admin.GlobalActions, "retention")
	for _, body := range []map[string]any{{"dry_run": true, "paths": []string{"other.sqlite"}}, {"dry_run": true, "activity_id": "A"}, {"dry_run": nil}, {}} {
		status, _ = f.request(t, "/v1/retention/run", body, &admin)
		if status != http.StatusBadRequest {
			t.Fatal("cleanup accepted client-controlled scope or null mode")
		}
	}
	status, _ = f.request(t, "/v1/retention/run", map[string]any{"dry_run": false}, &admin)
	if status != http.StatusConflict {
		t.Fatal("HTTP bypassed deletion startup gate")
	}
	status, reply := f.request(t, "/v1/retention/run", map[string]any{"dry_run": true}, &admin)
	if status != http.StatusOK || reply["client_context"] == nil || manager.Status().Attempts != 1 {
		t.Fatal("authorized dry run failed")
	}
}

func (f *apiFixture) request(t *testing.T, path string, body any, claims *trust.Claims) (int, map[string]any) {
	t.Helper()
	encoded, _ := json.Marshal(body)
	r := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(encoded))
	if claims != nil {
		r.Header.Set("Authorization", "Bearer "+token(t, *claims, f.key))
	}
	w := httptest.NewRecorder()
	f.handler.ServeHTTP(w, r)
	var reply map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &reply); err != nil {
		t.Fatal(err)
	}
	return w.Code, reply
}

func TestEveryActivityRouteRequiresAuthorizedAction(t *testing.T) {
	f := fixture(t)
	paths := []string{"/v1/activities/sync", "/v1/activities/bindings", "/v1/activities/monitoring", "/v1/activities/get", "/v1/templates", "/v1/search", "/v1/associations/observe", "/v1/associations/correct", "/v1/associations/rebuild", "/v1/monitoring/persons", "/v1/monitoring/observe", "/v1/monitoring/gate-event", "/v1/monitoring/get", "/v1/visits/build", "/v1/trajectories/raw", "/v1/trajectories/rebuild", "/v1/trajectories/get", "/v1/timeline/query", "/v1/trajectories/geometry", "/v1/point-trajectories/rebuild", "/v1/point-trajectories/get", "/v1/point-trajectories/predict", "/v1/point-trajectories/compare"}
	denied := f.claims
	denied.Grants = nil
	for _, path := range paths {
		t.Run(path, func(t *testing.T) {
			status, _ := f.request(t, path, map[string]any{"activity_id": "A"}, nil)
			if status != http.StatusUnauthorized {
				t.Fatalf("missing signed identity: %d", status)
			}
			status, _ = f.request(t, path, map[string]any{"activity_id": "A"}, &denied)
			if status != http.StatusForbidden {
				t.Fatalf("missing action grant: %d", status)
			}
			status, _ = f.request(t, path, map[string]any{"activity_id": "B"}, &f.claims)
			if status != http.StatusForbidden {
				t.Fatalf("cross activity: %d", status)
			}
		})
	}
	if len(f.worker.calls) != 0 {
		t.Fatal("denied requests reached algorithm")
	}
	status, reply := f.request(t, "/v1/activities/get", map[string]any{"activity_id": "A"}, &f.claims)
	if status != http.StatusOK || reply["client_context"].(map[string]any)["context_id"] != "selection-a" {
		t.Fatal("signed response context missing")
	}
}

func TestGlobalRoutesAndPrivateFieldsFailClosed(t *testing.T) {
	f := fixture(t)
	denied := f.claims
	denied.GlobalActions = nil
	for _, path := range []string{"/v1/geometry", "/v1/site-scenes", "/v1/site-scenes/camera-context", "/v1/clocks/profiles", "/v1/clocks/normalize", "/v1/point-graphs", "/v1/point-graphs/get"} {
		status, _ := f.request(t, path, map[string]any{}, &denied)
		if status != http.StatusForbidden {
			t.Fatalf("%s bypassed global authorization", path)
		}
	}
	for _, field := range []string{"_trusted_clock", "_normalization", "_client_context", "_authorization_validity", "client_context"} {
		status, _ := f.request(t, "/v1/activities/get", map[string]any{"activity_id": "A", field: map[string]any{}}, &f.claims)
		if status != http.StatusBadRequest {
			t.Fatalf("client injected %s", field)
		}
	}
	if len(f.worker.calls) != 0 {
		t.Fatal("forged trust reached algorithm")
	}
	defaultServer := New(f.worker)
	w := httptest.NewRecorder()
	defaultServer.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/v1/activities/get", bytes.NewBufferString(`{"activity_id":"A"}`)))
	if w.Code != http.StatusServiceUnavailable {
		t.Fatal("unconfigured server did not fail closed")
	}
}

func TestSubscriptionSwitchSessionOwnershipAndCommittedResults(t *testing.T) {
	f := fixture(t)
	status, opened := f.request(t, "/v1/subscriptions/open", map[string]any{"activity_id": "A", "config_version": 1, "binding_version": 1, "monitoring_version": 1}, &f.claims)
	if status != http.StatusOK {
		t.Fatalf("open: %+v", opened)
	}
	request := map[string]any{"subscription_id": opened["subscription_id"], "generation": opened["generation"]}
	f.worker.sequence = 1
	f.worker.events = []map[string]any{{"activity_id": "A", "activity_num": "number-A", "config_version": 1, "binding_version": 1, "monitoring_version": 1, "sequence": uint64(1), "operation": "monitoring_observe", "result": map[string]any{"target_id": "person"}}}
	status, poll := f.request(t, "/v1/subscriptions/poll", request, &f.claims)
	if status != http.StatusOK || len(poll["events"].([]any)) != 1 {
		t.Fatalf("actual result not delivered: %+v", poll)
	}
	event := poll["events"].([]any)[0].(map[string]any)
	if event["subscription_id"] != opened["subscription_id"] || event["client_context"].(map[string]any)["context_id"] != "selection-a" {
		t.Fatal("event lacks discard context")
	}
	status, poll = f.request(t, "/v1/subscriptions/poll", request, &f.claims)
	if status != http.StatusOK || len(poll["events"].([]any)) != 0 {
		t.Fatal("cursor replayed committed result")
	}
	for _, change := range []func(*trust.Claims){func(c *trust.Claims) { c.ContextID = "selection-b"; c.ContextGeneration++ }, func(c *trust.Claims) { c.Session = "login-b" }, func(c *trust.Claims) { c.Subject = "bob" }, func(c *trust.Claims) { c.AuthzRevision++ }} {
		other := f.claims
		change(&other)
		status, _ = f.request(t, "/v1/subscriptions/poll", request, &other)
		if status != http.StatusForbidden {
			t.Fatal("old subscription survived changed access context")
		}
	}
	before := len(f.worker.calls)
	status, _ = f.request(t, "/v1/subscriptions/close", request, &f.claims)
	if status != http.StatusOK || len(f.worker.calls) != before {
		t.Fatal("closing viewer changed algorithm lifecycle")
	}
	status, _ = f.request(t, "/v1/subscriptions/poll", request, &f.claims)
	if status != http.StatusGone {
		t.Fatal("closed subscription still pollable")
	}
}

func TestSubscriptionRejectsWrongScopeWithoutAdvancingCursor(t *testing.T) {
	f := fixture(t)
	_, opened := f.request(t, "/v1/subscriptions/open", map[string]any{"activity_id": "A", "config_version": 1, "binding_version": 1, "monitoring_version": 1}, &f.claims)
	request := map[string]any{"subscription_id": opened["subscription_id"], "generation": opened["generation"]}
	f.worker.sequence = 2
	f.worker.events = []map[string]any{{"activity_id": "A", "config_version": 1, "binding_version": 1, "monitoring_version": 1, "sequence": uint64(1)}, {"activity_id": "A", "config_version": 1, "binding_version": 2, "monitoring_version": 1, "sequence": uint64(2)}}
	status, _ := f.request(t, "/v1/subscriptions/poll", request, &f.claims)
	if status != http.StatusConflict {
		t.Fatal("stale version result delivered")
	}
	f.worker.events = f.worker.events[:1]
	status, reply := f.request(t, "/v1/subscriptions/poll", request, &f.claims)
	if status != http.StatusOK || len(reply["events"].([]any)) != 1 {
		t.Fatal("invalid batch advanced cursor")
	}
}
