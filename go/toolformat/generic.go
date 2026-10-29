package toolformat

import (
	"encoding/json"
	"fmt"

	"github.com/haikeilabs/agentware/go/tools"
	"github.com/haikeilabs/agentware/go/toolspec"
)

type GenericFormatter struct{}

func (f *GenericFormatter) FormatToolDefinitions(toolsList []tools.Tool) string {
	return f.formatToolDefinitions(adaptTools(toolsList))
}

// FormatToolSpecs formats the model-facing fields from shared ToolSpecs.
func (f *GenericFormatter) FormatToolSpecs(specs []toolspec.ToolSpec) string {
	return f.formatToolDefinitions(adaptToolSpecs(specs))
}

func (f *GenericFormatter) formatToolDefinitions(toolsList []formatTool) string {
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
		if et, ok := t.(interface{ InputSchema() map[string]any }); ok {
			if s := et.InputSchema(); s != nil {
				schema["function"].(map[string]any)["parameters"] = s
			}
		}
		schemas = append(schemas, schema)
	}

	b, _ := json.Marshal(schemas)
	return string(b)
}

// decodeToolCallArgs decodes a tool call's `arguments` value. OpenAI-style
// APIs (including GLM-5.3-Flash on vLLM) emit it either as a JSON object or
// as a JSON string containing the object; anything else is an error so a
// malformed call is never silently dropped.
func decodeToolCallArgs(raw json.RawMessage) (map[string]any, error) {
	var args map[string]any
	if err := json.Unmarshal(raw, &args); err == nil {
		return args, nil
	}
	var encoded string
	if err := json.Unmarshal(raw, &encoded); err != nil {
		return nil, fmt.Errorf("arguments must be an object or a string containing one: %w", err)
	}
	if err := json.Unmarshal([]byte(encoded), &args); err != nil {
		return nil, fmt.Errorf("arguments string is not a JSON object: %w", err)
	}
	return args, nil
}

func (f *GenericFormatter) ParseToolCalls(response string) ([]ParsedToolCall, error) {
	if response == "" {
		return nil, nil
	}

	var calls []struct {
		ID   string          `json:"id"`
		Name string          `json:"name"`
		Args json.RawMessage `json:"arguments"`
	}

	if err := json.Unmarshal([]byte(response), &calls); err != nil {
		return nil, fmt.Errorf("failed to parse tool calls: %w", err)
	}

	result := make([]ParsedToolCall, 0, len(calls))
	for _, c := range calls {
		args, err := decodeToolCallArgs(c.Args)
		if err != nil {
			return nil, fmt.Errorf("tool call %q: %w", c.Name, err)
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

func (f *GenericFormatter) FormatToolResult(name string, result *tools.Result) string {
	if result.Success {
		return result.Output
	}
	return fmt.Sprintf("Error: %s", result.Error)
}

func (f *GenericFormatter) ModelFamily() string {
	return "generic"
}

func (f *GenericFormatter) ValidateFormat(response string) error {
	if response == "" {
		return nil
	}

	var calls []struct {
		ID   string          `json:"id"`
		Name string          `json:"name"`
		Args json.RawMessage `json:"arguments"`
	}

	if err := json.Unmarshal([]byte(response), &calls); err != nil {
		return fmt.Errorf("invalid JSON format: %w", err)
	}

	for _, c := range calls {
		if c.Name == "" {
			return fmt.Errorf("tool call missing 'name' field")
		}
		if len(c.Args) == 0 {
			return fmt.Errorf("tool call %q missing 'arguments'", c.Name)
		}
	}
	return nil
}
