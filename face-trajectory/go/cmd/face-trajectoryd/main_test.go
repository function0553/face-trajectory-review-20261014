package main

import (
	"context"
	"encoding/json"
	"face-trajectory/internal/api"
	"face-trajectory/internal/worker"
	"io"
	"net"
	"net/http"
	"testing"
	"time"
)

type coldTransport struct{}

func (coldTransport) Request(context.Context, json.RawMessage) (json.RawMessage, error) {
	// First restoration is already admitted. Queue cancellation must not drop
	// its response, including through the real daemon HTTP write deadline.
	time.Sleep(31 * time.Second)
	return json.RawMessage(`{"ok":true}`), nil
}
func TestHTTPColdRecoveryExceedsThirtySeconds(t *testing.T) {
	fair := worker.NewFair(coldTransport{}, 8, 4)
	defer fair.Close()
	server := algorithmHTTPServer("127.0.0.1:0", api.New(fair))
	listener, err := net.Listen("tcp", server.Addr)
	if err != nil {
		t.Fatal(err)
	}
	go server.Serve(listener)
	defer server.Close()
	client := http.Client{Timeout: 40 * time.Second}
	response, err := client.Get("http://" + listener.Addr().String() + "/health")
	if err != nil {
		t.Fatalf("cold HTTP reply truncated: %v", err)
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(response.Body)
	if err != nil || response.StatusCode != 200 {
		t.Fatalf("status %d: %s %v", response.StatusCode, raw, err)
	}
	var reply struct {
		OK bool `json:"ok"`
	}
	if json.Unmarshal(raw, &reply) != nil || !reply.OK {
		t.Fatalf("missing committed reply: %s", raw)
	}
}
