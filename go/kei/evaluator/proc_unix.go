//go:build !windows

package evaluator

import (
	"io/fs"
	"os"
	"os/exec"
	"syscall"
)

// startInNewGroup runs cmd in its own process group and makes context
// cancellation SIGKILL the whole group, so a grandchild holding stdout open
// cannot outlive a timeout.
func startInNewGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		if err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL); err != nil {
			return cmd.Process.Kill()
		}
		return nil
	}
}

// openNoFollow opens path read-only, refusing a symlink as the final component.
func openNoFollow(path string) (*os.File, error) {
	return os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
}

func isExecutable(fi fs.FileInfo) bool {
	return fi.Mode().IsRegular() && fi.Mode().Perm()&0o111 != 0
}
