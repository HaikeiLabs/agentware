package tools

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// governedTool implements both Tool and GovernedTool for testing.
type governedTool struct {
	name        string
	description string
	scope       KeiScope
}

func (t *governedTool) Name() string        { return t.name }
func (t *governedTool) Description() string { return t.description }
func (t *governedTool) Execute(_ context.Context, _ map[string]any) (*Result, error) {
	return &Result{Success: true}, nil
}
func (t *governedTool) KeiScope() KeiScope { return t.scope }

// plainTool implements only Tool (not GovernedTool) for testing exclusion.
type plainTool struct {
	name        string
	description string
}

func (t *plainTool) Name() string        { return t.name }
func (t *plainTool) Description() string { return t.description }
func (t *plainTool) Execute(_ context.Context, _ map[string]any) (*Result, error) {
	return &Result{Success: true}, nil
}

func TestExportKeiToolManifest_FixtureMatch(t *testing.T) {
	registry := NewToolRegistry()

	// Register governed tools (sorted by name in the registry)
	registry.Register(&governedTool{
		name:        "github.get_issue",
		description: "Fetch an issue from a GitHub repository",
		scope: KeiScope{
			Source: "github", RequiredCapabilities: []string{"issue.read"}, ResourceTypes: []KeiResourceType{{Type: "issue", ParentType: "repository"}}, OperationClass: "read", Service: "github",
		},
	})
	registry.Register(&governedTool{
		name:        "github.list_issues",
		description: "List issues in a GitHub repository",
		scope: KeiScope{
			Source: "github", RequiredCapabilities: []string{"issue.read"}, ResourceTypes: []KeiResourceType{{Type: "issue", ParentType: "repository"}}, OperationClass: "read", Service: "github",
		},
	})
	registry.Register(&governedTool{
		name:        "linear.get_issue",
		description: "Fetch an issue from Linear",
		scope: KeiScope{
			Source: "linear", RequiredCapabilities: []string{"issue.read"}, ResourceTypes: []KeiResourceType{{Type: "issue", ParentType: "team"}}, OperationClass: "read", Service: "linear",
		},
	})

	// Register a plain tool that should NOT appear in the manifest
	registry.Register(&plainTool{
		name:        "local_echo",
		description: "Echo input back",
	})

	got, err := registry.ExportKeiToolManifest()
	if err != nil {
		t.Fatalf("ExportKeiToolManifest() returned error: %v", err)
	}

	// Read the fixture
	fixturePath := filepath.Join("..", "..", "fixtures", "kei", "tool-manifest.v2.json")
	fixtureData, err := os.ReadFile(filepath.Clean(fixturePath))
	if err != nil {
		t.Fatalf("failed to read fixture: %v", err)
	}

	// Normalize both to decoded JSON for comparison
	var gotNormalized, fixtureNormalized any
	if err := json.Unmarshal(got, &gotNormalized); err != nil {
		t.Fatalf("failed to unmarshal output: %v", err)
	}
	if err := json.Unmarshal(fixtureData, &fixtureNormalized); err != nil {
		t.Fatalf("failed to unmarshal fixture: %v", err)
	}

	gotJSON, _ := json.MarshalIndent(gotNormalized, "", "  ")
	fixtureJSON, _ := json.MarshalIndent(fixtureNormalized, "", "  ")

	if string(gotJSON) != string(fixtureJSON) {
		t.Errorf("ExportKeiToolManifest() output does not match fixture\n--- got:\n%s\n--- want:\n%s", gotJSON, fixtureJSON)
	}
	if !bytes.Equal(bytes.TrimSpace(got), bytes.TrimSpace(fixtureData)) {
		t.Fatalf("serialized manifest differs from shared fixture\n%s", got)
	}
}

func TestExportKeiToolManifest_EmptyRegistry(t *testing.T) {
	registry := NewToolRegistry()
	got, err := registry.ExportKeiToolManifest()
	if err != nil {
		t.Fatalf("ExportKeiToolManifest() returned error: %v", err)
	}
	var m KeiToolManifest
	if err := json.Unmarshal(got, &m); err != nil {
		t.Fatalf("output is not valid JSON: %v", err)
	}
	if len(m.Tools) != 0 {
		t.Errorf("expected empty tools list, got %d tools", len(m.Tools))
	}
}

func TestV1ExportRequiresExplicitOption(t *testing.T) {
	r := NewToolRegistry()
	r.Register(&governedTool{name: "github.get_issue", description: "get issue", scope: KeiScope{Source: "github", Service: "github", RequiredCapabilities: []string{"issue.read"}, ResourceTypes: []KeiResourceType{{Type: "issue"}}, OperationClass: "read"}})
	raw, err := r.ExportKeiToolManifest(ManifestV1)
	if err != nil {
		t.Fatal(err)
	}
	var parsed map[string]any
	if err := json.Unmarshal(raw, &parsed); err != nil {
		t.Fatal(err)
	}
	if _, ok := parsed["schema"]; ok {
		t.Fatal("v1 manifest unexpectedly has schema")
	}
}

func TestExportKeiToolManifest_UngovernedExcluded(t *testing.T) {
	registry := NewToolRegistry()
	registry.Register(&plainTool{name: "local_echo", description: "Echo"})

	got, err := registry.ExportKeiToolManifest()
	if err != nil {
		t.Fatalf("ExportKeiToolManifest() returned error: %v", err)
	}
	var m KeiToolManifest
	if err := json.Unmarshal(got, &m); err != nil {
		t.Fatalf("output is not valid JSON: %v", err)
	}
	if len(m.Tools) != 0 {
		t.Errorf("expected 0 tools (ungoverned excluded), got %d", len(m.Tools))
	}
}

func TestKeiToolManifestEntry_JSONShape(t *testing.T) {
	entry := KeiToolManifestEntry{
		Name: "test_tool", Source: "github", RequiredCapabilities: []string{"issue.read"},
		Description: "A test tool", OperationClass: "read", ResourceTypes: []KeiResourceType{{Type: "issue"}}, Enabled: true,
	}
	data, err := json.Marshal(entry)
	if err != nil {
		t.Fatalf("marshal error: %v", err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("unmarshal error: %v", err)
	}
	if decoded["name"] != "test_tool" {
		t.Errorf("expected name test_tool, got %v", decoded["name"])
	}
	if decoded["source"] != "github" {
		t.Errorf("expected source github, got %v", decoded["source"])
	}
	if decoded["operation_class"] != "read" {
		t.Errorf("expected operation_class read, got %v", decoded["operation_class"])
	}
	if decoded["enabled"] != true {
		t.Errorf("expected enabled true, got %v", decoded["enabled"])
	}
}
