package llm

import (
	"encoding/json"
	"errors"
	"strings"
)

type anthropicBlocks struct {
	Before []json.RawMessage `json:"before"`
	After  []json.RawMessage `json:"after"`
}
type anthropicMetadata struct {
	Anthropic *anthropicBlocks `json:"anthropic"`
}

// ParseAnthropicResponse parses native Messages content blocks, retaining
// opaque surrounding blocks on their tool call without interpreting them.
func ParseAnthropicResponse(data []byte) (*Response, error) {
	var wire struct {
		Content    []json.RawMessage `json:"content"`
		StopReason string            `json:"stop_reason"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return nil, errors.New("invalid Anthropic response")
	}
	response := &Response{FinishReason: wire.StopReason}
	pending := []json.RawMessage{}
	var text strings.Builder
	for _, raw := range wire.Content {
		var block struct {
			Type  string          `json:"type"`
			ID    string          `json:"id"`
			Name  string          `json:"name"`
			Input json.RawMessage `json:"input"`
			Text  string          `json:"text"`
		}
		if err := json.Unmarshal(raw, &block); err != nil {
			return nil, errors.New("invalid Anthropic block")
		}
		if block.Type == "tool_use" {
			var input map[string]any
			if block.ID == "" || block.Name == "" || json.Unmarshal(block.Input, &input) != nil || input == nil {
				return nil, errors.New("invalid Anthropic tool_use")
			}
			call := ToolCall{ID: block.ID, Name: block.Name, Args: input}
			if len(pending) > 0 {
				call.Metadata, _ = json.Marshal(anthropicMetadata{Anthropic: &anthropicBlocks{Before: pending, After: []json.RawMessage{}}})
			}
			response.ToolCalls = append(response.ToolCalls, call)
			pending = []json.RawMessage{}
		} else {
			if block.Type == "text" {
				text.WriteString(block.Text)
			}
			pending = append(pending, append(json.RawMessage(nil), raw...))
		}
	}
	if len(response.ToolCalls) > 0 && len(pending) > 0 {
		last := &response.ToolCalls[len(response.ToolCalls)-1]
		meta := anthropicMetadata{Anthropic: &anthropicBlocks{Before: []json.RawMessage{}, After: []json.RawMessage{}}}
		if len(last.Metadata) > 0 {
			if err := json.Unmarshal(last.Metadata, &meta); err != nil {
				return nil, errors.New("invalid Anthropic metadata")
			}
		}
		meta.Anthropic.After = pending
		last.Metadata, _ = json.Marshal(meta)
	}
	response.Content = text.String()
	return response, nil
}

// FormatAnthropicTools renders native Anthropic tool declarations.
func FormatAnthropicTools(tools []ToolDefinition) []map[string]any {
	out := make([]map[string]any, 0, len(tools))
	for _, tool := range tools {
		schema := tool.InputSchema
		if len(schema) == 0 {
			schema = map[string]any{"type": "object", "properties": map[string]any{}}
		}
		out = append(out, map[string]any{"name": tool.Name, "description": tool.Description, "input_schema": schema})
	}
	return out
}

// FormatAnthropicMessages renders assistant tool uses and groups adjacent tool
// results into user messages. System instructions belong in the top-level
// system parameter and are rejected here instead of silently discarded.
func FormatAnthropicMessages(messages []Message) ([]map[string]any, error) {
	out := make([]map[string]any, 0, len(messages))
	for _, message := range messages {
		switch message.Role {
		case RoleSystem:
			return nil, errors.New("Anthropic system instructions must be top-level")
		case RoleTool:
			if message.ToolCallID == "" {
				return nil, errors.New("tool_result requires tool_call_id")
			}
			result := map[string]any{"type": "tool_result", "tool_use_id": message.ToolCallID, "content": message.Content}
			if len(out) > 0 {
				prev := out[len(out)-1]
				if blocks, ok := prev["content"].([]any); ok && prev["role"] == "user" {
					prev["content"] = append(blocks, result)
					continue
				}
			}
			out = append(out, map[string]any{"role": "user", "content": []any{result}})
		case RoleAssistant:
			if len(message.ToolCalls) == 0 {
				out = append(out, map[string]any{"role": "assistant", "content": message.Content})
				continue
			}
			saved := make([]*anthropicBlocks, len(message.ToolCalls))
			hasSaved := false
			for i, call := range message.ToolCalls {
				if len(call.Metadata) == 0 {
					continue
				}
				var raw map[string]json.RawMessage
				if err := json.Unmarshal(call.Metadata, &raw); err != nil || raw == nil {
					continue // metadata from another model family is opaque here
				}
				if _, ok := raw["anthropic"]; !ok {
					continue
				}
				var meta anthropicMetadata
				if err := json.Unmarshal(call.Metadata, &meta); err != nil {
					return nil, errors.New("invalid Anthropic metadata")
				}
				if meta.Anthropic != nil {
					saved[i] = meta.Anthropic
					hasSaved = true
				}
			}
			blocks := []any{}
			if !hasSaved && message.Content != "" {
				blocks = append(blocks, map[string]any{"type": "text", "text": message.Content})
			}
			for i, call := range message.ToolCalls {
				if saved[i] != nil {
					for _, b := range saved[i].Before {
						blocks = append(blocks, b)
					}
				}
				blocks = append(blocks, map[string]any{"type": "tool_use", "id": call.ID, "name": call.Name, "input": call.Args})
				if saved[i] != nil {
					for _, b := range saved[i].After {
						blocks = append(blocks, b)
					}
				}
			}
			out = append(out, map[string]any{"role": "assistant", "content": blocks})
		default:
			out = append(out, map[string]any{"role": string(message.Role), "content": message.Content})
		}
	}
	return out, nil
}
