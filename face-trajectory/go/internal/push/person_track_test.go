package push

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func trackEnvelope() map[string]any {
	e := pushEvent("A")
	e["retention_deadline_unix_ms"] = testUTC - 1
	e["person_track_event"] = map[string]any{"event_id": "same-event", "event_type": "UPSERT", "revision": 1,
		"activity_id": "A", "source_id": "S", "observation_id": "face-0", "person_id": "P",
		"camera_id": "C", "camera_name": nil, "captured_at_unix_ms": testUTC - 604800001,
		"clock_uncertainty_ms": 0, "capture_photo_url": nil}
	return e
}

// Catches accepting transport-only success or sending an internal envelope.
func TestPersonTrackRequiresMatchingBusinessReceipt(t *testing.T) {
	for _, tc := range []struct {
		name    string
		status  int
		body    string
		success bool
	}{
		{"saved", 200, `{"code":200,"event_id":"same-event","message":"保存成功"}`, true},
		{"old revision", 200, `{"code":200,"event_id":"same-event","message":"已忽略旧修订"}`, true},
		{"empty", 200, ``, false}, {"wrong id", 200, `{"code":200,"event_id":"other"}`, false},
		{"wrong code", 200, `{"code":500,"event_id":"same-event"}`, false},
		{"no content", 204, ``, false}, {"accepted", 202, `{"code":200,"event_id":"same-event"}`, false},
		{"conflict", 409, `{"code":409,"event_id":"same-event"}`, false},
		{"trailing JSON", 200, `{"code":200,"event_id":"same-event"}{}`, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			posted := false
			receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				posted = true
				raw, _ := io.ReadAll(r.Body)
				var body map[string]any
				_ = json.Unmarshal(raw, &body)
				if len(body) != 12 || body["event_type"] != "UPSERT" || body["person_track_event"] != nil {
					t.Errorf("wrong wire body: %s", raw)
				}
				if r.Header.Get("Authorization") != "Bearer dedicated-token" {
					t.Error("missing credential")
				}
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			}))
			defer receiver.Close()
			worker := &queuedWorker{event: trackEnvelope()}
			sender, _ := New(worker, receiver.URL, "Bearer dedicated-token", validClock())
			sent, err := sender.deliver(context.Background())
			if !posted || sent != tc.success || (err == nil) != tc.success || (worker.acks == 1) != tc.success {
				t.Fatalf("posted=%v sent=%v ack=%d err=%v", posted, sent, worker.acks, err)
			}
		})
	}
}

func TestPersonTrackRetryKeepsOriginalBody(t *testing.T) {
	var bodies []string
	receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		bodies = append(bodies, string(raw))
		if len(bodies) == 1 {
			w.WriteHeader(503)
			return
		}
		_, _ = w.Write([]byte(`{"code":200,"event_id":"same-event"}`))
	}))
	defer receiver.Close()
	worker := &queuedWorker{event: trackEnvelope()}
	sender, _ := New(worker, receiver.URL, "", validClock())
	if sent, _ := sender.deliver(context.Background()); sent || worker.acks != 0 {
		t.Fatal("failure acknowledged")
	}
	if sent, err := sender.deliver(context.Background()); !sent || err != nil || worker.acks != 1 {
		t.Fatalf("retry: %v %v", sent, err)
	}
	if len(bodies) != 2 || bodies[0] != bodies[1] {
		t.Fatal("retry mutated event")
	}
}

func TestProtocolRetrySchedule(t *testing.T) {
	retry := activityRetry{}
	now := time.Now()
	for _, seconds := range []int{1, 2, 5, 10, 30, 30, 30} {
		retry.failed(now)
		if retry.delay != time.Duration(seconds)*time.Second {
			t.Fatalf("delay %s expected %ds", retry.delay, seconds)
		}
	}
}
