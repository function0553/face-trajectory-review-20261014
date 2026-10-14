//go:build !linux && !windows

package worker

func processRunning(pid int) bool { return false }
