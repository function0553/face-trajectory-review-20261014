// Package push delivers transactionally queued algorithm results to a backend.
package push

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"face-trajectory/internal/trust"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

type Exchanger interface {
	Request(context.Context, json.RawMessage) (json.RawMessage, error)
}

type TrustedClock interface{ Now() (trust.Stamp, error) }

type Sender struct {
	worker        Exchanger
	endpoint      string
	authorization string
	client        *http.Client
	wake          chan struct{}
	clock         TrustedClock
}

func New(worker Exchanger, endpoint, authorization string, clocks ...TrustedClock) (*Sender, error) {
	if endpoint != "" {
		u, err := url.Parse(endpoint)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil || u.Fragment != "" {
			return nil, errors.New("push-url must be an absolute HTTP(S) URL without userinfo or fragment")
		}
	}
	if strings.ContainsAny(authorization, "\r\n") {
		return nil, errors.New("invalid push authorization header")
	}
	var clock TrustedClock
	if len(clocks) > 0 {
		clock = clocks[0]
	}
	return &Sender{worker: worker, endpoint: endpoint, authorization: authorization, clock: clock,
		wake: make(chan struct{}, 1), client: &http.Client{Timeout: 10 * time.Second,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

// HTTP cancellation cannot remove an already committed outbox item. Run also
// checks the queue periodically, including after a timed-out caller disconnects.
func (s *Sender) Request(ctx context.Context, request json.RawMessage) (json.RawMessage, error) {
	result, err := s.worker.Request(ctx, request)
	var operation struct {
		Op string `json:"op"`
	}
	if err == nil && json.Unmarshal(request, &operation) == nil && operation.Op == "push_status" {
		var reply map[string]json.RawMessage
		if json.Unmarshal(result, &reply) == nil {
			var status map[string]any
			if json.Unmarshal(reply["push"], &status) == nil && status != nil {
				status["receiver_configured"] = s.endpoint != ""
				reply["push"], _ = json.Marshal(status)
				result, _ = json.Marshal(reply)
			}
		}
	}
	select {
	case s.wake <- struct{}{}:
	default:
	}
	return result, err
}

func (s *Sender) control(ctx context.Context, request any) (json.RawMessage, error) {
	body, err := json.Marshal(request)
	if err != nil {
		return nil, err
	}
	result, err := s.worker.Request(ctx, body)
	if err != nil {
		return nil, err
	}
	var status struct {
		OK    bool   `json:"ok"`
		Error string `json:"error"`
	}
	if err := json.Unmarshal(result, &status); err != nil {
		return nil, err
	}
	if !status.OK {
		return nil, fmt.Errorf("push worker: %s", status.Error)
	}
	return result, nil
}

func (s *Sender) deliver(ctx context.Context) (bool, error) {
	sent, _, err := s.deliverSelected(ctx, nil)
	return sent, err
}

// The worker returns an eligible activity's oldest item. A failed activity is
// excluded during its own backoff so it cannot hold another activity's queue.
func (s *Sender) deliverSelected(ctx context.Context, excluded []string) (bool, string, error) {
	selection := map[string]any{"op": "push_next"}
	if len(excluded) > 0 {
		selection["exclude_activity_ids"] = excluded
	}
	result, err := s.control(ctx, selection)
	if err != nil {
		return false, "", err
	}
	var reply struct {
		Event json.RawMessage `json:"event"`
	}
	if err := json.Unmarshal(result, &reply); err != nil {
		return false, "", err
	}
	if len(reply.Event) == 0 || bytes.Equal(reply.Event, []byte("null")) {
		return false, "", nil
	}
	var event struct {
		ID                string  `json:"event_id"`
		ActivityID        string  `json:"activity_id"`
		ActivityNum       string  `json:"activity_num"`
		ConfigVersion     *uint64 `json:"config_version"`
		BindingVersion    *uint64 `json:"binding_version"`
		MonitoringVersion *uint64 `json:"monitoring_version"`
		Deadline          *int64  `json:"retention_deadline_unix_ms"`
	}
	if err := json.Unmarshal(reply.Event, &event); err != nil || event.ID == "" || event.ActivityID == "" || event.ActivityNum == "" || event.ConfigVersion == nil || event.BindingVersion == nil || event.MonitoringVersion == nil || event.Deadline == nil {
		return false, event.ActivityID, errors.New("queued push requires explicit activity identity, versions and retention deadline")
	}
	if s.clock == nil {
		return false, "", errors.New("backend push requires verified UTC")
	}
	stamp, err := s.clock.Now()
	if err != nil || !stamp.Verified || stamp.ValidUntil <= stamp.Now {
		return false, "", errors.New("backend push requires a fresh verified UTC lease")
	}
	if stamp.Now > *event.Deadline {
		return false, event.ActivityID, errors.New("backend push deadline expired")
	}
	// Equality at the cutoff is retained. The receiver must independently reject
	// delivery after the envelope deadline; sender cancellation cannot recall a
	// request already accepted by an external receiver.
	remaining := *event.Deadline - stamp.Now + 1
	if lease := stamp.ValidUntil - stamp.Now; lease < remaining {
		remaining = lease
	}
	if remaining > 10000 {
		remaining = 10000
	}
	deliveryCtx, cancel := context.WithTimeout(ctx, time.Duration(remaining)*time.Millisecond)
	defer cancel()
	request, err := http.NewRequestWithContext(deliveryCtx, http.MethodPost, s.endpoint, bytes.NewReader(reply.Event))
	if err != nil {
		return false, event.ActivityID, errors.New("cannot create backend push request")
	}
	request.Header.Set("Content-Type", "application/json")
	keyBytes, _ := json.Marshal([]string{event.ActivityID, event.ID})
	keyHash := sha256.Sum256(keyBytes)
	request.Header.Set("Idempotency-Key", hex.EncodeToString(keyHash[:]))
	if s.authorization != "" {
		request.Header.Set("Authorization", s.authorization)
	}
	response, err := s.client.Do(request)
	if err != nil {
		return false, event.ActivityID, errors.New("backend push transport failed")
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
	_ = response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return false, event.ActivityID, fmt.Errorf("backend push HTTP %d", response.StatusCode)
	}
	_, err = s.control(ctx, map[string]string{"op": "push_ack", "event_id": event.ID})
	return err == nil, event.ActivityID, err
}

type activityRetry struct {
	delay time.Duration
	next  time.Time
}

func (r *activityRetry) failed(now time.Time) {
	if r.delay == 0 {
		r.delay = time.Second
	} else if r.delay < 30*time.Second {
		r.delay *= 2
		if r.delay > 30*time.Second {
			r.delay = 30 * time.Second
		}
	}
	r.next = now.Add(r.delay)
}

func retrySelection(retries map[string]*activityRetry, now time.Time) ([]string, time.Duration) {
	excluded := make([]string, 0, len(retries))
	wait := time.Second
	for activity, retry := range retries {
		if remaining := retry.next.Sub(now); remaining > 0 {
			excluded = append(excluded, activity)
			if remaining < wait {
				wait = remaining
			}
		}
	}
	sort.Strings(excluded)
	return excluded, wait
}

func waitRetry(ctx context.Context, delay time.Duration, wake <-chan struct{}) bool {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	case <-wake:
		return true
	}
}

// One sender preserves commit order inside each activity. The C++ selector
// rotates activity heads; failure delays only that activity and retains its
// oldest durable item. Global transport/clock failures use a separate backoff.
func (s *Sender) Run(ctx context.Context) {
	if s.endpoint == "" {
		return
	}
	defer s.client.CloseIdleConnections()
	retries := make(map[string]*activityRetry)
	global := activityRetry{}
	for ctx.Err() == nil {
		excluded, idle := retrySelection(retries, time.Now())
		sent, activity, err := s.deliverSelected(ctx, excluded)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			if activity == "" {
				global.failed(time.Now())
				log.Printf("%v; queued results retained, global retry in %s", err, global.delay)
				// Producer wakeups cannot shorten a failed global reference's backoff.
				if !waitRetry(ctx, global.delay, nil) {
					return
				}
			} else {
				retry := retries[activity]
				if retry == nil {
					retry = &activityRetry{}
					retries[activity] = retry
				}
				retry.failed(time.Now())
				log.Printf("%v; activity=%s queued result retained, activity retry in %s", err, activity, retry.delay)
			}
			continue
		}
		global = activityRetry{}
		if sent {
			delete(retries, activity)
			continue
		}
		if !waitRetry(ctx, idle, s.wake) {
			return
		}
	}
}
