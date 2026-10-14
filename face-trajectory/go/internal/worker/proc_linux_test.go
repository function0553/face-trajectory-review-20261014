package worker

import (
	"fmt"
	"os"
	"strings"
	"syscall"
)

func processRunning(pid int) bool {
	if syscall.Kill(pid, 0) != nil {
		return false
	}
	// A killed orphan may remain a zombie until container init reaps it.
	raw, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return false
	}
	i := strings.LastIndex(string(raw), ") ")
	return i < 0 || raw[i+2] != 'Z'
}
