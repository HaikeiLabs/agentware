package llm

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// newTestBackend returns a serverBackend pointed at a stub server that always
// replies with the given JSON body.
func newTestBackend(t *testing.T, body string) Backend {
	t.Helper()

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		// #nosec G104
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)

	return NewServerBackend(ServerConfig{
		BaseURL: srv.URL,
		Model:   "test-model",
		Timeout: 5 * time.Second,
	})
}

func TestServerBackend_Complete_CapturesCachedTokens(t *testing.T) {
	backend := newTestBackend(t, `{
		"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
		"usage": {
			"prompt_tokens": 100,
			"completion_tokens": 20,
			"total_tokens": 120,
			"prompt_tokens_details": {"cached_tokens": 64}
		}
	}`)

	resp, err := backend.Complete(context.Background(), &Request{
		Messages: []Message{{Role: RoleUser, Content: "hello"}},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if resp.UsageTokens.CachedTokens != 64 {
		t.Errorf("expected CachedTokens 64, got %d", resp.UsageTokens.CachedTokens)
	}
	if resp.UsageTokens.PromptTokens != 100 {
		t.Errorf("expected PromptTokens 100, got %d", resp.UsageTokens.PromptTokens)
	}
	if resp.UsageTokens.CompletionTokens != 20 {
		t.Errorf("expected CompletionTokens 20, got %d", resp.UsageTokens.CompletionTokens)
	}
	if resp.UsageTokens.TotalTokens != 120 {
		t.Errorf("expected TotalTokens 120, got %d", resp.UsageTokens.TotalTokens)
	}
}

// Backends that omit prompt_tokens_details must not fail; CachedTokens stays 0.
func TestServerBackend_Complete_MissingCachedTokens(t *testing.T) {
	backend := newTestBackend(t, `{
		"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
		"usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
	}`)

	resp, err := backend.Complete(context.Background(), &Request{
		Messages: []Message{{Role: RoleUser, Content: "hello"}},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if resp.UsageTokens.CachedTokens != 0 {
		t.Errorf("expected CachedTokens 0, got %d", resp.UsageTokens.CachedTokens)
	}
	if resp.UsageTokens.PromptTokens != 100 {
		t.Errorf("expected PromptTokens 100, got %d", resp.UsageTokens.PromptTokens)
	}
}

// assertExactKeys fails when got has any key outside want or misses any of
// them, so PascalCase or Meta keys cannot sneak onto the wire unnoticed.
func assertExactKeys(t *testing.T, label string, got map[string]any, want []string) {
	t.Helper()
	if len(got) != len(want) {
		t.Errorf("%s: expected %d keys %v, got %d: %v", label, len(want), want, len(got), got)
		return
	}
	for _, k := range want {
		if _, ok := got[k]; !ok {
			t.Errorf("%s: missing key %q (got %v)", label, k, got)
		}
	}
}

// TestServerBackend_Complete_WireFormat pins the request body shape against a
// fake HTTP server (HAI-959): exact top-level and per-message OpenAI
// snake_case keys (role, content, tool_calls/tool_call_id when present),
// tool calls with function.arguments as a JSON string, and no Meta or
// PascalCase keys anywhere.
func TestServerBackend_Complete_WireFormat(t *testing.T) {
	var captured []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("failed to read request body: %v", err)
			return
		}
		captured = body
		w.Header().Set("Content-Type", "application/json")
		// #nosec G104
		_, _ = w.Write([]byte(`{"choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`))
	}))
	t.Cleanup(srv.Close)

	step := 1
	backend := NewServerBackend(ServerConfig{
		BaseURL: srv.URL,
		Model:   "test-model",
		Timeout: 5 * time.Second,
	})

	_, err := backend.Complete(context.Background(), &Request{
		Messages: []Message{
			{Role: RoleSystem, Content: "system prompt", Meta: MessageMeta{Type: MessageTypeSystemPrompt}},
			{Role: RoleUser, Content: "hello", Meta: MessageMeta{Type: MessageTypeUserInput, StepIndex: &step}},
			{
				Role:    RoleAssistant,
				Content: "",
				Meta:    MessageMeta{Type: MessageTypeToolCall, StepIndex: &step},
				ToolCalls: []ToolCall{
					{ID: "call_1", Name: "bash", Args: map[string]any{"command": "ls -la"}},
					{ID: "call_2", Name: "no_args"},
				},
			},
			{Role: RoleTool, Content: "total 0", ToolCallID: "call_1", Meta: MessageMeta{Type: MessageTypeToolResult, StepIndex: &step}},
		},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var payload map[string]any
	if err := json.Unmarshal(captured, &payload); err != nil {
		t.Fatalf("request body is not JSON: %v\nbody: %s", err, captured)
	}

	assertExactKeys(t, "top-level payload", payload, []string{"model", "messages", "temperature", "max_tokens", "stop"})

	rawMessages, ok := payload["messages"].([]any)
	if !ok {
		t.Fatalf("messages is not an array: %v", payload["messages"])
	}
	if len(rawMessages) != 4 {
		t.Fatalf("expected 4 messages, got %d: %v", len(rawMessages), rawMessages)
	}

	wantKeys := [][]string{
		{"role", "content"},
		{"role", "content"},
		{"role", "content", "tool_calls"},
		{"role", "content", "tool_call_id"},
	}
	messages := make([]map[string]any, len(rawMessages))
	for i, raw := range rawMessages {
		msg, ok := raw.(map[string]any)
		if !ok {
			t.Fatalf("message %d is not an object: %v", i, raw)
		}
		messages[i] = msg
		assertExactKeys(t, fmt.Sprintf("message %d", i), msg, wantKeys[i])
	}

	// Role and content survive the mapping; Meta content does not leak.
	if messages[0]["role"] != "system" || messages[0]["content"] != "system prompt" {
		t.Errorf("message 0: unexpected role/content: %v", messages[0])
	}
	if messages[1]["role"] != "user" || messages[1]["content"] != "hello" {
		t.Errorf("message 1: unexpected role/content: %v", messages[1])
	}
	if messages[3]["role"] != "tool" || messages[3]["tool_call_id"] != "call_1" {
		t.Errorf("message 3: unexpected role/tool_call_id: %v", messages[3])
	}

	// tool_calls must be the OpenAI shape with function.arguments as a JSON
	// string.
	toolCalls, ok := messages[2]["tool_calls"].([]any)
	if !ok || len(toolCalls) != 2 {
		t.Fatalf("expected 2 tool calls on the assistant message, got %v", messages[2]["tool_calls"])
	}

	call0, ok := toolCalls[0].(map[string]any)
	if !ok {
		t.Fatalf("tool call 0 is not an object: %v", toolCalls[0])
	}
	assertExactKeys(t, "tool call 0", call0, []string{"id", "type", "function"})
	if call0["id"] != "call_1" || call0["type"] != "function" {
		t.Errorf("tool call 0: unexpected id/type: %v", call0)
	}
	fn0, ok := call0["function"].(map[string]any)
	if !ok {
		t.Fatalf("tool call 0 function is not an object: %v", call0["function"])
	}
	assertExactKeys(t, "tool call 0 function", fn0, []string{"name", "arguments"})
	if fn0["name"] != "bash" {
		t.Errorf("tool call 0: expected name 'bash', got %v", fn0["name"])
	}
	argsStr, ok := fn0["arguments"].(string)
	if !ok {
		t.Fatalf("tool call 0: arguments is not a JSON string: %v (%T)", fn0["arguments"], fn0["arguments"])
	}
	var args map[string]any
	if err := json.Unmarshal([]byte(argsStr), &args); err != nil {
		t.Fatalf("tool call 0: arguments is not valid JSON: %v", err)
	}
	if args["command"] != "ls -la" {
		t.Errorf("tool call 0: expected arguments command 'ls -la', got %v", args)
	}

	call1, ok := toolCalls[1].(map[string]any)
	if !ok {
		t.Fatalf("tool call 1 is not an object: %v", toolCalls[1])
	}
	assertExactKeys(t, "tool call 1", call1, []string{"id", "type", "function"})
	fn1, ok := call1["function"].(map[string]any)
	if !ok {
		t.Fatalf("tool call 1 function is not an object: %v", call1["function"])
	}
	if fn1["name"] != "no_args" {
		t.Errorf("tool call 1: expected name 'no_args', got %v", fn1["name"])
	}
	if fn1["arguments"] != "{}" {
		t.Errorf("tool call 1: expected arguments '{}' for nil Args, got %v", fn1["arguments"])
	}
}

// Tool arguments that cannot be marshaled must surface as an error, not a
// silently mangled request body.
func TestServerBackend_Complete_UnmarshalableToolArgs(t *testing.T) {
	backend := newTestBackend(t, `{"choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`)

	_, err := backend.Complete(context.Background(), &Request{
		Messages: []Message{{
			Role:      RoleAssistant,
			ToolCalls: []ToolCall{{ID: "call_1", Name: "bad", Args: map[string]any{"ch": make(chan int)}}},
		}},
	})
	if err == nil {
		t.Fatal("expected error for unmarshalable tool arguments, got nil")
	}
}
