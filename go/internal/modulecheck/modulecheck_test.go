package modulecheck

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// ModulePath is the import path a developer passes to go get.
const ModulePath = "github.com/haikeilabs/agentware/go"

func moduleRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatalf("resolve module root: %v", err)
	}
	return root
}

func TestGoModDeclaresPublishedPath(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(moduleRoot(t), "go.mod"))
	if err != nil {
		t.Fatalf("read go.mod: %v", err)
	}
	var got string
	for _, line := range strings.Split(string(data), "\n") {
		if rest, ok := strings.CutPrefix(strings.TrimSpace(line), "module "); ok {
			got = strings.TrimSpace(rest)
			break
		}
	}
	if got != ModulePath {
		t.Fatalf("go.mod declares module %q, want %q", got, ModulePath)
	}
}

// TestExternalConsumerBuilds creates a module outside this one that requires
// the published path (replaced with this checkout) and builds a program that
// imports the main public packages. GOPROXY=off keeps it hermetic: every
// package must come from this checkout or the module cache, so an internal
// import still using a retired path fails instead of being fetched.
func TestExternalConsumerBuilds(t *testing.T) {
	if testing.Short() {
		t.Skip("builds a separate module; skipped in -short mode")
	}
	goBin, err := exec.LookPath("go")
	if err != nil {
		t.Skipf("go toolchain not on PATH: %v", err)
	}

	dir := t.TempDir()
	goMod := "module example.com/consumer\n\ngo 1.26\n\n" +
		"require " + ModulePath + " v0.0.0\n\n" +
		"replace " + ModulePath + " => " + moduleRoot(t) + "\n"
	mainGo := `package main

import (
	"fmt"

	"` + ModulePath + `/kei/evaluator"
	"` + ModulePath + `/middleware"
	"` + ModulePath + `/tools"
)

func main() {
	_ = evaluator.DefaultTimeout
	_ = tools.NewToolRegistry()
	fmt.Println(middleware.ActionAllow)
}
`
	writeFile(t, filepath.Join(dir, "go.mod"), goMod)
	writeFile(t, filepath.Join(dir, "main.go"), mainGo)

	for _, args := range [][]string{{"mod", "tidy"}, {"build", "./..."}} {
		cmd := exec.Command(goBin, args...)
		cmd.Dir = dir
		cmd.Env = append(os.Environ(), "GOFLAGS=-mod=mod", "GOWORK=off", "GOPROXY=off")
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("go %s in consumer module: %v\n%s", strings.Join(args, " "), err, out)
		}
	}

	list := exec.Command(goBin, "list", "-m", "all")
	list.Dir = dir
	list.Env = append(os.Environ(), "GOWORK=off", "GOPROXY=off")
	out, err := list.CombinedOutput()
	if err != nil {
		t.Fatalf("go list -m all in consumer module: %v\n%s", err, out)
	}
	for _, line := range strings.Split(string(out), "\n") {
		if strings.Contains(line, "pedro-agentware") {
			t.Fatalf("consumer build graph still references a retired module path: %s", line)
		}
	}
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}
