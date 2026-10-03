package toolformat

import (
	"encoding/json"
	"fmt"
	"regexp"

	"github.com/soypete/pedro-agentware/go/tools"
)

type AnthropicFormatter struct{}

var anthropicCallsOuterRegex = regexp.MustCompile(`<function_calls>\s*(.*?)\s*</function_calls>`)
var anthropicInvokeRegex = regexp.MustCompile(`<invoke tool="([^"]+)">\s*(.*?)\s*</invoke>`)
var anthropicAnyInvokeRegex = regexp.MustCompile(`<invoke\b[^>]*>`)

func (f *AnthropicFormatter) FormatToolDefinitions(toolsList []tools.Tool) string {
	if len(toolsList) == 0 {
		return ""
	}

	schemas := make([]map[string]any, 0, len(toolsList))
	for _, t := range toolsList {
		schema := map[string]any{
			"type": "function",
			"function": map[string]any{
				"name":        t.Name(),
				"description": t.Description(),
			},
		}
		if et, ok := t.(tools.ExtendedTool); ok {
			if s := et.InputSchema(); s != nil {
				schema["function"].(map[string]any)["parameters"] = s
			}
		}
		schemas = append(schemas, schema)
	}

	b, _ := json.Marshal(schemas)
	return string(b)
}

func (f *AnthropicFormatter) ParseToolCalls(response string) ([]ParsedToolCall, error) {
	if response == "" {
		return nil, nil
	}

	outerMatch := anthropicCallsOuterRegex.FindStringSubmatch(response)
	if outerMatch == nil {
		return nil, nil
	}

	inner := outerMatch[1]
	invokeMatches := anthropicInvokeRegex.FindAllStringSubmatch(inner, -1)
	if len(invokeMatches) == 0 {
		return nil, nil
	}

	result := make([]ParsedToolCall, 0, len(invokeMatches))
	for _, m := range invokeMatches {
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

func (f *AnthropicFormatter) FormatToolResult(name string, result *tools.Result) string {
	if result.Success {
		return fmt.Sprintf(`<function_results><result tool="%s">%s</result></function_results>`, name, result.Output)
	}
	return fmt.Sprintf(`<function_results><result tool="%s">Error: %s</result></function_results>`, name, result.Error)
}

func (f *AnthropicFormatter) ModelFamily() string {
	return "anthropic"
}

func (f *AnthropicFormatter) ValidateFormat(response string) error {
	if response == "" {
		return nil
	}

	outerMatch := anthropicCallsOuterRegex.FindStringSubmatch(response)
	if outerMatch == nil {
		return nil
	}

	inner := outerMatch[1]

	anyInvoke := anthropicAnyInvokeRegex.FindString(inner)
	if anyInvoke != "" && !anthropicInvokeRegex.MatchString(inner) {
		return fmt.Errorf("invalid invoke tag: missing 'tool' attribute")
	}

	invokeMatches := anthropicInvokeRegex.FindAllStringSubmatch(inner, -1)
	for _, m := range invokeMatches {
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
