package toolformat

import (
	"encoding/json"
	"fmt"
	"regexp"

	"github.com/haikeilabs/agentware/go/tools"
	"github.com/haikeilabs/agentware/go/toolspec"
)

type QwenFormatter struct{}

var qwenToolCallRegex = regexp.MustCompile(`<tool_call>\s*<tool name="([^"]+)">\s*(.*?)\s*</tool>\s*</tool_call>`)
var qwenAnyToolRegex = regexp.MustCompile(`<tool\b[^>]*>`)

func (f *QwenFormatter) FormatToolDefinitions(toolsList []tools.Tool) string {
	return f.formatToolDefinitions(adaptTools(toolsList))
}

// FormatToolSpecs formats the model-facing fields from shared ToolSpecs.
func (f *QwenFormatter) FormatToolSpecs(specs []toolspec.ToolSpec) string {
	return f.formatToolDefinitions(adaptToolSpecs(specs))
}

func (f *QwenFormatter) formatToolDefinitions(toolsList []formatTool) string {
	if len(toolsList) == 0 {
		return ""
	}

	var result string
	for _, t := range toolsList {
		result += fmt.Sprintf(`<tool_description>
<tool_name>%s</tool_name>
<description>%s</description>
`, t.Name(), t.Description())

		if et, ok := t.(interface{ InputSchema() map[string]any }); ok {
			if schema := et.InputSchema(); schema != nil {
				schemaJSON, _ := json.Marshal(schema)
				result += fmt.Sprintf(`<parameters>%s</parameters>`, schemaJSON) + "\n"
			}
		}
		result += "</tool_description>\n"
	}
	return result
}

func (f *QwenFormatter) ParseToolCalls(response string) ([]ParsedToolCall, error) {
	if response == "" {
		return nil, nil
	}

	matches := qwenToolCallRegex.FindAllStringSubmatch(response, -1)
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

		args := decodeLooseArgs(argsRaw)

		result = append(result, ParsedToolCall{
			Name: name,
			Args: args,
			Raw:  m[0],
		})
	}
	return result, nil
}

func (f *QwenFormatter) FormatToolResult(name string, result *tools.Result) string {
	if result.Success {
		return fmt.Sprintf(`<tool_response>
<tool_name>%s</tool_name>
<result>%s</result>
</tool_response>`, name, result.Output)
	}
	return fmt.Sprintf(`<tool_response>
<tool_name>%s</tool_name>
<error>%s</error>
</tool_response>`, name, result.Error)
}

func (f *QwenFormatter) ModelFamily() string {
	return "qwen"
}

func (f *QwenFormatter) ValidateFormat(response string) error {
	if response == "" {
		return nil
	}

	anyTool := qwenAnyToolRegex.FindString(response)
	if anyTool != "" && !qwenToolCallRegex.MatchString(response) {
		return fmt.Errorf("invalid tool tag: missing 'name' attribute")
	}

	matches := qwenToolCallRegex.FindAllStringSubmatch(response, -1)
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
