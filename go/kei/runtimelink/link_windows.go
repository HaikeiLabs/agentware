//go:build windows

package runtimelink

import (
	"os/exec"
)

func setSysProcAttr(cmd *exec.Cmd) {}

func killProcessGroup(cmd *exec.Cmd) {
	if cmd.Process != nil {
		cmd.Process.Kill()
	}
}
