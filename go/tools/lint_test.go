package tools

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestLintKeiToolManifestSharedFixture(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "fixtures", "kei", "tool-manifest.v2.json"))
	if err != nil {
		t.Fatal(err)
	}
	caps := filepath.Join("..", "..", "fixtures", "kei", "connector-capabilities.v0.5.0.json")
	if errors := LintKeiToolManifest(raw, caps); len(errors) != 0 {
		t.Fatalf("unexpected lint errors: %v", errors)
	}
}

func TestLintRejectsRetiredApprovalAndInvalidCapability(t *testing.T) {
	manifest := map[string]any{"schema": "kei.tool-manifest/v2", "tools": []any{map[string]any{
		"name": "bad", "source": "github", "required_capabilities": []string{"admin.raw_sql"},
		"resource_types": []any{map[string]any{"type": "repo:org/*"}}, "operation_class": "read", "allow": true,
	}}}
	raw, _ := json.Marshal(manifest)
	errors := LintKeiToolManifest(raw, filepath.Join("..", "..", "fixtures", "kei", "connector-capabilities.v0.5.0.json"))
	if len(errors) < 3 {
		t.Fatalf("expected capability, resource, and allow errors; got %v", errors)
	}
}
