package toolformat

import (
	"encoding/json"
	"fmt"

	"github.com/haikeilabs/agentware/go/tools"
)

type NemotronFormatter struct{}

func (f *NemotronFormatter) FormatToolDefinitions(toolsList []tools.Tool) string {
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

func (f *NemotronFormatter) ParseToolCalls(response string) ([]ParsedToolCall, error) {
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

func (f *NemotronFormatter) FormatToolResult(name string, result *tools.Result) string {
	if result.Success {
		return result.Output
	}
	return fmt.Sprintf("Error: %s", result.Error)
}

func (f *NemotronFormatter) ModelFamily() string {
	return "nemotron"
}

func (f *NemotronFormatter) ValidateFormat(response string) error {
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
