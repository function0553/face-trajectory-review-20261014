package worker

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
)

// Requester is the existing C++ transaction transport. Fair serializes accepted
// work with a round robin across explicit activities; cancelled viewers do not
// cancel another activity or mutate its monitoring lifecycle.
type Requester interface {
	Request(context.Context, json.RawMessage) (json.RawMessage, error)
}
type fairCall struct {
	ctx     context.Context
	data    json.RawMessage
	result  chan transactionResult
	started bool // protected by Fair.mu; cancellation is admission-only
}
type Fair struct {
	inner              Requester
	mu                 sync.Mutex
	queues             map[string][]*fairCall
	order              []string
	total              int
	limit, perActivity int
	wake               chan struct{}
	done               chan struct{}
	once               sync.Once
	closed             bool
}

func NewFair(inner Requester, limit, perActivity int) *Fair {
	if limit < 1 || perActivity < 1 {
		panic("positive fair queue limits required")
	}
	f := &Fair{inner: inner, queues: make(map[string][]*fairCall), limit: limit, perActivity: perActivity, wake: make(chan struct{}, 1), done: make(chan struct{})}
	go f.run()
	return f
}
func (f *Fair) Request(ctx context.Context, request json.RawMessage) (json.RawMessage, error) {
	var scope struct {
		ActivityID string `json:"activity_id"`
	}
	if err := json.Unmarshal(request, &scope); err != nil {
		return nil, err
	}
	// Global config/control is one scheduler participant, not a priority bypass.
	id := scope.ActivityID
	c := &fairCall{ctx: ctx, data: append(json.RawMessage(nil), request...), result: make(chan transactionResult, 1)}
	f.mu.Lock()
	if f.closed {
		f.mu.Unlock()
		return nil, errors.New("fair worker is closing")
	}
	if f.total >= f.limit || len(f.queues[id]) >= f.perActivity {
		f.mu.Unlock()
		return nil, ErrBusy
	}
	if len(f.queues[id]) == 0 {
		f.order = append(f.order, id)
	}
	f.queues[id] = append(f.queues[id], c)
	f.total++
	f.mu.Unlock()
	select {
	case f.wake <- struct{}{}:
	default:
	}
	select {
	case r := <-c.result:
		return r.response, r.err
	case <-ctx.Done():
		f.mu.Lock()
		started := c.started
		f.mu.Unlock()
		if started {
			r := <-c.result
			return r.response, r.err
		}
		return nil, ctx.Err()
	case <-f.done:
		f.mu.Lock()
		started := c.started
		f.mu.Unlock()
		if started {
			r := <-c.result
			return r.response, r.err
		}
		return nil, errors.New("fair worker is closing")
	}
}
func (f *Fair) next() (*fairCall, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.closed || len(f.order) == 0 {
		return nil, false
	}
	id := f.order[0]
	f.order = f.order[1:]
	q := f.queues[id]
	c := q[0]
	q = q[1:]
	f.total--
	if len(q) > 0 {
		f.order = append(f.order, id)
		f.queues[id] = q
	} else {
		delete(f.queues, id)
	}
	// Check and mark atomically with Request's cancellation branch. A queued
	// cancelled call never crosses this boundary; a running call waits for I/O.
	c.started = c.ctx.Err() == nil
	return c, true
}
func (f *Fair) run() {
	for {
		if c, ok := f.next(); ok {
			if !c.started {
				err := c.ctx.Err()
				c.result <- transactionResult{err: err}
				continue
			}
			r, err := f.inner.Request(c.ctx, c.data)
			c.result <- transactionResult{response: r, err: err}
			continue
		}
		select {
		case <-f.wake:
		case <-f.done:
			return
		}
	}
}
func (f *Fair) Close() {
	f.once.Do(func() { f.mu.Lock(); f.closed = true; f.mu.Unlock(); close(f.done) })
}
