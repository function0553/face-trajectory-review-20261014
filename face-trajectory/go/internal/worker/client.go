package worker

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sync"
	"time"
)

const requestTimeout = 20 * time.Second

var ErrBusy = errors.New("C++ worker queue is full")

// Client keeps one C++ algorithm process alive. The gate belongs to a complete
// worker transaction, even after its caller stops waiting for the response.
type Client struct {
	gate               chan struct{}
	closing            chan struct{}
	closeOnce          sync.Once
	closeErr           error
	cmd                *exec.Cmd
	stdin              io.WriteCloser
	stdout             *bufio.Reader
	processCleanup     func() error // platform lifetime guard; held until Wait completes
	reapDone           chan error
	transactionTimeout time.Duration // set before first request
	closed             bool          // guarded by gate
	failure            error         // guarded by gate
	pending            chan struct{}
	path               string
	args               []string
	recoverState       bool
	starting           bool // guarded by gate; gallery restoration can exceed one HTTP request
}

func Start(path string) (*Client, error) {
	return start(path, "", 128, nil)
}
func StartWithState(path, stateFile string, maxPending int) (*Client, error) {
	return start(path, stateFile, maxPending, nil)
}
func StartConfigured(path, stateFile string, maxPending, maxStateMB, maxTemplates int) (*Client, error) {
	return StartConfiguredPush(path, stateFile, maxPending, maxStateMB, maxTemplates, false)
}
func StartConfiguredPush(path, stateFile string, maxPending, maxStateMB, maxTemplates int, pushResults bool) (*Client, error) {
	return StartConfiguredRetention(path, stateFile, maxPending, maxStateMB, maxTemplates, pushResults, false, nil)
}

func StartConfiguredRetention(path, stateFile string, maxPending, maxStateMB, maxTemplates int, pushResults, retentionEnabled bool, p1Paths []string) (*Client, error) {
	if maxStateMB < 1 || maxTemplates < 1 {
		return nil, errors.New("positive state and gallery capacity required")
	}
	if pushResults && stateFile == "" {
		return nil, errors.New("result push requires durable state-file")
	}
	options := []string{"--max-state-mb", fmt.Sprint(maxStateMB), "--max-templates", fmt.Sprint(maxTemplates), "--push-results", fmt.Sprint(pushResults), "--retention-enabled", fmt.Sprint(retentionEnabled)}
	for _, path := range p1Paths {
		options = append(options, "--p1-retention-db", path)
	}
	return start(path, stateFile, maxPending, options)
}
func start(path, stateFile string, maxPending int, extraArgs []string) (*Client, error) {
	if path == "" {
		return nil, errors.New("C++ worker path is required")
	}
	if maxPending < 1 {
		return nil, errors.New("max pending must be positive")
	}
	args := []string{}
	if stateFile != "" {
		args = []string{"--state-file", stateFile}
	}
	args = append(args, extraArgs...)
	cmd := exec.Command(path, args...)
	cleanup, err := configureProcess(cmd)
	if err != nil {
		return nil, err
	}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		_ = cleanup()
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		_ = stdin.Close()
		_ = cleanup()
		return nil, err
	}
	cmd.Stderr = os.Stderr
	if err := startProcess(cmd); err != nil {
		_ = stdin.Close()
		_ = cleanup()
		return nil, err
	}
	client := &Client{cmd: cmd, stdin: stdin, stdout: bufio.NewReader(stdout), gate: make(chan struct{}, 1), closing: make(chan struct{}), transactionTimeout: requestTimeout}
	client.processCleanup = cleanup
	client.pending = make(chan struct{}, maxPending)
	client.path = path
	client.args = args
	client.recoverState = stateFile != ""
	client.starting = client.recoverState
	client.gate <- struct{}{}
	return client, nil
}

func (c *Client) Request(ctx context.Context, request json.RawMessage) (json.RawMessage, error) {
	select {
	case c.pending <- struct{}{}:
		defer func() { <-c.pending }()
	default:
		return nil, ErrBusy
	}
	ctx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-c.closing:
		return nil, errors.New("C++ worker is closing")
	case <-c.gate:
	}
	select {
	case <-c.closing:
		c.gate <- struct{}{}
		return nil, errors.New("C++ worker is closing")
	default:
	}
	if c.closed {
		if c.recoverState {
			if err := c.restartLocked(); err != nil {
				c.gate <- struct{}{}
				return nil, err
			}
		} else {
			c.gate <- struct{}{}
			return nil, c.failure
		}
	}
	if !json.Valid(request) || bytes.ContainsAny(request, "\r\n") {
		c.gate <- struct{}{}
		return nil, errors.New("request must be one JSON line")
	}
	if err := ctx.Err(); err != nil {
		c.gate <- struct{}{}
		return nil, err
	}
	done := make(chan transactionResult, 1)
	go c.transact(append([]byte(nil), request...), done)
	// Admission cancellation stops at this boundary. Once submitted, wait for
	// the worker's bounded transaction and preserve its committed receipt.
	reply := <-done
	return reply.response, reply.err
}

// Only the next request restarts a durable worker. The interrupted mutation is
// never silently replayed; producers retry using the same observation ID.
func (c *Client) restartLocked() error {
	if c.cmd != nil {
		if err := c.finishReapLocked(); err != nil {
			return err
		}
	}
	cmd := exec.Command(c.path, c.args...)
	cleanup, err := configureProcess(cmd)
	if err != nil {
		return err
	}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		_ = cleanup()
		return err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		_ = stdin.Close()
		_ = cleanup()
		return err
	}
	cmd.Stderr = os.Stderr
	if err = startProcess(cmd); err != nil {
		_ = stdin.Close()
		_ = cleanup()
		return err
	}
	c.cmd = cmd
	c.processCleanup = cleanup
	c.stdin = stdin
	c.stdout = bufio.NewReader(stdout)
	c.closed = false
	c.failure = nil
	c.starting = true
	return nil
}

type transactionResult struct {
	response json.RawMessage
	err      error
}

func (c *Client) transact(request []byte, done chan<- transactionResult) {
	defer func() { c.gate <- struct{}{} }()
	ioDone := make(chan transactionResult, 1)
	go func() {
		if _, err := c.stdin.Write(append(request, '\n')); err != nil {
			ioDone <- transactionResult{err: fmt.Errorf("write to C++ worker: %w", err)}
			return
		}
		response, err := c.stdout.ReadBytes('\n')
		if err != nil {
			ioDone <- transactionResult{err: fmt.Errorf("read from C++ worker: %w", err)}
			return
		}
		response = bytes.TrimSpace(response)
		if !json.Valid(response) {
			ioDone <- transactionResult{err: errors.New("C++ worker returned invalid JSON")}
			return
		}
		ioDone <- transactionResult{response: response}
	}()
	// An abandoned transaction remains bounded. Reaching the bound loses the
	// single worker's in-memory state; this client does not silently restart it.
	timeout := c.transactionTimeout
	if c.starting && timeout < 5*time.Minute {
		timeout = 5 * time.Minute
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	var reply transactionResult
	select {
	case reply = <-ioDone:
		c.starting = false
		if reply.err != nil {
			c.stopLocked(reply.err)
		}
	case <-timer.C:
		reply.err = fmt.Errorf("C++ worker transaction exceeded %s", timeout)
		c.stopLocked(reply.err)
		<-ioDone // killed process and closed stdin unblock the stream operation
	case <-c.closing:
		reply.err = errors.New("C++ worker is closing")
		c.stopLocked(reply.err)
		<-ioDone
	}
	done <- reply
}

// stopLocked is called only by the transaction holding the gate.
func (c *Client) stopLocked(err error) {
	c.closed = true
	c.failure = err
	if c.cmd == nil {
		return
	}
	_ = c.stdin.Close()
	_ = c.cmd.Process.Kill()
	// Wait runs exactly once. If reaping exceeds the short bound, retain cmd
	// and fail closed: a replacement must never race the old state lock.
	cmd := c.cmd
	c.reapDone = make(chan error, 1)
	wait := c.reapDone
	go func() { wait <- cmd.Wait() }()
	_ = c.finishReapLocked()
}

func (c *Client) finishReapLocked() error {
	select {
	case <-c.reapDone:
	case <-time.After(3 * time.Second):
		return errors.New("old C++ worker has not exited; restart is blocked")
	}
	c.cmd = nil
	c.reapDone = nil
	if c.processCleanup != nil {
		_ = c.processCleanup()
		c.processCleanup = nil
	}
	return nil
}

func (c *Client) Close() error {
	c.closeOnce.Do(func() {
		close(c.closing)
		<-c.gate
		c.closed = true
		if c.failure == nil {
			c.failure = errors.New("C++ worker is closed")
		}
		if c.cmd != nil {
			if c.reapDone == nil {
				c.stopLocked(c.failure)
			}
			if c.cmd != nil {
				c.closeErr = c.finishReapLocked()
			}
		}
		c.gate <- struct{}{}
	})
	return c.closeErr
}
