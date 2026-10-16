package proxy

import (
	"encoding/json"
	"testing"

	"github.com/haikeilabs/agentware/go/llm"
)

func TestToInternalMessages_Basic(t *testing.T) {
	openAI := MessageList{
		{"role": "system", "content": "You are a helpful assistant"},
		{"role": "user", "content": "Hello"},
	}

	messages := ToInternalMessages(openAI)

	if len(messages) != 2 {
		t.Fatalf("expected 2 messages, got %d", len(messages))
	}

	if messages[0].Role != llm.RoleSystem {
		t.Errorf("expected role system, got %s", messages[0].Role)
	}
	if messages[0].Content != "You are a helpful assistant" {
		t.Errorf("expected 'You are a helpful assistant', got %s", messages[0].Content)
	}

	if messages[1].Role != llm.RoleUser {
		t.Errorf("expected role user, got %s", messages[1].Role)
	}
	if messages[1].Content != "Hello" {
		t.Errorf("expected 'Hello', got %s", messages[1].Content)
	}
}

func TestToInternalMessages_WithToolCalls(t *testing.T) {
	openAI := MessageList{
		{
			"role": "assistant",
			"tool_calls": []any{
				map[string]any{
					"id":   "call_123",
					"type": "function",
					"function": map[string]any{
						"name":      "bash",
						"arguments": `{"command": "ls -la"}`,
					},
				},
			},
		},
	}

	messages := ToInternalMessages(openAI)

	if len(messages) != 1 {
		t.Fatalf("expected 1 message, got %d", len(messages))
	}

	if len(messages[0].ToolCalls) != 1 {
		t.Fatalf("expected 1 tool call, got %d", len(messages[0].ToolCalls))
	}

	if messages[0].ToolCalls[0].Name != "bash" {
		t.Errorf("expected tool name 'bash', got %s", messages[0].ToolCalls[0].Name)
	}
}

func TestToInternalMessages_WithToolResult(t *testing.T) {
	openAI := MessageList{
		{
			"role":         "tool",
			"tool_call_id": "call_123",
			"content":      "total 0",
		},
	}

	messages := ToInternalMessages(openAI)

	if len(messages) != 1 {
		t.Fatalf("expected 1 message, got %d", len(messages))
	}

	if messages[0].Role != llm.RoleTool {
		t.Errorf("expected role tool, got %s", messages[0].Role)
	}
	if messages[0].ToolCallID != "call_123" {
		t.Errorf("expected tool_call_id 'call_123', got %s", messages[0].ToolCallID)
	}
	if messages[0].Content != "total 0" {
		t.Errorf("expected content 'total 0', got %s", messages[0].Content)
	}
}

func TestToOpenAIMessages_Basic(t *testing.T) {
	internal := []llm.Message{
		{Role: llm.RoleSystem, Content: "You are helpful"},
		{Role: llm.RoleUser, Content: "Hi"},
	}

	openAI := ToOpenAIMessages(internal)

	if len(openAI) != 2 {
		t.Fatalf("expected 2 messages, got %d", len(openAI))
	}

	role0, _ := openAI[0]["role"].(string)
	if role0 != "system" {
		t.Errorf("expected role 'system', got %s", role0)
	}

	role1, _ := openAI[1]["role"].(string)
	if role1 != "user" {
		t.Errorf("expected role 'user', got %s", role1)
	}
}

func TestToInternalTools_Basic(t *testing.T) {
	openAI := ToolList{
		{
			"type": "function",
			"function": map[string]any{
				"name":        "bash",
				"description": "Execute a command",
				"parameters": map[string]any{
					"type": "object",
					"properties": map[string]any{
						"command": map[string]any{"type": "string"},
					},
				},
			},
		},
	}

	tools := ToInternalTools(openAI)

	if len(tools) != 1 {
		t.Fatalf("expected 1 tool, got %d", len(tools))
	}

	if tools[0].Name != "bash" {
		t.Errorf("expected name 'bash', got %s", tools[0].Name)
	}
	if tools[0].Description != "Execute a command" {
		t.Errorf("expected description 'Execute a command', got %s", tools[0].Description)
	}
}

func TestToOpenAITools_Basic(t *testing.T) {
	internal := []llm.ToolDefinition{
		{
			Name:        "read_file",
			Description: "Read a file",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"path": map[string]any{"type": "string"},
				},
			},
		},
	}

	openAI := ToOpenAITools(internal)

	if len(openAI) != 1 {
		t.Fatalf("expected 1 tool, got %d", len(openAI))
	}

	toolType, _ := openAI[0]["type"].(string)
	if toolType != "function" {
		t.Errorf("expected type 'function', got %s", toolType)
	}

	fn, _ := openAI[0]["function"].(map[string]any)
	name, _ := fn["name"].(string)
	if name != "read_file" {
		t.Errorf("expected name 'read_file', got %s", name)
	}
}

func TestInjectRespondTool_Nil(t *testing.T) {
	result := injectRespondTool(nil)

	if len(result) != 1 {
		t.Fatalf("expected 1 tool, got %d", len(result))
	}

	fn := result[0]["function"].(map[string]any)
	name := fn["name"].(string)
	if name != "respond" {
		t.Errorf("expected 'respond', got %s", name)
	}
}

func TestInjectRespondTool_WithTools(t *testing.T) {
	tools := ToolList{
		{
			"type": "function",
			"function": map[string]any{
				"name": "bash",
			},
		},
	}

	result := injectRespondTool(tools)

	if len(result) != 2 {
		t.Fatalf("expected 2 tools, got %d", len(result))
	}

	// First should be respond
	fn0 := result[0]["function"].(map[string]any)
	if fn0["name"] != "respond" {
		t.Errorf("first tool should be respond, got %s", fn0["name"])
	}

	// Second should be original
	fn1 := result[1]["function"].(map[string]any)
	if fn1["name"] != "bash" {
		t.Errorf("second tool should be bash, got %s", fn1["name"])
	}
}

func TestHasToolCalls_True(t *testing.T) {
	response := map[string]any{
		"choices": []any{
			map[string]any{
				"message": map[string]any{
					"tool_calls": []any{
						map[string]any{
							"id":   "call_1",
							"type": "function",
						},
					},
				},
			},
		},
	}

	if !HasToolCalls(response) {
		t.Error("expected true, got false")
	}
}

func TestHasToolCalls_False(t *testing.T) {
	response := map[string]any{
		"choices": []any{
			map[string]any{
				"message": map[string]any{
					"content": "Hello",
				},
			},
		},
	}

	if HasToolCalls(response) {
		t.Error("expected false, got true")
	}
}

func TestExtractToolCall(t *testing.T) {
	response := map[string]any{
		"choices": []any{
			map[string]any{
				"message": map[string]any{
					"tool_calls": []any{
						map[string]any{
							"id":   "call_123",
							"type": "function",
							"function": map[string]any{
								"name":      "bash",
								"arguments": `{"command": "ls"}`,
							},
						},
					},
				},
			},
		},
	}

	name, args, found := ExtractToolCall(response)

	if !found {
		t.Error("expected found=true")
	}
	if name != "bash" {
		t.Errorf("expected name 'bash', got %s", name)
	}
	if args["command"] != "ls" {
		t.Errorf("expected command 'ls', got %v", args["command"])
	}
}

func TestStripRespondTool(t *testing.T) {
	response := map[string]any{
		"choices": []any{
			map[string]any{
				"message": map[string]any{
					"content": "",
					"tool_calls": []any{
						map[string]any{
							"id":   "call_1",
							"type": "function",
							"function": map[string]any{
								"name":      "respond",
								"arguments": `{"message": "Task complete!"}`,
							},
						},
					},
				},
				"finish_reason": "tool_calls",
			},
		},
	}

	result := StripRespondTool(response)

	choices := result["choices"].([]any)
	msg := choices[0].(map[string]any)["message"].(map[string]any)

	if msg["content"] != "Task complete!" {
		t.Errorf("expected content 'Task complete!', got %v", msg["content"])
	}
}

func TestStripRespondTool_NoRespond(t *testing.T) {
	response := map[string]any{
		"choices": []any{
			map[string]any{
				"message": map[string]any{
					"content": "Hello",
					"tool_calls": []any{
						map[string]any{
							"id":   "call_1",
							"type": "function",
							"function": map[string]any{
								"name":      "bash",
								"arguments": `{"command": "ls"}`,
							},
						},
					},
				},
			},
		},
	}

	result := StripRespondTool(response)

	// Should be unchanged
	choices := result["choices"].([]any)
	msg := choices[0].(map[string]any)["message"].(map[string]any)
	_, hasToolCalls := msg["tool_calls"]
	if !hasToolCalls {
		t.Error("expected tool_calls to remain")
	}
}

func TestRoundTripConversion(t *testing.T) {
	original := MessageList{
		{"role": "system", "content": "You are helpful"},
		{"role": "user", "content": "Test"},
	}

	internal := ToInternalMessages(original)
	backToOpenAI := ToOpenAIMessages(internal)

	if len(original) != len(backToOpenAI) {
		t.Errorf("message count mismatch: %d vs %d", len(original), len(backToOpenAI))
	}

	origJSON, _ := json.Marshal(original)
	backJSON, _ := json.Marshal(backToOpenAI)
	if string(origJSON) != string(backJSON) {
		t.Errorf("JSON mismatch:\noriginal:   %s\nconverted: %s", origJSON, backJSON)
	}
}
