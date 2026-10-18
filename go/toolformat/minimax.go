package toolformat

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	"github.com/haikeilabs/agentware/go/tools"
	"github.com/haikeilabs/agentware/go/toolspec"
)

type MiniMaxFormatter struct{}

var minimaxToolCallRegex = regexp.MustCompile(`(?s)(?:action|function_call|tool_call)[\"']?\s*[:=]\s*[\"']?([a-zA-Z_][a-zA-Z0-9_]*)[\"']?\s*\((.*?)\)`)
var minimaxToolCallRegex2 = regexp.MustCompile(`<tool_call>\s*<tool_name>([^<]+)</tool_name>\s*<parameters>(.*?)</parameters>\s*</tool_call>`)

func (f *MiniMaxFormatter) FormatToolDefinitions(toolsList []tools.Tool) string {
	return f.formatToolDefinitions(adaptTools(toolsList))
}

// FormatToolSpecs formats the model-facing fields from shared ToolSpecs.
func (f *MiniMaxFormatter) FormatToolSpecs(specs []toolspec.ToolSpec) string {
	return f.formatToolDefinitions(adaptToolSpecs(specs))
}

func (f *MiniMaxFormatter) formatToolDefinitions(toolsList []formatTool) string {
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

func (f *MiniMaxFormatter) ParseToolCalls(response string) ([]ParsedToolCall, error) {
	if response == "" {
		return nil, nil
	}

	response = strings.TrimSpace(response)

	{
		matches := minimaxToolCallRegex2.FindAllStringSubmatch(response, -1)
		if len(matches) > 0 {
			result := make([]ParsedToolCall, 0, len(matches))
			for _, m := range matches {
				if len(m) < 3 {
					continue
				}
				name := strings.TrimSpace(m[1])
				argsRaw := strings.TrimSpace(m[2])

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
	}

	{
		matches := minimaxToolCallRegex.FindAllStringSubmatch(response, -1)
		if len(matches) > 0 {
			result := make([]ParsedToolCall, 0, len(matches))
			for _, m := range matches {
				if len(m) < 3 {
					continue
				}
				name := strings.TrimSpace(m[1])
				argsRaw := strings.TrimSpace(m[2])

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
	}

	var toolCalls []struct {
		ID   string          `json:"id"`
		Name string          `json:"name"`
		Args json.RawMessage `json:"arguments"`
	}

	if err := json.Unmarshal([]byte(response), &toolCalls); err == nil && len(toolCalls) > 0 {
		result := make([]ParsedToolCall, 0, len(toolCalls))
		for _, c := range toolCalls {
			var args map[string]any
			if err := json.Unmarshal(c.Args, &args); err != nil {
				continue
			}
			result = append(result, ParsedToolCall{
				ID:   c.ID,
				Name: c.Name,
				Args: args,
				Raw:  response,
			})
		}
		return result, nil
	}

	return nil, nil
}

func (f *MiniMaxFormatter) FormatToolResult(name string, result *tools.Result) string {
	if result.Success {
		return fmt.Sprintf(`<tool_response><tool_name>%s</tool_name><result>%s</result></tool_response>`, name, result.Output)
	}
	return fmt.Sprintf(`<tool_response><tool_name>%s</tool_name><error>%s</error></tool_response>`, name, result.Error)
}

func (f *MiniMaxFormatter) ModelFamily() string {
	return "minimax"
}

func (f *MiniMaxFormatter) ValidateFormat(response string) error {
	if response == "" {
		return nil
	}

	calls, err := f.ParseToolCalls(response)
	if err != nil {
		return err
	}

	for _, c := range calls {
		if c.Name == "" {
			return fmt.Errorf("tool call missing function name")
		}
		if len(c.Args) == 0 {
			return fmt.Errorf("tool call %q missing arguments", c.Name)
		}
	}

	return nil
}
