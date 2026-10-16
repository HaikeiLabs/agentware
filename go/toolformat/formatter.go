package toolformat

import "github.com/haikeilabs/agentware/go/tools"

type ToolFormatter interface {
	FormatToolDefinitions(tools []tools.Tool) string
	ParseToolCalls(response string) ([]ParsedToolCall, error)
	FormatToolResult(name string, result *tools.Result) string
	ModelFamily() string
	ValidateFormat(response string) error
}

type ParsedToolCall struct {
	ID   string
	Name string
	Args map[string]any
	Raw  string
}
