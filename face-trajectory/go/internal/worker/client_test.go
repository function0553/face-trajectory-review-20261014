package worker

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"testing"
	"time"
)

// A real child process: cancellation happens after it has received the write.
func TestWorkerProcess(t *testing.T) {
	if os.Getenv("TRAJECTORY_TEST_CHILD") != "1" {
		return
	}
	s := bufio.NewScanner(os.Stdin)
	for s.Scan() {
		var r struct {
			Delay  int    `json:"delay_ms"`
			Marker string `json:"marker"`
		}
		if json.Unmarshal(s.Bytes(), &r) != nil {
			os.Exit(2)
		}
		if r.Marker != "" {
			_ = os.WriteFile(r.Marker, []byte("started"), 0600)
		}
		time.Sleep(time.Duration(r.Delay) * time.Millisecond)
		fmt.Println(`{"ok":true}`)
	}
	os.Exit(0)
}
func testClient(t *testing.T) *Client {
	t.Helper()
	t.Setenv("TRAJECTORY_TEST_CHILD", "1")
	c, err := start(os.Args[0], "", 4, []string{"-test.run=^TestWorkerProcess$"})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = c.Close() })
	return c
}
func waitMarker(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for {
		if _, err := os.Stat(path); err == nil {
			return
		}
		if time.Now().After(deadline) {
			t.Fatal("child did not receive transaction")
		}
		time.Sleep(time.Millisecond)
	}
}
func TestCancellationAfterWriteReturnsCommittedReply(t *testing.T) {
	c := testClient(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	marker := t.TempDir() + "/started"
	b, _ := json.Marshal(map[string]any{"delay_ms": 150, "marker": marker})
	done := make(chan transactionResult, 1)
	go func() { r, e := c.Request(ctx, b); done <- transactionResult{r, e} }()
	waitMarker(t, marker)
	cancel()
	r := <-done
	if r.err != nil || string(r.response) != `{"ok":true}` {
		t.Fatalf("transaction reply lost after cancellation: %s %v", r.response, r.err)
	}
	if _, err := c.Request(context.Background(), json.RawMessage(`{}`)); err != nil {
		t.Fatal(err)
	}
}
func TestCancelledQueueDoesNotWrite(t *testing.T) {
	c := testClient(t)
	<-c.gate
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := c.Request(ctx, json.RawMessage(`{}`))
	c.gate <- struct{}{}
	if err != context.Canceled {
		t.Fatalf("got %v", err)
	}
}
func TestStopReapsBeforeRestart(t *testing.T) {
	c := testClient(t)
	<-c.gate
	old := c.cmd
	c.stopLocked(fmt.Errorf("test stop"))
	if old.ProcessState == nil || c.cmd != nil {
		c.gate <- struct{}{}
		t.Fatal("old worker was not synchronously reaped")
	}
	if err := c.restartLocked(); err != nil {
		c.gate <- struct{}{}
		t.Fatal(err)
	}
	c.gate <- struct{}{}
	if _, err := c.Request(context.Background(), json.RawMessage(`{}`)); err != nil {
		t.Fatal(err)
	}
}

func TestColdRecoveryCanExceedTwentySeconds(t *testing.T) {
	c := testClient(t)
	c.starting = true
	begin := time.Now()
	r, err := c.Request(context.Background(), json.RawMessage(`{"delay_ms":21000}`))
	if err != nil || string(r) != `{"ok":true}` {
		t.Fatalf("cold recovery hard truncated: %s %v", r, err)
	}
	if time.Since(begin) < 20*time.Second {
		t.Fatal("test did not exceed normal timeout")
	}
}
func TestFairCancellationAfterWriteReturnsCommittedReply(t *testing.T) {
	c := testClient(t)
	f := NewFair(c, 8, 4)
	defer f.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	marker := t.TempDir() + "/started"
	b, _ := json.Marshal(map[string]any{"delay_ms": 150, "marker": marker})
	done := make(chan transactionResult, 1)
	go func() { r, e := f.Request(ctx, b); done <- transactionResult{r, e} }()
	waitMarker(t, marker)
	cancel()
	r := <-done
	if r.err != nil || string(r.response) != `{"ok":true}` {
		t.Fatalf("fair dispatcher lost reply: %s %v", r.response, r.err)
	}
}
func TestWorkerLifetimeParent(t *testing.T) {
	if os.Getenv("TRAJECTORY_TEST_PARENT") != "1" {
		return
	}
	c, err := start(os.Args[0], "", 4, []string{"-test.run=^TestWorkerProcess$"})
	if err != nil {
		os.Exit(3)
	}
	marker := os.Getenv("TRAJECTORY_TEST_READY")
	b, _ := json.Marshal(map[string]any{"delay_ms": 60000, "marker": marker + ".child"})
	go c.Request(context.Background(), b)
	waitMarker(t, marker+".child")
	if os.WriteFile(marker, []byte(strconv.Itoa(c.cmd.Process.Pid)), 0600) != nil {
		os.Exit(4)
	}
	for {
		time.Sleep(time.Hour)
	}
}
func TestParentKillTerminatesWorker(t *testing.T) {
	ready := t.TempDir() + "/ready"
	parent := exec.Command(os.Args[0], "-test.run=^TestWorkerLifetimeParent$")
	parent.Env = append(os.Environ(), "TRAJECTORY_TEST_PARENT=1", "TRAJECTORY_TEST_CHILD=1", "TRAJECTORY_TEST_READY="+ready)
	if err := parent.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = parent.Process.Kill(); _ = parent.Wait() }()
	waitMarker(t, ready)
	raw, _ := os.ReadFile(ready)
	pid, _ := strconv.Atoi(string(raw))
	defer func() {
		if processRunning(pid) {
			p, _ := os.FindProcess(pid)
			_ = p.Kill()
		}
	}()
	if err := parent.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for processRunning(pid) {
		if time.Now().After(deadline) {
			t.Fatalf("orphan worker %d survived daemon kill", pid)
		}
		time.Sleep(10 * time.Millisecond)
	}
}
