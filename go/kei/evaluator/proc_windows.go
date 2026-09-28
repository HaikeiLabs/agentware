//go:build windows

package evaluator

import (
	"errors"
	"io/fs"
	"os"
	"os/exec"
)

// startInNewGroup keeps the default: exec kills the direct child on timeout.
func startInNewGroup(*exec.Cmd) {}

// openNoFollow is unsupported on Windows, so a pinned executable always denies.
func openNoFollow(string) (*os.File, error) {
	return nil, errors.New("O_NOFOLLOW is unavailable")
}

func isExecutable(fi fs.FileInfo) bool { return fi.Mode().IsRegular() }
