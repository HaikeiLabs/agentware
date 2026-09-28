//go:build !windows

package evaluator

import (
	"syscall"
	"time"
)

// waitDead reports whether pid is gone within the deadline.
func waitDead(pid int, within time.Duration) bool {
	deadline := time.Now().Add(within)
	for {
		if syscall.Kill(pid, 0) != nil {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(20 * time.Millisecond)
	}
}
