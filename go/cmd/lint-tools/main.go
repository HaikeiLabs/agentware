package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/soypete/pedro-agentware/go/tools"
)

func main() {
	if len(os.Args) != 2 {
		emit([]string{"usage: go run ./cmd/lint-tools MANIFEST.json"})
		os.Exit(1)
	}
	raw, err := os.ReadFile(os.Args[1])
	if err != nil {
		emit([]string{err.Error()})
		os.Exit(1)
	}
	errors := tools.LintKeiToolManifest(raw, "")
	emit(errors)
	if len(errors) > 0 {
		os.Exit(1)
	}
}
func emit(errors []string) {
	if errors == nil {
		errors = []string{}
	}
	out, _ := json.Marshal(map[string]any{"valid": len(errors) == 0, "errors": errors})
	fmt.Println(string(out))
}
