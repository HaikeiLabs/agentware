package toolformat

import (
	"encoding/json"
	"fmt"
	"regexp"

	"github.com/haikeilabs/agentware/go/tools"
	"github.com/haikeilabs/agentware/go/toolspec"
)

type LlamaFormatter struct{}

var llamaToolCallRegex = regexp.MustCompile(`<\|python_tag\|>\s*([a-zA-Z_][a-zA-Z0-9_]*)\((.*?)\)`)

func (f *LlamaFormatter) FormatToolDefinitions(toolsList []tools.Tool) string {
	return f.formatToolDefinitions(adaptTools(toolsList))
}

// FormatToolSpecs formats the model-facing fields from shared ToolSpecs.
func (f *LlamaFormatter) FormatToolSpecs(specs []toolspec.ToolSpec) string {
	return f.formatToolDefinitions(adaptToolSpecs(specs))
}

func (f *LlamaFormatter) formatToolDefinitions(toolsList []formatTool) string {
	if len(toolsList) == 0 {
		return ""
	}

	var result string
	result += "Available tools:\n"
	for _, t := range toolsList {
		result += fmt.Sprintf("- %s: %s\n", t.Name(), t.Description())
		if et, ok := t.(interface{ InputSchema() map[string]any }); ok {
			if schema := et.InputSchema(); schema != nil {
				schemaJSON, _ := json.Marshal(schema)
				result += fmt.Sprintf("  Parameters: %s\n", schemaJSON)
			}
		}
	}
	return result
}

func (f *LlamaFormatter) ParseToolCalls(response string) ([]ParsedToolCall, error) {
	if response == "" {
		return nil, nil
	}

	matches := llamaToolCallRegex.FindAllStringSubmatch(response, -1)
	if len(matches) == 0 {
		return nil, nil
	}

	result := make([]ParsedToolCall, 0, len(matches))
	for _, m := range matches {
		if len(m) < 3 {
			continue
		}
		name := m[1]
		argsRaw := m[2]

		var args map[string]any
		if err := json.Unmarshal([]byte(argsRaw), &args); err != nil {
			args = map[string]any{"_raw": argsRaw}
		}

		result = append(result, ParsedToolCall{
			Name: name,
			Args: args,
			Raw:  m[0],
		})
	}
	return result, nil
}

func (f *LlamaFormatter) FormatToolResult(name string, result *tools.Result) string {
	if result.Success {
		return fmt.Sprintf("%s: %s", name, result.Output)
	}
	return fmt.Sprintf("%s error: %s", name, result.Error)
}

func (f *LlamaFormatter) ModelFamily() string {
	return "llama"
}

func (f *LlamaFormatter) ValidateFormat(response string) error {
	if response == "" {
		return nil
	}

	matches := llamaToolCallRegex.FindAllStringSubmatch(response, -1)
	for _, m := range matches {
		if len(m) < 3 {
			continue
		}
		name := m[1]
		argsRaw := m[2]

		if name == "" {
			return fmt.Errorf("tool call missing function name")
		}
		if argsRaw == "" {
			return fmt.Errorf("tool call %q missing arguments", name)
		}

		var args map[string]any
		if err := json.Unmarshal([]byte(argsRaw), &args); err != nil {
			return fmt.Errorf("tool call %q has invalid JSON arguments: %w", name, err)
		}
	}
	return nil
}
