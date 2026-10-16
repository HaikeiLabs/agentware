package prompts

import "github.com/haikeilabs/agentware/go/tools"

// PromptGenerator generates the tool-use section of a system prompt
// from the registered tools in a ToolRegistry.
type PromptGenerator interface {
	// GenerateToolSection returns the formatted tool documentation block
	// to inject into the system prompt.
	GenerateToolSection(registry *tools.ToolRegistry) string
	// GenerateToolSchemas returns the full JSON schema block for all tools
	// in the registry, suitable for native tool calling APIs.
	GenerateToolSchemas(registry *tools.ToolRegistry) []map[string]any
}

// generator is the standard implementation of PromptGenerator.
type generator struct {
	format string
}

// NewGenerator constructs a new PromptGenerator with the given format.
func NewGenerator(format string) PromptGenerator {
	return &generator{format: format}
}

// GenerateToolSection generates the tool section for the registry.
func (g *generator) GenerateToolSection(registry *tools.ToolRegistry) string {
	return GenerateToolSection(registry)
}

// GenerateToolSchemas generates the tool schemas for the registry.
func (g *generator) GenerateToolSchemas(registry *tools.ToolRegistry) []map[string]any {
	return GenerateToolSchemas(registry)
}
