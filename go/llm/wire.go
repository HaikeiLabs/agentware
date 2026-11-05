package llm

import (
	"encoding/json"
	"fmt"
	"strings"
)

// OpenAI-compatible wire types for the chat-completions request body. They are
// deliberately unexported: they exist only for the body serverBackend sends,
// and keep the exported Message/MessageMeta/ToolCall types free of JSON tags so
// their marshal behaviour does not change for other consumers. Meta is internal
// bookkeeping and is never serialized.
type wireToolCallFunction struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

type wireToolCall struct {
	ID       string               `json:"id"`
	Type     string               `json:"type"`
	Function wireToolCallFunction `json:"function"`
}

type wireMessage struct {
	Role       string         `json:"role"`
	Content    string         `json:"content"`
	ToolCallID string         `json:"tool_call_id,omitempty"`
	ToolCalls  []wireToolCall `json:"tool_calls,omitempty"`
}

// toWireMessages maps internal messages to the OpenAI chat-completions wire
// shape: snake_case keys, tool calls under tool_calls[].function with
// arguments as a JSON string, and Meta dropped (it never goes on the wire).
// It mirrors ToOpenAIMessages in go/llm/proxy/convert.go, which cannot be
// imported here because the proxy package imports llm.
func toWireMessages(messages []Message) ([]wireMessage, error) {
	wire := make([]wireMessage, 0, len(messages))

	for _, m := range messages {
		wm := wireMessage{
			Role:       string(m.Role),
			Content:    m.Content,
			ToolCallID: m.ToolCallID,
		}

		if len(m.ToolCalls) > 0 {
			wm.ToolCalls = make([]wireToolCall, 0, len(m.ToolCalls))
			for _, tc := range m.ToolCalls {
				args := "{}"
				if tc.Args != nil {
					argsBytes, err := json.Marshal(tc.Args)
					if err != nil {
						return nil, fmt.Errorf("tool call %q arguments: %w", tc.Name, err)
					}
					args = string(argsBytes)
				}
				wm.ToolCalls = append(wm.ToolCalls, wireToolCall{
					ID:   tc.ID,
					Type: "function",
					Function: wireToolCallFunction{
						Name:      tc.Name,
						Arguments: args,
					},
				})
			}
		}

		wire = append(wire, wm)
	}

	return wire, nil
}

// wireResponseToolCall is the OpenAI tool_calls entry in a chat-completions
// response: the name and arguments are nested under function, and arguments
// is usually a JSON string (some servers send an object).
type wireResponseToolCall struct {
	ID       string `json:"id"`
	Function struct {
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
	} `json:"function"`
}

// fromWireToolCalls maps response tool_calls to ToolCall, keeping the ID so
// the tool result can answer it with tool_call_id. Arguments that are not a
// JSON object (or a string holding one) are an error, so a malformed call is
// never run with empty arguments.
func fromWireToolCalls(calls []wireResponseToolCall) ([]ToolCall, error) {
	if len(calls) == 0 {
		return nil, nil
	}
	out := make([]ToolCall, 0, len(calls))
	for _, c := range calls {
		args, err := decodeWireArguments(c.Function.Arguments)
		if err != nil {
			return nil, fmt.Errorf("tool call %q arguments: %w", c.Function.Name, err)
		}
		out = append(out, ToolCall{ID: c.ID, Name: c.Function.Name, Args: args})
	}
	return out, nil
}

func decodeWireArguments(raw json.RawMessage) (map[string]any, error) {
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" || trimmed == "null" || trimmed == `""` {
		return map[string]any{}, nil
	}
	var args map[string]any
	if err := json.Unmarshal(raw, &args); err == nil && args != nil {
		return args, nil
	}
	var encoded string
	if err := json.Unmarshal(raw, &encoded); err != nil {
		return nil, fmt.Errorf("must be a JSON object or a string containing one: %w", err)
	}
	if strings.TrimSpace(encoded) == "" {
		return map[string]any{}, nil
	}
	if err := json.Unmarshal([]byte(encoded), &args); err != nil || args == nil {
		return nil, fmt.Errorf("arguments string is not a JSON object")
	}
	return args, nil
}
