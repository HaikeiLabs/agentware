// Package livecapture lets the Go fixture packages fill in the expected
// values of candidate fixtures captured from live models
// (scripts/live-captures, see docs/testing.md). Go is the reference
// implementation, so its parse of a captured response becomes the expected
// value the other languages are then held to. It is a no-op unless
// AGENTWARE_LIVE_CAPTURE_DIR is set.
package livecapture

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// DirEnv names the directory holding the candidate fixtures, laid out like
// fixtures/: <dir>/toolformat/*-cases.json and <dir>/reasoning/*-cases.json.
const DirEnv = "AGENTWARE_LIVE_CAPTURE_DIR"

// Files returns the candidate fixture files of family (a fixtures/
// subdirectory such as "toolformat"). It skips t when DirEnv is unset.
func Files(t *testing.T, family string) []string {
	t.Helper()
	dir := os.Getenv(DirEnv)
	if dir == "" {
		t.Skip(DirEnv + " is not set; nothing to fill")
	}
	files, err := filepath.Glob(filepath.Join(dir, family, "*-cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	return files
}

// Read decodes the candidate fixture at path into a generic object, so
// fields the filler does not touch are written back unchanged.
func Read(t *testing.T, path string) map[string]any {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var fx map[string]any
	if err := json.Unmarshal(data, &fx); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
	return fx
}

// Write encodes fx back to path, indented and without HTML escaping so the
// captured model output stays readable in review.
func Write(t *testing.T, path string, fx map[string]any) {
	t.Helper()
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(fx); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
	if err := os.WriteFile(path, buf.Bytes(), 0o644); err != nil {
		t.Fatal(err)
	}
}

// Cases returns the entries of a fixture section, or nil when the section is
// missing or not a list.
func Cases(fx map[string]any, section string) []map[string]any {
	list, _ := fx[section].([]any)
	cases := make([]map[string]any, 0, len(list))
	for _, item := range list {
		if c, ok := item.(map[string]any); ok {
			cases = append(cases, c)
		}
	}
	return cases
}
