//go:build !linux && !windows

package worker

import "os/exec"

// Parent-death protection is supported on the deployment platforms Linux/Windows.
func configureProcess(cmd *exec.Cmd) (func() error, error) {
	return func() error { return nil }, nil
}
func startProcess(cmd *exec.Cmd) error { return cmd.Start() }
