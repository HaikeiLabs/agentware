package tools

import (
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
			Service:   "github",
			Action:    "read",
			Resources: []string{"repo:haikeilabs/*", "issue:*"},
		},
	})
	registry.Register(&governedTool{
		name:        "github.list_issues",
		description: "List issues in a GitHub repository",
		scope: KeiScope{
			Service:   "github",
			Action:    "read",
			Resources: []string{"repo:haikeilabs/*", "issue:*"},
		},
	})
	registry.Register(&governedTool{
		name:        "linear.get_issue",
		description: "Fetch an issue from Linear",
		scope: KeiScope{
			Service:   "linear",
			Action:    "read",
			Resources: []string{"team:*", "issue:*"},
		},
	})
	registry.Register(&governedTool{
		name:        "slack.post_message",
		description: "Post a message to a Slack channel",
		scope: KeiScope{
			Service:   "slack",
			Action:    "write",
			Resources: []string{"channel:*"},
		},
	})
	registry.Register(&governedTool{
		name:        "send_email",
		description: "Send an email message",
		scope: KeiScope{
			Service:   "email",
			Action:    "write",
			Resources: nil, // should become empty slice
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
	fixturePath := filepath.Join("..", "..", "fixtures", "kei", "tool-manifest.v1.json")
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
		Name:        "test_tool",
		Service:     "test_service",
		Description: "A test tool",
		Action:      "read",
		Resources:   []string{"res:*"},
		Enabled:     true,
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
	if decoded["service"] != "test_service" {
		t.Errorf("expected service test_service, got %v", decoded["service"])
	}
	if decoded["action"] != "read" {
		t.Errorf("expected action read, got %v", decoded["action"])
	}
	if decoded["enabled"] != true {
		t.Errorf("expected enabled true, got %v", decoded["enabled"])
	}
}
