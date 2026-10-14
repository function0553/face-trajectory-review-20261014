package worker

import (
	"os/exec"
	"runtime"
	"sync"
	"syscall"
)

var processThreads sync.Map

func configureProcess(cmd *exec.Cmd) (func() error, error) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Pdeathsig: syscall.SIGKILL}
	release := make(chan struct{})
	processThreads.Store(cmd, release)
	return func() error { processThreads.Delete(cmd); close(release); return nil }, nil
}
func startProcess(cmd *exec.Cmd) error {
	value, _ := processThreads.Load(cmd)
	release := value.(chan struct{})
	started := make(chan error, 1)
	go func() {
		// Linux Pdeathsig follows the creating OS thread, not just the PID.
		// Keep that thread alive until the child is reaped, so Go scheduler
		// thread retirement cannot kill a healthy worker.
		runtime.LockOSThread()
		defer runtime.UnlockOSThread()
		err := cmd.Start()
		started <- err
		if err == nil {
			<-release
		}
	}()
	return <-started
}
