package push

import (
	"context"
	"encoding/json"
	"errors"
	"face-trajectory/internal/trust"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const testUTC int64 = 1791626400000

type clock struct {
	stamp trust.Stamp
	err   error
}

func (c clock) Now() (trust.Stamp, error) { return c.stamp, c.err }

type queuedWorker struct {
	mu    sync.Mutex
	event map[string]any
	acks  int
}

func (w *queuedWorker) Request(_ context.Context, raw json.RawMessage) (json.RawMessage, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	var request map[string]any
	_ = json.Unmarshal(raw, &request)
	if request["op"] == "push_ack" {
		if request["event_id"] != w.event["event_id"] {
			return nil, errors.New("wrong acknowledgment")
		}
		w.acks++
		return json.RawMessage(`{"ok":true}`), nil
	}
	encoded, _ := json.Marshal(map[string]any{"ok": true, "event": w.event})
	return encoded, nil
}
func pushEvent(activity string) map[string]any {
	return map[string]any{"event_id": "same-event", "activity_id": activity, "activity_num": "number-" + activity, "config_version": 1, "binding_version": 1, "monitoring_version": 1, "retention_deadline_unix_ms": testUTC + 10000, "result": map[string]any{"target_id": "person"}}
}
func validClock() clock {
	return clock{stamp: trust.Stamp{Verified: true, Now: testUTC, ValidUntil: testUTC + 120000, EvidenceID: "synthetic-test"}}
}

func TestScopedBackendDeliveryAndIdempotency(t *testing.T) {
	keys := make(map[string]string)
	receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var event map[string]any
		if err := json.NewDecoder(r.Body).Decode(&event); err != nil {
			t.Error(err)
		}
		keys[event["activity_id"].(string)] = r.Header.Get("Idempotency-Key")
		if event["retention_deadline_unix_ms"] == nil || event["monitoring_version"] == nil {
			t.Error("result lacks expiry/context")
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer receiver.Close()
	for _, activity := range []string{"A", "B"} {
		worker := &queuedWorker{event: pushEvent(activity)}
		sender, err := New(worker, receiver.URL, "", validClock())
		if err != nil {
			t.Fatal(err)
		}
		if sent, err := sender.deliver(context.Background()); err != nil || !sent || worker.acks != 1 {
			t.Fatalf("delivery: %v %v", sent, err)
		}
	}
	if keys["A"] == "" || keys["A"] == keys["B"] {
		t.Fatal("different activities shared an idempotency key")
	}
}

func TestPushExpiredUnqualifiedAndLegacyNeverPosted(t *testing.T) {
	posts := 0
	receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { posts++; w.WriteHeader(http.StatusNoContent) }))
	defer receiver.Close()
	for _, scenario := range []string{"expired", "unverified", "unleased", "missing-deadline", "unscoped", "proof-error"} {
		t.Run(scenario, func(t *testing.T) {
			worker := &queuedWorker{event: pushEvent("A")}
			reference := validClock()
			switch scenario {
			case "expired":
				worker.event["retention_deadline_unix_ms"] = testUTC - 1
			case "unverified":
				reference.stamp.Verified = false
			case "unleased":
				reference.stamp.ValidUntil = testUTC
			case "missing-deadline":
				delete(worker.event, "retention_deadline_unix_ms")
			case "unscoped":
				delete(worker.event, "activity_id")
			case "proof-error":
				reference.err = errors.New("proof expired")
			}
			sender, _ := New(worker, receiver.URL, "", reference)
			if sent, err := sender.deliver(context.Background()); sent || err == nil || worker.acks != 0 {
				t.Fatal("unqualified/expired result delivered or acknowledged")
			}
		})
	}
	if posts != 0 {
		t.Fatal("rejected queued event reached backend")
	}
}

func TestBackendFailureDoesNotLoseDurableQueue(t *testing.T) {
	receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusServiceUnavailable) }))
	defer receiver.Close()
	worker := &queuedWorker{event: pushEvent("A")}
	sender, _ := New(worker, receiver.URL, "", validClock())
	if sent, err := sender.deliver(context.Background()); sent || err == nil || worker.acks != 0 {
		t.Fatal("failed result was removed")
	}
}

func TestPushTransportCannotOutliveRetentionLease(t *testing.T) {
	receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(time.Second):
		}
	}))
	defer receiver.Close()
	worker := &queuedWorker{event: pushEvent("A")}
	worker.event["retention_deadline_unix_ms"] = testUTC + 20
	sender, _ := New(worker, receiver.URL, "", validClock())
	started := time.Now()
	if sent, err := sender.deliver(context.Background()); sent || err == nil || worker.acks != 0 {
		t.Fatal("expired in-flight result acknowledged")
	}
	if time.Since(started) > 500*time.Millisecond {
		t.Fatal("transport ignored retention deadline")
	}
}

type scopedQueueWorker struct {
	mu     sync.Mutex
	queues map[string][]map[string]any
	order  []string
	next   int
	acks   []string
}

func TestExcludedHistoricalActivityStaysQueuedWhileProductionDelivers(t *testing.T) {
	posts := 0
	receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var event map[string]any
		_ = json.NewDecoder(r.Body).Decode(&event)
		if event["activity_id"] != "production" {
			t.Error("historical event reached production receiver")
		}
		posts++
		w.WriteHeader(http.StatusNoContent)
	}))
	defer receiver.Close()
	old, live := pushEvent("historical"), pushEvent("production")
	old["event_id"], live["event_id"] = "old", "live"
	worker := &scopedQueueWorker{queues: map[string][]map[string]any{"historical": {old}, "production": {live}}, order: []string{"historical", "production"}}
	sender, _ := New(worker, receiver.URL, "", validClock())
	if err := sender.ExcludeActivities([]string{"historical"}); err != nil {
		t.Fatal(err)
	}
	if sent, err := sender.deliver(context.Background()); err != nil || !sent {
		t.Fatalf("production delivery: %v %v", sent, err)
	}
	if sent, err := sender.deliver(context.Background()); err != nil || sent {
		t.Fatalf("excluded queue: %v %v", sent, err)
	}
	if posts != 1 || len(worker.acks) != 1 || worker.acks[0] != "live" || len(worker.queues["historical"]) != 1 {
		t.Fatal("excluded evidence was sent, acknowledged or removed")
	}
}

func (w *scopedQueueWorker) Request(_ context.Context, raw json.RawMessage) (json.RawMessage, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	var request struct {
		Op       string   `json:"op"`
		ID       string   `json:"event_id"`
		Excluded []string `json:"exclude_activity_ids"`
	}
	if err := json.Unmarshal(raw, &request); err != nil {
		return nil, err
	}
	if request.Op == "push_ack" {
		for activity, queue := range w.queues {
			if len(queue) > 0 && queue[0]["event_id"] == request.ID {
				w.queues[activity] = queue[1:]
				w.acks = append(w.acks, request.ID)
				return json.RawMessage(`{"ok":true}`), nil
			}
		}
		return nil, errors.New("acknowledgment skipped an activity head")
	}
	excluded := make(map[string]bool)
	for _, activity := range request.Excluded {
		excluded[activity] = true
	}
	var event any
	for offset := 0; offset < len(w.order); offset++ {
		index := (w.next + offset) % len(w.order)
		activity := w.order[index]
		if queue := w.queues[activity]; !excluded[activity] && len(queue) > 0 {
			event = queue[0]
			w.next = (index + 1) % len(w.order)
			break
		}
	}
	encoded, _ := json.Marshal(map[string]any{"ok": true, "event": event})
	return encoded, nil
}

func TestActivityFailureDoesNotDelayOtherActivityAndPreservesScopeOrder(t *testing.T) {
	type attempt struct {
		activity string
		id       string
		at       time.Time
	}
	posted := make(chan attempt, 8)
	var recovered atomic.Bool
	receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var event struct {
			Activity string `json:"activity_id"`
			ID       string `json:"event_id"`
		}
		if err := json.NewDecoder(r.Body).Decode(&event); err != nil {
			t.Error(err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		posted <- attempt{activity: event.Activity, id: event.ID, at: time.Now()}
		if event.Activity == "A" && !recovered.Load() {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer receiver.Close()
	a1, a2, b1 := pushEvent("A"), pushEvent("A"), pushEvent("B")
	a1["event_id"], a2["event_id"], b1["event_id"] = "A-1", "A-2", "B-1"
	worker := &scopedQueueWorker{queues: map[string][]map[string]any{"A": {a1, a2}, "B": {b1}}, order: []string{"A", "B"}}
	sender, err := New(worker, receiver.URL, "", validClock())
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { sender.Run(ctx); close(done) }()
	selectAttempt := func(timeout time.Duration) attempt {
		t.Helper()
		select {
		case value := <-posted:
			return value
		case <-time.After(timeout):
			t.Fatal("expected scoped backend delivery did not arrive")
			return attempt{}
		}
	}
	first := selectAttempt(time.Second)
	if first.id != "A-1" {
		t.Fatalf("unexpected first activity head: %+v", first)
	}
	second := selectAttempt(700 * time.Millisecond)
	if second.id != "B-1" || second.at.Sub(first.at) >= 700*time.Millisecond {
		t.Fatalf("A failure held B's durable item: first=%+v second=%+v", first, second)
	}
	recovered.Store(true)
	// Producer wakeups may discover B/new activities, but must not defeat A's
	// independent backoff. This uses real HTTP delivery and the actual Run loop.
	wakesDone := make(chan struct{})
	go func() {
		defer close(wakesDone)
		ticker := time.NewTicker(10 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				select {
				case sender.wake <- struct{}{}:
				default:
				}
			}
		}
	}()
	third := selectAttempt(2 * time.Second)
	if third.id != "A-1" || third.at.Sub(first.at) < time.Second {
		t.Fatalf("A's head/order/backoff was bypassed: first=%+v third=%+v", first, third)
	}
	fourth := selectAttempt(time.Second)
	if fourth.id != "A-2" {
		t.Fatalf("A's following event overtook its retry: %+v", fourth)
	}
	// The receiver posts an attempt before responding, so wait for C++'s mocked
	// commit acknowledgment rather than treating a request as a delivered item.
	ackDeadline := time.Now().Add(time.Second)
	for {
		worker.mu.Lock()
		acks := append([]string(nil), worker.acks...)
		worker.mu.Unlock()
		if len(acks) == 3 {
			if acks[0] != "B-1" || acks[1] != "A-1" || acks[2] != "A-2" {
				t.Fatalf("cross-activity acknowledgement order: %v", acks)
			}
			break
		}
		if time.Now().After(ackDeadline) {
			t.Fatalf("expected all three durable acknowledgments, got %v", acks)
		}
		time.Sleep(time.Millisecond)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("sender did not stop after cancellation")
	}
	<-wakesDone
}

func TestActivityBackoffIsIndependentAndCapped(t *testing.T) {
	start := time.Now()
	a, b := &activityRetry{}, &activityRetry{}
	a.failed(start)
	b.failed(start.Add(500 * time.Millisecond))
	for range 10 {
		a.failed(start)
	}
	if a.delay != 30*time.Second || b.delay != time.Second {
		t.Fatalf("activity delays interfered or overflowed: A=%s B=%s", a.delay, b.delay)
	}
	excluded, wait := retrySelection(map[string]*activityRetry{"A": a, "B": b}, start.Add(time.Second))
	if len(excluded) != 2 || excluded[0] != "A" || excluded[1] != "B" || wait != 500*time.Millisecond {
		t.Fatalf("selection ignored nearest independent retry: %v %s", excluded, wait)
	}
	excluded, _ = retrySelection(map[string]*activityRetry{"A": a, "B": b}, start.Add(2*time.Second))
	if len(excluded) != 1 || excluded[0] != "A" {
		t.Fatalf("B was still excluded after its own deadline: %v", excluded)
	}
}
