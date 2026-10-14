// Package retention schedules the C++ retention transaction. It never computes
// trajectory deadlines or opens an algorithm/P1 SQLite database in Go.
package retention

import (
	"context"
	"encoding/json"
	"errors"
	"face-trajectory/internal/trust"
	"log"
	"sync"
	"time"
)

type Exchanger interface {
	Request(context.Context, json.RawMessage) (json.RawMessage, error)
}

type Status struct {
	Enabled        bool            `json:"enabled"`
	IntervalMS     int64           `json:"interval_ms"`
	Running        bool            `json:"running"`
	Attempts       uint64          `json:"attempts"`
	Successes      uint64          `json:"successes"`
	Failures       uint64          `json:"failures"`
	LastDryRun     bool            `json:"last_dry_run"`
	LastDurationMS int64           `json:"last_duration_ms"`
	LastError      string          `json:"last_error,omitempty"`
	LastResult     json.RawMessage `json:"last_result,omitempty"`
}

type Manager struct {
	worker   Exchanger
	interval time.Duration
	enabled  bool
	gate     chan struct{}
	mu       sync.Mutex
	status   Status
}

func New(worker Exchanger, interval time.Duration, enabled bool) (*Manager, error) {
	if interval < time.Second || interval > 24*time.Hour {
		return nil, errors.New("retention interval must be between one second and 24 hours")
	}
	return &Manager{worker: worker, interval: interval, enabled: enabled, gate: make(chan struct{}, 1), status: Status{Enabled: enabled, IntervalMS: interval.Milliseconds()}}, nil
}

func (m *Manager) Enabled() bool { return m != nil && m.enabled }

func (m *Manager) Status() Status {
	if m == nil {
		return Status{}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	result := m.status
	result.LastResult = append(json.RawMessage(nil), result.LastResult...)
	return result
}

// Run is serialized with the periodic loop. Deadlines remain exclusively in
// C++, using the trusted UTC inserted immediately before worker dispatch.
func (m *Manager) Run(ctx context.Context, dryRun bool, claims *trust.Claims) (json.RawMessage, error) {
	if m == nil {
		return nil, errors.New("retention manager is not configured")
	}
	if !dryRun && !m.enabled {
		return nil, errors.New("destructive retention requires the explicit retention-enabled startup flag")
	}
	select {
	case m.gate <- struct{}{}:
		defer func() { <-m.gate }()
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	start := time.Now()
	m.mu.Lock()
	m.status.Running = true
	m.status.Attempts++
	m.status.LastDryRun = dryRun
	m.mu.Unlock()
	request := map[string]any{"op": "retention_run", "dry_run": dryRun}
	if claims != nil {
		request["_client_context"] = claims.ClientContext
		request["_authorization_validity"] = map[string]int64{"not_before_unix_ms": claims.NotBefore, "expires_unix_ms": claims.Expires}
	}
	encoded, _ := json.Marshal(request)
	result, err := m.worker.Request(ctx, encoded)
	var reply struct {
		OK *bool `json:"ok"`
	}
	if err == nil && (json.Unmarshal(result, &reply) != nil || reply.OK == nil) {
		err = errors.New("invalid C++ retention response")
	}
	if err == nil && !*reply.OK {
		err = errors.New("C++ retention rejected the operation; inspect last_result")
	}
	m.mu.Lock()
	m.status.Running = false
	m.status.LastDurationMS = time.Since(start).Milliseconds()
	m.status.LastResult = append(json.RawMessage(nil), result...)
	m.status.LastError = ""
	if err != nil {
		m.status.Failures++
		m.status.LastError = err.Error()
	} else {
		m.status.Successes++
	}
	m.mu.Unlock()
	return result, err
}

// Default mode periodically reports a read-only dry run. Deletion starts only
// when deployment explicitly enables retention; expired/missing clock proofs
// make the adapter fail closed and are observable in Status.
func (m *Manager) Loop(ctx context.Context) {
	ticker := time.NewTicker(m.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		_, err := m.Run(ctx, !m.enabled, nil)
		if ctx.Err() != nil {
			return
		}
		status := m.Status()
		if err != nil {
			log.Printf("retention run failed; dry_run=%t attempts=%d failures=%d duration_ms=%d", status.LastDryRun, status.Attempts, status.Failures, status.LastDurationMS)
		} else {
			log.Printf("retention run completed; dry_run=%t attempts=%d successes=%d duration_ms=%d", status.LastDryRun, status.Attempts, status.Successes, status.LastDurationMS)
		}
	}
}
