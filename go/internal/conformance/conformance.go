// Package conformance records which shared fixture cases a Go test ran, for
// the equal-coverage gate in scripts/conformance/gate.py (see
// docs/testing.md). It is a no-op unless AGENTWARE_CONFORMANCE_LOG is set.
package conformance

import (
	"encoding/json"
	"os"
	"testing"
)

// LogEnv names the JSONL file that recorded cases are appended to.
const LogEnv = "AGENTWARE_CONFORMANCE_LOG"

// Record notes that t ran the case named name in the section of fixture
// (a path relative to fixtures/, e.g. "toolformat/glm-cases.json"). The case
// is written when t finishes, and only if t passed.
func Record(t testing.TB, fixture, section, name string) {
	t.Helper()
	path := os.Getenv(LogEnv)
	if path == "" {
		return
	}
	t.Cleanup(func() {
		if t.Failed() {
			return
		}
		line, err := json.Marshal(map[string]string{
			"language": "go", "fixture": fixture, "section": section, "case": name,
		})
		if err != nil {
			t.Errorf("conformance: %v", err)
			return
		}
		f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
		if err != nil {
			t.Errorf("conformance: %v", err)
			return
		}
		_, err = f.Write(append(line, '\n'))
		if closeErr := f.Close(); err == nil {
			err = closeErr
		}
		if err != nil {
			t.Errorf("conformance: %v", err)
		}
	})
}
