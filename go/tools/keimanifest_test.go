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
	registry.Register(&plainTool{name: "github.get_issue", description: "Fetch an issue from a GitHub repository"}, KeiToolRegistration{
		Service: "github", Source: "github", OperationClass: "read",
		Route:                ToolRoute{ConnectorBinding: &ConnectorBindingRoute{AgentID: "11111111-1111-4111-8111-111111111111", ConnectorID: "22222222-2222-4222-8222-222222222222"}},
		RequiredCapabilities: []string{"issue.read"}, ResourceTypes: []KeiResourceType{{Type: "issue", ParentType: "repository"}},
	})
	registry.Register(&plainTool{name: "codex.shell", description: "Run a local harness action"}, KeiToolRegistration{
		Service: "codex", Source: "codex", OperationClass: "write",
		Route: ToolRoute{HarnessExecutor: &HarnessExecutorRoute{Executor: "codex", Registration: "codex.shell"}},
	})

	got, err := registry.ExportKeiToolManifest()
	if err != nil {
		t.Fatalf("ExportKeiToolManifest() returned error: %v", err)
	}

	// Read the fixture
	fixturePath := filepath.Join("..", "..", "fixtures", "kei", "tool-manifest.v3.json")
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
	var m map[string]any
	if err := json.Unmarshal(got, &m); err != nil {
		t.Fatalf("output is not valid JSON: %v", err)
	}
	if m["schema"] != "kei.tool-manifest/v3" || len(m["tools"].([]any)) != 0 {
		t.Errorf("expected empty v3 manifest, got %#v", m)
	}
}

func TestV3ConnectorRouteWinsOverLocalDispatchHandler(t *testing.T) {
	r := NewToolRegistry()
	r.Register(&plainTool{name: "local.echo", description: "Echo"}, KeiToolRegistration{
		Service: "local", Source: "harness", OperationClass: "write",
		Route: ToolRoute{HarnessExecutor: &HarnessExecutorRoute{Executor: "pi", Registration: "local.echo"}},
	})
	r.Register(&plainTool{name: "github.get_issue", description: "Get issue"}, KeiToolRegistration{
		Service: "github", Source: "github", OperationClass: "read",
		Route:                ToolRoute{ConnectorBinding: &ConnectorBindingRoute{AgentID: "agent-1", ConnectorID: "binding-1"}},
		RequiredCapabilities: []string{"issue.read"}, ResourceTypes: []KeiResourceType{{Type: "issue"}},
	})
	got, err := r.ExportKeiToolManifest(ManifestV3)
	if err != nil {
		t.Fatal(err)
	}
	var manifest map[string]any
	if err := json.Unmarshal(got, &manifest); err != nil {
		t.Fatal(err)
	}
	if manifest["schema"] != "kei.tool-manifest/v3" {
		t.Fatalf("schema = %v", manifest["schema"])
	}
	entries := manifest["tools"].([]any)
	connector := entries[0].(map[string]any)
	if _, ok := r.Get("github.get_issue"); !ok {
		t.Fatal("connector-bound tool lost its local dispatch handler")
	}
	if _, routeHasHandler := connector["route"].(map[string]any)["harness_executor"]; routeHasHandler {
		t.Fatal("connector binding did not take precedence over the tool's harness Execute handler")
	}
	if _, routeHasBinding := connector["route"].(map[string]any)["connector_binding"]; !routeHasBinding {
		t.Fatal("connector registration was not exported")
	}
	harness := entries[1].(map[string]any)
	if _, ok := harness["required_capabilities"]; ok {
		t.Fatal("harness entry contains connector capabilities")
	}
	if _, ok := harness["resource_types"]; ok {
		t.Fatal("harness entry contains connector resources")
	}
}

func TestV3ExportRejectsConnectorRouteWithoutAgentID(t *testing.T) {
	r := NewToolRegistry()
	r.Register(&plainTool{name: "github.read", description: "Read"}, KeiToolRegistration{
		Service: "github", Source: "github", OperationClass: "read",
		Route:                ToolRoute{ConnectorBinding: &ConnectorBindingRoute{ConnectorID: "binding-1"}},
		RequiredCapabilities: []string{"issue.read"},
	})
	if _, err := r.ExportKeiToolManifest(ManifestV3); err == nil {
		t.Fatal("expected connector route without agent_id to fail")
	}
}

func TestV3ExportRejectsEmptyService(t *testing.T) {
	r := NewToolRegistry()
	r.Register(&plainTool{name: "local.echo", description: "Echo"}, KeiToolRegistration{
		Source: "harness", OperationClass: "write",
		Route: ToolRoute{HarnessExecutor: &HarnessExecutorRoute{Executor: "pi", Registration: "local.echo"}},
	})
	if _, err := r.ExportKeiToolManifest(ManifestV3); err == nil {
		t.Fatal("expected invalid registration error")
	}
}

type extendedGovernedTool struct{ governedTool }

func (t *extendedGovernedTool) InputSchema() map[string]any {
	return map[string]any{"type": "object", "properties": map[string]any{"issue_number": map[string]any{"type": "integer", "minimum": 1}}, "required": []any{"issue_number"}, "additionalProperties": false}
}
func (t *extendedGovernedTool) Examples() []ToolExample { return nil }

func TestExportKeiToolManifest_UngovernedExcluded(t *testing.T) {
	registry := NewToolRegistry()
	registry.Register(&plainTool{name: "local_echo", description: "Echo"})

	got, err := registry.ExportKeiToolManifest()
	if err != nil {
		t.Fatalf("ExportKeiToolManifest() returned error: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(got, &m); err != nil {
		t.Fatalf("output is not valid JSON: %v", err)
	}
	if m["schema"] != "kei.tool-manifest/v3" || len(m["tools"].([]any)) != 0 {
		t.Errorf("expected empty v3 manifest, got %#v", m)
	}
}
