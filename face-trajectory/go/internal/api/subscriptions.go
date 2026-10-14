package api

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"face-trajectory/internal/trust"
	"net/http"
	"sync"
)

// Subscriptions are user access state. Losing them on service restart requires
// reopening a subscription and never changes durable algorithm monitoring.
type subscription struct {
	mu         sync.Mutex
	id         string
	generation int64
	context    trust.ClientContext
	activity   string
	versions   map[string]json.RawMessage
	cursor     uint64
	expires    int64
	closed     bool
}

type subscriptions struct {
	mu             sync.Mutex
	items          map[string]*subscription
	nextGeneration int64
}

func newSubscriptions() *subscriptions { return &subscriptions{items: make(map[string]*subscription)} }

func versions(body map[string]json.RawMessage) (map[string]json.RawMessage, error) {
	result := make(map[string]json.RawMessage)
	for _, field := range []string{"config_version", "binding_version", "monitoring_version"} {
		var value *uint64
		if json.Unmarshal(body[field], &value) != nil || value == nil {
			return nil, errors.New("explicit integer activity versions are required")
		}
		result[field], _ = json.Marshal(*value)
	}
	return result, nil
}

func (s *Server) subscription(operation string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		body, err := readBody(w, r)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error(), nil)
			return
		}
		claims, stamp, ok := s.authorize(w, r)
		if !ok {
			return
		}
		if operation == "open" {
			s.openSubscription(w, r, body, claims, stamp)
			return
		}
		var id string
		var generation int64
		if json.Unmarshal(body["subscription_id"], &id) != nil || id == "" || json.Unmarshal(body["generation"], &generation) != nil || generation < 1 {
			writeError(w, http.StatusBadRequest, "subscription_id and generation are required", &claims)
			return
		}
		s.subscriptions.mu.Lock()
		sub := s.subscriptions.items[id]
		s.subscriptions.mu.Unlock()
		if sub == nil {
			writeError(w, http.StatusGone, "subscription no longer exists; reopen with the current activity context", &claims)
			return
		}
		sub.mu.Lock()
		defer sub.mu.Unlock()
		if sub.closed || stamp.Now+stamp.Uncertainty >= sub.expires {
			writeError(w, http.StatusGone, "subscription has ended; reopen with a fresh authorization context", &claims)
			return
		}
		if sub.generation != generation || sub.context != claims.ClientContext {
			writeError(w, http.StatusForbidden, "subscription belongs to a different identity or activity selection context", &claims)
			return
		}
		if !claims.Allows(sub.activity, "subscribe") {
			writeError(w, http.StatusForbidden, "backend identity does not authorize this activity subscription", &claims)
			return
		}
		if supplied := activity(body); supplied != "" && supplied != sub.activity {
			writeError(w, http.StatusForbidden, "subscription activity cannot be replaced", &claims)
			return
		}
		if operation == "close" {
			sub.closed = true
			s.subscriptions.mu.Lock()
			delete(s.subscriptions.items, id)
			s.subscriptions.mu.Unlock()
			writeReply(w, http.StatusOK, subscriptionReply(sub, claims, map[string]json.RawMessage{"ok": json.RawMessage("true"), "closed": json.RawMessage("true")}))
			return
		}
		request := subscriptionRequest(sub)
		request["_authorization_validity"], _ = json.Marshal(map[string]int64{"not_before_unix_ms": claims.NotBefore, "expires_unix_ms": claims.Expires})
		request["after_sequence"], _ = json.Marshal(sub.cursor)
		request["limit"] = json.RawMessage("100")
		reply, status, err := s.exchange(r.Context(), request)
		if err != nil {
			writeError(w, status, err.Error(), &claims)
			return
		}
		if status == http.StatusOK {
			var events []map[string]json.RawMessage
			cursor := sub.cursor
			if json.Unmarshal(reply["events"], &events) != nil {
				writeError(w, http.StatusBadGateway, "invalid activity result stream", &claims)
				return
			}
			for _, event := range events {
				var sequence uint64
				if activity(event) != sub.activity || json.Unmarshal(event["sequence"], &sequence) != nil || sequence <= cursor {
					writeError(w, http.StatusBadGateway, "result stream violated activity scope or sequence", &claims)
					return
				}
				for key, value := range sub.versions {
					if string(event[key]) != string(value) {
						writeError(w, http.StatusConflict, "activity versions changed; reopen the subscription", &claims)
						return
					}
				}
				event["client_context"], _ = json.Marshal(claims.ClientContext)
				event["subscription_id"] = rawString(sub.id)
				event["subscription_generation"], _ = json.Marshal(sub.generation)
				cursor = sequence
			}
			sub.cursor = cursor
			reply["events"], _ = json.Marshal(events)
		}
		writeReply(w, status, subscriptionReply(sub, claims, reply))
	}
}

func subscriptionRequest(sub *subscription) map[string]json.RawMessage {
	request := map[string]json.RawMessage{"op": rawString("activity_results"), "activity_id": rawString(sub.activity)}
	request["_client_context"], _ = json.Marshal(sub.context)
	for key, value := range sub.versions {
		request[key] = value
	}
	return request
}

func subscriptionReply(sub *subscription, claims trust.Claims, reply map[string]json.RawMessage) map[string]json.RawMessage {
	reply["activity_id"] = rawString(sub.activity)
	reply["client_context"], _ = json.Marshal(claims.ClientContext)
	reply["subscription_id"] = rawString(sub.id)
	reply["generation"], _ = json.Marshal(sub.generation)
	reply["after_sequence"], _ = json.Marshal(sub.cursor)
	return reply
}

func (s *Server) openSubscription(w http.ResponseWriter, r *http.Request, body map[string]json.RawMessage, claims trust.Claims, stamp trust.Stamp) {
	id := activity(body)
	if id == "" {
		writeError(w, http.StatusBadRequest, "explicit activity_id is required", &claims)
		return
	}
	if !claims.Allows(id, "subscribe") {
		writeError(w, http.StatusForbidden, "backend identity does not authorize this activity subscription", &claims)
		return
	}
	selected, err := versions(body)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error(), &claims)
		return
	}
	buffer := make([]byte, 32)
	if _, err := rand.Read(buffer); err != nil {
		writeError(w, http.StatusServiceUnavailable, "cannot allocate subscription context", &claims)
		return
	}
	sub := &subscription{id: hex.EncodeToString(buffer), context: claims.ClientContext, activity: id, versions: selected, expires: claims.Expires}
	request := subscriptionRequest(sub)
	request["_authorization_validity"], _ = json.Marshal(map[string]int64{"not_before_unix_ms": claims.NotBefore, "expires_unix_ms": claims.Expires})
	request["after_sequence"] = json.RawMessage("0")
	request["limit"] = json.RawMessage("1")
	reply, status, err := s.exchange(r.Context(), request)
	if err != nil {
		writeError(w, status, err.Error(), &claims)
		return
	}
	if status != http.StatusOK {
		reply["client_context"], _ = json.Marshal(claims.ClientContext)
		writeReply(w, status, reply)
		return
	}
	if json.Unmarshal(reply["latest_sequence"], &sub.cursor) != nil {
		writeError(w, http.StatusBadGateway, "invalid activity result stream sequence", &claims)
		return
	}
	s.subscriptions.mu.Lock()
	for key, item := range s.subscriptions.items {
		if item.expires <= stamp.Now+stamp.Uncertainty {
			delete(s.subscriptions.items, key)
		}
	}
	count := 0
	for _, item := range s.subscriptions.items {
		if item.context.Subject == claims.Subject && item.context.Session == claims.Session {
			count++
		}
	}
	if len(s.subscriptions.items) >= 10000 || count >= 32 {
		s.subscriptions.mu.Unlock()
		writeError(w, http.StatusServiceUnavailable, "subscription capacity reached; close unused subscriptions", &claims)
		return
	}
	s.subscriptions.nextGeneration++
	sub.generation = s.subscriptions.nextGeneration
	s.subscriptions.items[sub.id] = sub
	s.subscriptions.mu.Unlock()
	delete(reply, "events")
	writeReply(w, http.StatusOK, subscriptionReply(sub, claims, reply))
}
