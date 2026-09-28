//go:build windows

package evaluator

import "time"

// waitDead is unused on Windows: the subprocess cases are skipped there.
func waitDead(int, time.Duration) bool { return true }
