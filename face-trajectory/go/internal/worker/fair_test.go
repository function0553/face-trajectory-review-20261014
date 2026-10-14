package worker

import (
	"context"
	"encoding/json"
	"sync"
	"testing"
	"time"
)

type orderedTransport struct {
	mu      sync.Mutex
	seen    []string
	started chan struct{}
	release chan struct{}
	once    sync.Once
}

func (o *orderedTransport) Request(_ context.Context, b json.RawMessage) (json.RawMessage, error) {
	var r struct {
		Activity string `json:"activity_id"`
	}
	_ = json.Unmarshal(b, &r)
	o.once.Do(func() { close(o.started); <-o.release })
	o.mu.Lock()
	o.seen = append(o.seen, r.Activity)
	o.mu.Unlock()
	return json.RawMessage(`{"ok":true}`), nil
}
func TestFairActivityRoundRobin(t *testing.T) {
	o := &orderedTransport{started: make(chan struct{}), release: make(chan struct{})}
	f := NewFair(o, 32, 16)
	defer f.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var wg sync.WaitGroup
	submit := func(id string) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := f.Request(ctx, json.RawMessage(`{"activity_id":"`+id+`"}`))
			if err != nil {
				t.Error(err)
			}
		}()
	}
	submit("A")
	<-o.started
	for i := 0; i < 8; i++ {
		submit("A")
	}
	submit("B")
	submit("C")
	deadline := time.Now().Add(time.Second)
	for {
		f.mu.Lock()
		n := f.total
		f.mu.Unlock()
		if n == 10 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("requests did not enqueue")
		}
		time.Sleep(time.Millisecond)
	}
	close(o.release)
	wg.Wait()
	o.mu.Lock()
	defer o.mu.Unlock()
	positions := map[string]int{}
	for i, id := range o.seen {
		if id != "A" {
			positions[id] = i
		}
	}
	if len(positions) != 2 || positions["B"] > 3 || positions["C"] > 3 {
		t.Fatalf("starved activity: %v", o.seen)
	}
}
func TestFairPerActivityCapacityReservesOtherActivity(t *testing.T) {
	o := &orderedTransport{started: make(chan struct{}), release: make(chan struct{})}
	f := NewFair(o, 8, 1)
	defer f.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	go f.Request(ctx, json.RawMessage(`{"activity_id":"A"}`))
	<-o.started
	go f.Request(ctx, json.RawMessage(`{"activity_id":"A"}`))
	deadline := time.Now().Add(time.Second)
	for {
		f.mu.Lock()
		n := f.total
		f.mu.Unlock()
		if n == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("not enqueued")
		}
		time.Sleep(time.Millisecond)
	}
	if _, err := f.Request(ctx, json.RawMessage(`{"activity_id":"A"}`)); err != ErrBusy {
		t.Fatalf("want busy, got %v", err)
	}
	result := make(chan error, 1)
	go func() { _, err := f.Request(ctx, json.RawMessage(`{"activity_id":"B"}`)); result <- err }()
	close(o.release)
	if err := <-result; err != nil {
		t.Fatal(err)
	}
}
