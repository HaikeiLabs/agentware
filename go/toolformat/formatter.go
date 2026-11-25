package toolformat

import (
	"github.com/haikeilabs/agentware/go/tools"
	"github.com/haikeilabs/agentware/go/toolspec"
)

type ToolFormatter interface {
	FormatToolDefinitions(tools []tools.Tool) string
	ParseToolCalls(response string) ([]ParsedToolCall, error)
	// FormatToolResult renders a tool result for the next model turn. callID
	// is optional: the id of the tool call this result answers, when the
	// caller has one (only the first value is used). Families whose result
	// format carries no id ignore it; AnthropicFormatter renders it as
	// tool_use_id.
	FormatToolResult(name string, result *tools.Result, callID ...string) string
	ModelFamily() string
	ValidateFormat(response string) error
}

type ParsedToolCall struct {
	ID   string
	Name string
	Args map[string]any
	Raw  string
}

type formatTool interface {
	Name() string
	Description() string
	InputSchema() map[string]any
}

type legacyFormatTool struct{ tools.Tool }

func (t legacyFormatTool) InputSchema() map[string]any {
	if extended, ok := t.Tool.(tools.ExtendedTool); ok {
		return extended.InputSchema()
	}
	return nil
}

type specFormatTool struct{ toolspec.ToolSpec }

func (t specFormatTool) Name() string                { return t.ToolSpec.Name }
func (t specFormatTool) Description() string         { return t.ToolSpec.Description }
func (t specFormatTool) InputSchema() map[string]any { return t.Parameters }

func adaptTools(toolsList []tools.Tool) []formatTool {
	adapted := make([]formatTool, len(toolsList))
	for i, tool := range toolsList {
		adapted[i] = legacyFormatTool{Tool: tool}
	}
	return adapted
}

func adaptToolSpecs(specs []toolspec.ToolSpec) []formatTool {
	adapted := make([]formatTool, len(specs))
	for i, spec := range specs {
		adapted[i] = specFormatTool{ToolSpec: spec}
	}
	return adapted
}

// ToolSpecFormatter formats model-facing fields from the shared contract.
type ToolSpecFormatter interface {
	FormatToolSpecs(specs []toolspec.ToolSpec) string
}
