package retention

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"testing"
	"time"
)

type testWorker struct {
	mu      sync.Mutex
	calls   []map[string]any
	fail    bool
	entered chan struct{}
	release chan struct{}
}

func (w *testWorker) Request(ctx context.Context, raw json.RawMessage) (json.RawMessage, error) {
	var body map[string]any
	_ = json.Unmarshal(raw, &body)
	w.mu.Lock()
	w.calls = append(w.calls, body)
	fail := w.fail
	w.mu.Unlock()
	if w.entered != nil {
		select {
		case w.entered <- struct{}{}:
		default:
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-w.release:
		}
	}
	if fail {
		return nil, errors.New("verified UTC proof expired")
	}
	return json.RawMessage(`{"ok":true,"retention":{"expired_records":2,"dry_run":true}}`), nil
}

func TestDefaultDryRunAndExplicitDeleteGate(t *testing.T) {
	worker := &testWorker{}
	manager, err := New(worker, time.Second, false)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := manager.Run(context.Background(), false, nil); err == nil {
		t.Fatal("deletion enabled by HTTP alone")
	}
	if len(worker.calls) != 0 {
		t.Fatal("denied deletion reached worker")
	}
	result, err := manager.Run(context.Background(), true, nil)
	if err != nil || len(result) == 0 {
		t.Fatal(err)
	}
	status := manager.Status()
	if status.Enabled || status.Attempts != 1 || status.Successes != 1 || status.Failures != 0 || !status.LastDryRun {
		t.Fatalf("wrong observability: %+v", status)
	}
	if worker.calls[0]["op"] != "retention_run" || worker.calls[0]["dry_run"] != true || len(worker.calls[0]) != 2 {
		t.Fatal("Go changed cleanup scope/deadlines")
	}
	// Returning status must not expose a mutable view of the cached report.
	status.LastResult[0] = 'x'
	if manager.Status().LastResult[0] != '{' {
		t.Fatal("status report was mutated by a reader")
	}
}

func TestRetentionFailureVisibleAndNextAttemptRecovers(t *testing.T) {
	worker := &testWorker{fail: true}
	manager, _ := New(worker, time.Second, true)
	if _, err := manager.Run(context.Background(), false, nil); err == nil {
		t.Fatal("missing trusted UTC accepted")
	}
	status := manager.Status()
	if status.Failures != 1 || status.LastError == "" || status.Running {
		t.Fatal("failure state missing")
	}
	worker.fail = false
	if _, err := manager.Run(context.Background(), false, nil); err != nil {
		t.Fatal(err)
	}
	status = manager.Status()
	if status.Successes != 1 || status.Failures != 1 || status.LastError != "" || status.Attempts != 2 {
		t.Fatal("recovery state incorrect")
	}
}

func TestPeriodicDefaultIsDryRunAndStops(t *testing.T) {
	worker := &testWorker{entered: make(chan struct{}, 1), release: make(chan struct{})}
	manager, _ := New(worker, time.Second, false)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { manager.Loop(ctx); close(done) }()
	select {
	case <-worker.entered:
	case <-time.After(3 * time.Second):
		t.Fatal("periodic cleanup did not run")
	}
	worker.mu.Lock()
	if len(worker.calls) != 1 || worker.calls[0]["dry_run"] != true {
		t.Fatal("default periodic deletion occurred")
	}
	worker.mu.Unlock()
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("periodic manager did not stop")
	}
}

func TestRetentionRunsSerializeAndCanceledWaiterDoesNotMutate(t *testing.T) {
	worker := &testWorker{entered: make(chan struct{}, 1), release: make(chan struct{})}
	manager, _ := New(worker, time.Second, true)
	done := make(chan error, 1)
	go func() { _, err := manager.Run(context.Background(), false, nil); done <- err }()
	<-worker.entered
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := manager.Run(ctx, true, nil); !errors.Is(err, context.Canceled) {
		t.Fatal("canceled waiter dispatched")
	}
	close(worker.release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if manager.Status().Attempts != 1 || len(worker.calls) != 1 {
		t.Fatal("canceled or concurrent cleanup wrote another transaction")
	}
}
