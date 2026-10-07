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

func TestV4ExportMatchesFixtureAndRequiresExplicitSelection(t *testing.T) {
	r := NewToolRegistry()
	tool := &extendedGovernedTool{governedTool{name: "github.get_issue", description: "Fetch issue", scope: KeiScope{}}}
	r.Register(tool, KeiToolRegistration{Service: "github", Source: "github", OperationClass: "read", Route: ToolRoute{ConnectorBinding: &ConnectorBindingRoute{AgentID: "agent-1", ConnectorID: "github-1"}}, RequiredCapabilities: []string{"issue.read"}, ResourceTypes: []KeiResourceType{{Type: "issue", ParentType: "repository"}}, Plan: &KeiToolPlan{ContextSchema: map[string]any{"type": "object", "properties": map[string]any{"repository": map[string]any{"type": "string", "minLength": 1, "maxLength": 256, "pattern": "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"}}, "required": []any{"repository"}, "additionalProperties": false}, Operations: []KeiToolOperation{{ID: "get-issue", Capability: "issue.read", Resource: KeiPlannedResource{Type: "issue", ID: KeiValueRef{From: "args", Pointer: "/issue_number"}, Parent: &KeiPlannedParent{Type: "repository", ID: KeiValueRef{From: "context", Field: "repository"}}}, ProviderResourceTemplate: "repos/{parent.id}/issues/{resource.id}", ProviderInput: map[string]any{}}}}})
	if _, err := r.ExportKeiToolManifest(); err != nil {
		t.Fatal(err)
	}
	got, err := r.ExportKeiToolManifest(ManifestV4)
	if err != nil {
		t.Fatal(err)
	}
	fixture, err := os.ReadFile(filepath.Join("..", "..", "fixtures", "kei", "tool-manifest.v4.json"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(bytes.TrimSpace(got), bytes.TrimSpace(fixture)) {
		t.Fatalf("v4 output differs from fixture\n%s", got)
	}
}

func TestV4RejectsInvalidPlan(t *testing.T) {
	r := NewToolRegistry()
	tool := &extendedGovernedTool{governedTool{name: "x", description: "x"}}
	r.Register(tool, KeiToolRegistration{Service: "s", Source: "s", OperationClass: "read", Route: ToolRoute{ConnectorBinding: &ConnectorBindingRoute{AgentID: "a", ConnectorID: "c"}}, RequiredCapabilities: []string{"cap"}, Plan: &KeiToolPlan{ContextSchema: map[string]any{"type": "object", "properties": map[string]any{}, "additionalProperties": false}, Operations: []KeiToolOperation{{ID: "one", Capability: "other"}}}})
	if _, err := r.ExportKeiToolManifest(ManifestV4); err == nil {
		t.Fatal("expected undeclared capability to fail")
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
