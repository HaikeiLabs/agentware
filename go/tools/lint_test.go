package tools

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestLintKeiToolManifestSharedFixture(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "fixtures", "kei", "tool-manifest.v3.json"))
	if err != nil {
		t.Fatal(err)
	}
	caps := filepath.Join("..", "..", "fixtures", "kei", "connector-capabilities.v0.5.0.json")
	if errors := LintKeiToolManifest(raw, caps); len(errors) != 0 {
		t.Fatalf("unexpected lint errors: %v", errors)
	}
}

func TestLintKeiToolManifestRejectsUnsupportedSchema(t *testing.T) {
	if errors := LintKeiToolManifest([]byte(`{"schema":"unsupported","tools":[]}`), ""); len(errors) != 1 || errors[0] != "schema must be kei.tool-manifest/v3" {
		t.Fatalf("unexpected unsupported-schema diagnostics: %v", errors)
	}
}

func TestLintRejectsRetiredApprovalAndInvalidCapability(t *testing.T) {
	manifest := map[string]any{"schema": "kei.tool-manifest/v3", "tools": []any{map[string]any{
		"name": "bad", "source": "github", "required_capabilities": []string{"admin.raw_sql"},
		"resource_types": []any{map[string]any{"type": "repo:org/*"}}, "operation_class": "read", "allow": true,
		"route": map[string]any{"connector_binding": map[string]any{"agent_id": "agent", "connector_id": "binding"}},
	}}}
	raw, _ := json.Marshal(manifest)
	errors := LintKeiToolManifest(raw, filepath.Join("..", "..", "fixtures", "kei", "connector-capabilities.v0.5.0.json"))
	if len(errors) < 3 {
		t.Fatalf("expected capability, resource, and allow errors; got %v", errors)
	}
}
