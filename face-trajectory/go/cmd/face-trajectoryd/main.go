package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"face-trajectory/internal/api"
	"face-trajectory/internal/push"
	"face-trajectory/internal/retention"
	"face-trajectory/internal/trust"
	"face-trajectory/internal/worker"
)

type retentionPaths []string

func (p *retentionPaths) String() string { return strings.Join(*p, ";") }
func (p *retentionPaths) Set(value string) error {
	if !filepath.IsAbs(value) || strings.HasPrefix(value, `\\`) {
		return fmt.Errorf("p1-retention-db requires an absolute local database path")
	}
	resolved, err := filepath.EvalSymlinks(value)
	if err != nil {
		return fmt.Errorf("p1-retention-db must exist and resolve to a local file")
	}
	if strings.HasPrefix(resolved, `\\`) {
		return fmt.Errorf("p1-retention-db cannot resolve to a network share")
	}
	info, err := os.Stat(resolved)
	if err != nil || !info.Mode().IsRegular() {
		return fmt.Errorf("p1-retention-db must be a regular existing file")
	}
	for _, existing := range *p {
		if strings.EqualFold(existing, resolved) {
			return nil
		}
	}
	*p = append(*p, resolved)
	return nil
}

// loadSharedSecret reads the shared backend secret from a file or, when no file
// is configured, from FACE_TRAJECTORY_AUTH_SHARED_SECRET. The value is never
// accepted as a command line flag: argv is visible to every local user through
// ps and /proc/<pid>/cmdline.
func loadSharedSecret(path string) (string, error) {
	if path == "" {
		return strings.TrimSpace(os.Getenv("FACE_TRAJECTORY_AUTH_SHARED_SECRET")), nil
	}
	info, err := os.Stat(path)
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("auth-shared-secret-file must be a regular file")
	}
	if info.Mode().Perm()&0o077 != 0 {
		return "", fmt.Errorf("auth-shared-secret-file must not be readable by group or others; chmod 600")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	secret := strings.TrimSpace(string(raw))
	if secret == "" {
		return "", fmt.Errorf("auth-shared-secret-file is empty")
	}
	return secret, nil
}

func main() {
	workerPath := flag.String("worker", "", "path to face_trajectory_worker executable")
	address := flag.String("addr", "127.0.0.1:8081", "local listen address")
	stateFile := flag.String("state-file", "state/face-trajectory.sqlite", "SQLite state file; empty selects ephemeral mode")
	maxPending := flag.Int("max-pending", 128, "maximum active and queued requests")
	maxStateMB := flag.Int("max-state-mb", 8192, "SQLite database page limit in MiB")
	maxTemplates := flag.Int("max-templates", 500000, "maximum enrolled and automatic templates combined")
	pushURL := flag.String("push-url", "", "backend HTTP(S) result receiver; enables durable result push")
	queueResults := flag.Bool("push-results", false, "queue results even when receiver URL is not available")
	authKeysPath := flag.String("auth-public-key-file", "", "backend Ed25519 public key JSON; empty fails closed")
	authIssuer := flag.String("auth-issuer", "", "trusted backend identity issuer")
	authSecretFile := flag.String("auth-shared-secret-file", "", "file holding the shared backend secret; falls back to FACE_TRAJECTORY_AUTH_SHARED_SECRET")
	clockKeysPath := flag.String("clock-public-key-file", "", "verified UTC authority Ed25519 public key JSON")
	clockIssuer := flag.String("clock-issuer", "", "trusted verified UTC authority issuer")
	clockProofPath := flag.String("clock-proof-file", "", "challenge-bound signed UTC proof; authority refreshes atomically")
	retentionInterval := flag.Duration("retention-interval", time.Minute, "periodic C++ retention dry-run or cleanup interval")
	retentionEnabled := flag.Bool("retention-enabled", false, "explicitly permit deletion in configured local databases")
	var p1Paths retentionPaths
	flag.Var(&p1Paths, "p1-retention-db", "absolute existing local P1 SQLite database; repeat for delivery and legacy bridge")
	flag.Parse()
	if flag.NArg() != 0 {
		log.Fatal("unexpected positional arguments; boolean flags use -push-results or -push-results=true")
	}
	pushEnabled := *queueResults || *pushURL != ""
	if *retentionInterval < time.Second || *retentionInterval > 24*time.Hour {
		log.Fatal("retention interval must be between one second and 24 hours")
	}
	sharedSecret, secretErr := loadSharedSecret(*authSecretFile)
	if secretErr != nil {
		log.Fatal(secretErr)
	}
	if sharedSecret != "" && (*authKeysPath != "" || *authIssuer != "") {
		log.Fatal("shared secret authorization and signed backend identity are mutually exclusive")
	}
	if sharedSecret == "" {
		if (*authKeysPath == "") != (*authIssuer == "") {
			log.Fatal("authorization requires both key file and issuer, or a shared secret")
		}
		if *authKeysPath == "" {
			log.Fatal("authorization requires a shared secret or a backend public key file; empty fails closed")
		}
	}
	if (*clockKeysPath == "") != (*clockIssuer == "") || (*clockKeysPath == "") != (*clockProofPath == "") {
		log.Fatal("trusted clock requires key file, issuer and proof file")
	}
	var authKeys, clockKeys trust.Keys
	var err error
	if *authKeysPath != "" {
		authKeys, err = trust.LoadKeys(*authKeysPath)
		if err != nil {
			log.Fatal(err)
		}
	}
	if *clockKeysPath != "" {
		clockKeys, err = trust.LoadKeys(*clockKeysPath)
		if err != nil {
			log.Fatal(err)
		}
	}
	verifier := &trust.Verifier{Keys: authKeys, Issuer: *authIssuer, SharedSecret: sharedSecret}
	clock, err := trust.NewClock(clockKeys, *clockIssuer, *clockProofPath)
	if err != nil {
		log.Fatal(err)
	}
	// Validate configuration before starting a worker or opening state.
	var excludedPushActivities []string
	if value := os.Getenv("FACE_TRAJECTORY_PUSH_EXCLUDED_ACTIVITIES"); value != "" {
		if err := json.Unmarshal([]byte(value), &excludedPushActivities); err != nil {
			log.Fatal("FACE_TRAJECTORY_PUSH_EXCLUDED_ACTIVITIES must be a JSON array of activity IDs")
		}
	}
	sender, err := push.New(nil, *pushURL, os.Getenv("FACE_TRAJECTORY_PUSH_AUTHORIZATION"), clock)
	if err != nil {
		log.Fatal(err)
	}
	if err := sender.ExcludeActivities(excludedPushActivities); err != nil {
		log.Fatal(err)
	}
	client, err := worker.StartConfiguredRetention(*workerPath, *stateFile, *maxPending, *maxStateMB, *maxTemplates, pushEnabled, *retentionEnabled, p1Paths)
	if err != nil {
		log.Fatal(err)
	}
	defer client.Close()
	clocked := &trust.Clocked{Worker: client, Clock: clock}
	fair := worker.NewFair(clocked, *maxPending, 32)
	defer fair.Close()
	sender, _ = push.New(fair, *pushURL, os.Getenv("FACE_TRAJECTORY_PUSH_AUTHORIZATION"), clock)
	if err := sender.ExcludeActivities(excludedPushActivities); err != nil {
		log.Fatal(err)
	}
	retentionManager, err := retention.New(fair, *retentionInterval, *retentionEnabled)
	if err != nil {
		log.Fatal(err)
	}
	server := algorithmHTTPServer(*address, api.NewAuthorized(sender, verifier, clock, retentionManager))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	pushDone := make(chan struct{})
	go func() { defer close(pushDone); sender.Run(ctx) }()
	retentionDone := make(chan struct{})
	go func() { defer close(retentionDone); retentionManager.Loop(ctx) }()
	// The business-authorized 72h personnel lifetime is independent of optional
	// historical evidence retention. A trusted clock is still required.
	go func() {
		ticker := time.NewTicker(time.Minute)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				call, cancel := context.WithTimeout(ctx, 20*time.Second)
				_, err := fair.Request(call, json.RawMessage(`{"op":"platform_v4_expire"}`))
				cancel()
				if err != nil && ctx.Err() == nil {
					log.Print("personnel expiry sweep unavailable; retrying on next interval")
				}
			}
		}
	}()
	shutdownDone := make(chan struct{})
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownCtx)
		close(shutdownDone)
	}()
	log.Printf("face trajectory algorithm service listening on %s", *address)
	err = server.ListenAndServe()
	if ctx.Err() != nil {
		<-shutdownDone
	}
	if err != nil && err != http.ErrServerClosed {
		log.Print(err)
	}
	stop()
	<-pushDone
	<-retentionDone
}

func algorithmHTTPServer(address string, handler http.Handler) *http.Server {
	return &http.Server{
		Addr: address, Handler: handler,
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second,
		// Allow a 20s admission queue, 5m first restoration and bounded reaping.
		// Ordinary execution still has the worker's independent 20s timer.
		WriteTimeout: 6 * time.Minute,
	}
}
