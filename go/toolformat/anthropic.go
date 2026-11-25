package toolformat

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"

	"github.com/haikeilabs/agentware/go/tools"
	"github.com/haikeilabs/agentware/go/toolspec"
)

// AnthropicFormatter renders Anthropic Claude tool use as text.
//
// Definitions are Anthropic tool definitions: {"name", "description",
// "input_schema"}. Calls are Anthropic tool_use blocks, {"type": "tool_use",
// "id", "name", "input"}, given as one block or as a content array (its other
// blocks, such as text, are ignored); each call keeps the block's id. When the
// response holds no tool_use block, the legacy
// <function_calls><invoke tool="name">{json}</invoke></function_calls> form is
// parsed instead, and those calls have no id. Results are tool_result blocks,
// {"type": "tool_result", "tool_use_id", "content", "is_error"}. Without a
// call id, tool_name takes the place of tool_use_id; that block is a text
// rendering only, not a valid Messages API block.
//
// See https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
type AnthropicFormatter struct{}

var anthropicCallsOuterRegex = regexp.MustCompile(`<function_calls>\s*(.*?)\s*</function_calls>`)
var anthropicInvokeRegex = regexp.MustCompile(`<invoke tool="([^"]+)">\s*(.*?)\s*</invoke>`)
var anthropicAnyInvokeRegex = regexp.MustCompile(`<invoke\b[^>]*>`)

// anthropicToolDefinition is one Anthropic tool definition. The field order
// matches the other ports' output.
type anthropicToolDefinition struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"input_schema"`
}

// anthropicToolResult is one tool_result block. ToolName stands in for
// ToolUseID only when the caller has no call id.
type anthropicToolResult struct {
	Type      string `json:"type"`
	ToolUseID string `json:"tool_use_id,omitempty"`
	ToolName  string `json:"tool_name,omitempty"`
	Content   string `json:"content"`
	IsError   bool   `json:"is_error,omitempty"`
}

func (f *AnthropicFormatter) FormatToolDefinitions(toolsList []tools.Tool) string {
	return f.formatToolDefinitions(adaptTools(toolsList))
}

// FormatToolSpecs formats the model-facing fields from shared ToolSpecs.
func (f *AnthropicFormatter) FormatToolSpecs(specs []toolspec.ToolSpec) string {
	return f.formatToolDefinitions(adaptToolSpecs(specs))
}

func (f *AnthropicFormatter) formatToolDefinitions(toolsList []formatTool) string {
	if len(toolsList) == 0 {
		return ""
	}

	definitions := make([]anthropicToolDefinition, 0, len(toolsList))
	for _, t := range toolsList {
		schema := t.InputSchema()
		if schema == nil {
			// The Messages API requires input_schema; a tool without
			// parameters takes an empty object schema.
			schema = map[string]any{"type": "object", "properties": map[string]any{}}
		}
		definitions = append(definitions, anthropicToolDefinition{
			Name:        t.Name(),
			Description: t.Description(),
			InputSchema: schema,
		})
	}
	return anthropicMarshal(definitions)
}

// anthropicMarshal encodes v as compact JSON without HTML escaping, so <, >
// and & stay literal as in the other ports.
func anthropicMarshal(v any) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return ""
	}
	return strings.TrimSuffix(buf.String(), "\n")
}

// anthropicToolUseBlocks returns the tool_use blocks of a response that is a
// JSON block or a JSON content array. Any other response yields none.
func anthropicToolUseBlocks(response string) []map[string]any {
	text := strings.TrimSpace(response)
	if text == "" || (text[0] != '{' && text[0] != '[') {
		return nil
	}
	var value any
	if err := json.Unmarshal([]byte(text), &value); err != nil {
		return nil
	}
	items, ok := value.([]any)
	if !ok {
		items = []any{value}
	}
	var blocks []map[string]any
	for _, item := range items {
		if block, ok := item.(map[string]any); ok && block["type"] == "tool_use" {
			blocks = append(blocks, block)
		}
	}
	return blocks
}

// anthropicToolUseError reports why a tool_use block is malformed, or nil.
func anthropicToolUseError(block map[string]any) error {
	name, _ := block["name"].(string)
	if name == "" {
		return errors.New("tool_use block missing name")
	}
	if id, _ := block["id"].(string); id == "" {
		return fmt.Errorf("tool_use block %q missing id", name)
	}
	if _, ok := block["input"].(map[string]any); !ok {
		return fmt.Errorf("tool_use block %q input must be a JSON object", name)
	}
	return nil
}

func (f *AnthropicFormatter) ParseToolCalls(response string) ([]ParsedToolCall, error) {
	if response == "" {
		return nil, nil
	}

	blocks := anthropicToolUseBlocks(response)
	if len(blocks) == 0 {
		return f.parseInvoke(response), nil
	}

	calls := make([]ParsedToolCall, 0, len(blocks))
	for _, block := range blocks {
		// Fail closed: a malformed tool_use block is never returned as a
		// call (ValidateFormat names the problem).
		if anthropicToolUseError(block) != nil {
			continue
		}
		calls = append(calls, ParsedToolCall{
			ID:   block["id"].(string),
			Name: block["name"].(string),
			Args: block["input"].(map[string]any),
			Raw:  response,
		})
	}
	return calls, nil
}

// parseInvoke parses the legacy <function_calls><invoke> form. Those calls
// carry no id.
func (f *AnthropicFormatter) parseInvoke(response string) []ParsedToolCall {
	outerMatch := anthropicCallsOuterRegex.FindStringSubmatch(response)
	if outerMatch == nil {
		return nil
	}

	inner := outerMatch[1]
	invokeMatches := anthropicInvokeRegex.FindAllStringSubmatch(inner, -1)
	if len(invokeMatches) == 0 {
		return nil
	}

	result := make([]ParsedToolCall, 0, len(invokeMatches))
	for _, m := range invokeMatches {
		if len(m) < 3 {
			continue
		}
		result = append(result, ParsedToolCall{
			Name: m[1],
			Args: decodeLooseArgs(m[2]),
			Raw:  m[0],
		})
	}
	return result
}

// FormatToolResult renders a tool_result block. The first non-empty callID
// value becomes tool_use_id; without one, tool_name carries the tool name.
func (f *AnthropicFormatter) FormatToolResult(name string, result *tools.Result, callID ...string) string {
	block := anthropicToolResult{Type: "tool_result"}
	if len(callID) > 0 && callID[0] != "" {
		block.ToolUseID = callID[0]
	} else {
		block.ToolName = name
	}
	if result.Success {
		block.Content = result.Output
	} else {
		block.Content = result.Error
		block.IsError = true
	}
	return anthropicMarshal(block)
}

func (f *AnthropicFormatter) ModelFamily() string {
	return "anthropic"
}

func (f *AnthropicFormatter) ValidateFormat(response string) error {
	if response == "" {
		return nil
	}

	blocks := anthropicToolUseBlocks(response)
	if len(blocks) == 0 {
		return f.validateInvoke(response)
	}

	seen := make(map[string]bool, len(blocks))
	for _, block := range blocks {
		if err := anthropicToolUseError(block); err != nil {
			return err
		}
		id := block["id"].(string)
		if seen[id] {
			return fmt.Errorf("duplicate tool_use id %q", id)
		}
		seen[id] = true
	}
	return nil
}

// validateInvoke validates the legacy <function_calls><invoke> form.
func (f *AnthropicFormatter) validateInvoke(response string) error {
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
