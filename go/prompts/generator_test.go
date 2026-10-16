package prompts

import (
	"context"
	"testing"

	"github.com/haikeilabs/agentware/go/tools"
)

type testPromptTool struct {
	name        string
	description string
}

func (t *testPromptTool) Name() string        { return t.name }
func (t *testPromptTool) Description() string { return t.description }
func (t *testPromptTool) Execute(ctx context.Context, args map[string]any) (*tools.Result, error) {
	return &tools.Result{Success: true}, nil
}

type testPromptExtendedTool struct {
	testPromptTool
}

func (t *testPromptExtendedTool) InputSchema() map[string]any {
	return map[string]any{"type": "object"}
}
func (t *testPromptExtendedTool) Examples() []tools.ToolExample { return nil }

func TestGenerateToolSection_Empty(t *testing.T) {
	registry := tools.NewToolRegistry()
	result := GenerateToolSection(registry)
	if result != "" {
		t.Errorf("expected empty string for empty registry, got '%s'", result)
	}
}

func TestGenerateToolSection_SingleTool(t *testing.T) {
	registry := tools.NewToolRegistry()
	registry.Register(&testPromptTool{
		name:        "my_tool",
		description: "Does something useful",
	})

	result := GenerateToolSection(registry)
	if result == "" {
		t.Fatal("expected non-empty result")
	}
	if !containsString(result, "## Available Tools") {
		t.Error("expected '## Available Tools' in result")
	}
	if !containsString(result, "my_tool") {
		t.Error("expected tool name in result")
	}
	if !containsString(result, "Does something useful") {
		t.Error("expected tool description in result")
	}
}

func TestGenerateToolSection_WithExtendedTool(t *testing.T) {
	registry := tools.NewToolRegistry()
	registry.Register(&testPromptExtendedTool{
		testPromptTool: testPromptTool{
			name:        "tool_with_examples",
			description: "A tool with examples",
		},
	})

	result := GenerateToolSection(registry)
	if !containsString(result, "tool_with_examples") {
		t.Error("expected tool name in result")
	}
}

func TestGenerateToolSchemas_Empty(t *testing.T) {
	registry := tools.NewToolRegistry()
	schemas := GenerateToolSchemas(registry)
	if schemas != nil {
		t.Errorf("expected nil for empty registry, got %v", schemas)
	}
}

func TestGenerateToolSchemas_SingleTool(t *testing.T) {
	registry := tools.NewToolRegistry()
	registry.Register(&testPromptTool{
		name:        "schema_tool",
		description: "Tool with schema",
	})

	schemas := GenerateToolSchemas(registry)
	if len(schemas) != 1 {
		t.Fatalf("expected 1 schema, got %d", len(schemas))
	}

	schema := schemas[0]
	if schema["type"] != "function" {
		t.Errorf("expected type 'function', got '%v'", schema["type"])
	}

	fn, ok := schema["function"].(map[string]any)
	if !ok {
		t.Fatal("expected function map")
	}
	if fn["name"] != "schema_tool" {
		t.Errorf("expected name 'schema_tool', got '%v'", fn["name"])
	}
}

func TestGenerateToolSchemas_WithInputSchema(t *testing.T) {
	registry := tools.NewToolRegistry()
	registry.Register(&testPromptExtendedTool{
		testPromptTool: testPromptTool{
			name:        "full_schema_tool",
			description: "Tool with full schema",
		},
	})

	schemas := GenerateToolSchemas(registry)
	if len(schemas) != 1 {
		t.Fatalf("expected 1 schema, got %d", len(schemas))
	}

	fn := schemas[0]["function"].(map[string]any)
	params, ok := fn["parameters"].(map[string]any)
	if !ok {
		t.Fatal("expected parameters in function")
	}
	if params["type"] != "object" {
		t.Errorf("expected object type, got '%v'", params["type"])
	}
}

func TestNewGenerator(t *testing.T) {
	gen := NewGenerator("test_format")
	if gen == nil {
		t.Fatal("expected non-nil generator")
	}

	registry := tools.NewToolRegistry()
	registry.Register(&testPromptTool{name: "gen_tool", description: "desc"})

	section := gen.GenerateToolSection(registry)
	if section == "" {
		t.Error("expected non-empty tool section")
	}

	schemas := gen.GenerateToolSchemas(registry)
	if len(schemas) != 1 {
		t.Errorf("expected 1 schema, got %d", len(schemas))
	}
}

func containsString(s, substr string) bool {
	return len(s) >= len(substr) && (s == substr || len(substr) == 0 ||
		(len(s) > 0 && len(substr) > 0 && findSubstring(s, substr)))
}

func findSubstring(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}
