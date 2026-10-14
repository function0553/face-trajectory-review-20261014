package worker

import "golang.org/x/sys/windows"

func processRunning(pid int) bool {
	p, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return false
	}
	defer windows.CloseHandle(p)
	var code uint32
	return windows.GetExitCodeProcess(p, &code) == nil && code == 259
}
