package toolformat

import (
	"encoding/json"
	"fmt"
	"regexp"

	"github.com/haikeilabs/agentware/go/tools"
	"github.com/haikeilabs/agentware/go/toolspec"
)

type MistralFormatter struct{}

var mistralToolCallRegex = regexp.MustCompile(`\[TOOL_CALLS\]\s*\[(.*?)\]\s*\[/TOOL_CALLS\]`)
var mistralToolRegex = regexp.MustCompile(`tool_call>\s*(\w+)\s*:\s*(\{.*?\})`)

func (f *MistralFormatter) FormatToolDefinitions(toolsList []tools.Tool) string {
	return f.formatToolDefinitions(adaptTools(toolsList))
}

// FormatToolSpecs formats the model-facing fields from shared ToolSpecs.
func (f *MistralFormatter) FormatToolSpecs(specs []toolspec.ToolSpec) string {
	return f.formatToolDefinitions(adaptToolSpecs(specs))
}

func (f *MistralFormatter) formatToolDefinitions(toolsList []formatTool) string {
	if len(toolsList) == 0 {
		return ""
	}

	var result string
	result += "[TOOL_DEFINITIONS]\n"
	for _, t := range toolsList {
		result += fmt.Sprintf("{%s: %s}", t.Name(), t.Description())
		if et, ok := t.(interface{ InputSchema() map[string]any }); ok {
			if schema := et.InputSchema(); schema != nil {
				schemaJSON, _ := json.Marshal(schema)
				result += fmt.Sprintf(" | params: %s", schemaJSON)
			}
		}
		result += "\n"
	}
	result += "[/TOOL_DEFINITIONS]\n"
	return result
}

func (f *MistralFormatter) ParseToolCalls(response string) ([]ParsedToolCall, error) {
	if response == "" {
		return nil, nil
	}

	matches := mistralToolCallRegex.FindAllStringSubmatch(response, -1)
	if len(matches) == 0 {
		return nil, nil
	}

	result := make([]ParsedToolCall, 0)
	for _, m := range matches {
		if len(m) < 2 {
			continue
		}
		inner := m[1]

		toolMatches := mistralToolRegex.FindAllStringSubmatch(inner, -1)
		for _, tm := range toolMatches {
			if len(tm) < 3 {
				continue
			}
			name := tm[1]
			argsRaw := tm[2]

			var args map[string]any
			if err := json.Unmarshal([]byte(argsRaw), &args); err != nil {
				args = map[string]any{"_raw": argsRaw}
			}

			result = append(result, ParsedToolCall{
				Name: name,
				Args: args,
				Raw:  tm[0],
			})
		}
	}
	return result, nil
}

func (f *MistralFormatter) FormatToolResult(name string, result *tools.Result) string {
	if result.Success {
		return fmt.Sprintf("[TOOL_RESULT]\n%s\n%s\n[/TOOL_RESULT]", name, result.Output)
	}
	return fmt.Sprintf("[TOOL_RESULT]\n%s\nerror: %s\n[/TOOL_RESULT]", name, result.Error)
}

func (f *MistralFormatter) ModelFamily() string {
	return "mistral"
}

func (f *MistralFormatter) ValidateFormat(response string) error {
	if response == "" {
		return nil
	}

	matches := mistralToolCallRegex.FindAllStringSubmatch(response, -1)
	for _, m := range matches {
		if len(m) < 2 {
			continue
		}
		inner := m[1]

		toolMatches := mistralToolRegex.FindAllStringSubmatch(inner, -1)
		for _, tm := range toolMatches {
			if len(tm) < 3 {
				continue
			}
			name := tm[1]
			argsRaw := tm[2]

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
	}
	return nil
}
