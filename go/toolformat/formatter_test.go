package toolformat

import (
	"context"
	"testing"

	"github.com/soypete/pedro-agentware/go/tools"
)

type mockToolForFormat struct {
	name        string
	description string
}

func (m *mockToolForFormat) Name() string        { return m.name }
func (m *mockToolForFormat) Description() string { return m.description }
func (m *mockToolForFormat) Execute(ctx context.Context, args map[string]any) (*tools.Result, error) {
	return &tools.Result{Success: true}, nil
}

func TestToolFormatterInterface(t *testing.T) {
	formatter := &QwenFormatter{}

	toolList := []tools.Tool{
		&mockToolForFormat{name: "test_tool", description: "A test tool"},
	}

	defs := formatter.FormatToolDefinitions(toolList)
	if defs == "" {
		t.Error("expected non-empty tool definitions")
	}
}

func TestQwenFormatToolDefinitions(t *testing.T) {
	formatter := &QwenFormatter{}

	t.Run("empty tools list", func(t *testing.T) {
		result := formatter.FormatToolDefinitions([]tools.Tool{})
		if result != "" {
			t.Errorf("expected empty string, got '%s'", result)
		}
	})

	t.Run("single tool", func(t *testing.T) {
		tool := &mockToolForFormat{name: "my_tool", description: "Does something"}
		result := formatter.FormatToolDefinitions([]tools.Tool{tool})
		if result == "" {
			t.Error("expected non-empty result")
		}
	})
}

func TestQwenParseToolCalls(t *testing.T) {
	formatter := &QwenFormatter{}

	t.Run("empty response", func(t *testing.T) {
		calls, err := formatter.ParseToolCalls("")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if calls != nil {
			t.Error("expected nil calls for empty response")
		}
	})

	t.Run("valid tool call", func(t *testing.T) {
		response := `<tool_call><tool name="my_tool">{"arg1": "value1"}</tool></tool_call>`
		calls, err := formatter.ParseToolCalls(response)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if len(calls) != 1 {
			t.Errorf("expected 1 call, got %d", len(calls))
		}
		if calls[0].Name != "my_tool" {
			t.Errorf("expected tool name 'my_tool', got '%s'", calls[0].Name)
		}
	})

	t.Run("no tool calls in response", func(t *testing.T) {
		response := "Just some regular text response"
		calls, err := formatter.ParseToolCalls(response)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if calls != nil {
			t.Error("expected nil calls")
		}
	})
}

func TestQwenFormatToolResult(t *testing.T) {
	formatter := &QwenFormatter{}

	t.Run("success result", func(t *testing.T) {
		result := &tools.Result{Success: true, Output: "executed successfully"}
		output := formatter.FormatToolResult("my_tool", result)
		if output == "" {
			t.Error("expected non-empty output")
		}
	})

	t.Run("error result", func(t *testing.T) {
		result := &tools.Result{Success: false, Error: "something failed"}
		output := formatter.FormatToolResult("my_tool", result)
		if output == "" {
			t.Error("expected non-empty output")
		}
	})
}

func TestQwenModelFamily(t *testing.T) {
	formatter := &QwenFormatter{}
	if formatter.ModelFamily() != "qwen" {
		t.Errorf("expected 'qwen', got '%s'", formatter.ModelFamily())
	}
}

func TestQwenValidateFormat(t *testing.T) {
	formatter := &QwenFormatter{}

	t.Run("valid", func(t *testing.T) {
		err := formatter.ValidateFormat(`<tool_call><tool name="get_weather">{"loc":"NYC"}</tool></tool_call>`)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	})

	t.Run("empty", func(t *testing.T) {
		err := formatter.ValidateFormat("")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	})

	t.Run("missing tool name attribute", func(t *testing.T) {
		err := formatter.ValidateFormat(`<tool_call><tool>{"loc":"NYC"}</tool></tool_call>`)
		if err == nil {
			t.Error("expected error for missing tool name attribute")
		}
	})

	t.Run("missing arguments", func(t *testing.T) {
		err := formatter.ValidateFormat(`<tool_call><tool name="t"></tool></tool_call>`)
		if err == nil {
			t.Error("expected error for missing arguments")
		}
	})

	t.Run("invalid JSON arguments", func(t *testing.T) {
		err := formatter.ValidateFormat(`<tool_call><tool name="t">bad-json</tool></tool_call>`)
		if err == nil {
			t.Error("expected error for invalid JSON arguments")
		}
	})
}

func TestAnthropicFormatToolDefinitions(t *testing.T) {
	formatter := &AnthropicFormatter{}

	t.Run("empty tools list", func(t *testing.T) {
		result := formatter.FormatToolDefinitions([]tools.Tool{})
		if result != "" {
			t.Errorf("expected empty string, got '%s'", result)
		}
	})

	t.Run("single tool", func(t *testing.T) {
		tool := &mockToolForFormat{name: "my_tool", description: "Does something"}
		result := formatter.FormatToolDefinitions([]tools.Tool{tool})
		if result == "" {
			t.Error("expected non-empty result")
		}
	})
}

func TestAnthropicParseToolCalls(t *testing.T) {
	formatter := &AnthropicFormatter{}

	t.Run("empty response", func(t *testing.T) {
		calls, err := formatter.ParseToolCalls("")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if calls != nil {
			t.Error("expected nil calls for empty response")
		}
	})

	t.Run("valid tool call", func(t *testing.T) {
		response := `<function_calls><invoke tool="my_tool">{"arg1": "value1"}</invoke></function_calls>`
		calls, err := formatter.ParseToolCalls(response)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if len(calls) != 1 {
			t.Errorf("expected 1 call, got %d", len(calls))
		}
		if calls[0].Name != "my_tool" {
			t.Errorf("expected tool name 'my_tool', got '%s'", calls[0].Name)
		}
	})

	t.Run("no tool calls in response", func(t *testing.T) {
		response := "Just some regular text response"
		calls, err := formatter.ParseToolCalls(response)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if calls != nil {
			t.Error("expected nil calls")
		}
	})

	t.Run("no function_calls block", func(t *testing.T) {
		response := "I'll look that up for you."
		calls, err := formatter.ParseToolCalls(response)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if calls != nil {
			t.Error("expected nil calls")
		}
	})
}

func TestAnthropicFormatToolResult(t *testing.T) {
	formatter := &AnthropicFormatter{}

	t.Run("success result", func(t *testing.T) {
		result := &tools.Result{Success: true, Output: "executed successfully"}
		output := formatter.FormatToolResult("my_tool", result)
		if output == "" {
			t.Error("expected non-empty output")
		}
	})

	t.Run("error result", func(t *testing.T) {
		result := &tools.Result{Success: false, Error: "something failed"}
		output := formatter.FormatToolResult("my_tool", result)
		if output == "" {
			t.Error("expected non-empty output")
		}
	})
}

func TestAnthropicModelFamily(t *testing.T) {
	formatter := &AnthropicFormatter{}
	if formatter.ModelFamily() != "anthropic" {
		t.Errorf("expected 'anthropic', got '%s'", formatter.ModelFamily())
	}
}

func TestAnthropicValidateFormat(t *testing.T) {
	formatter := &AnthropicFormatter{}

	t.Run("valid", func(t *testing.T) {
		err := formatter.ValidateFormat(`<function_calls><invoke tool="get_weather">{}</invoke></function_calls>`)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	})

	t.Run("empty", func(t *testing.T) {
		err := formatter.ValidateFormat("")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	})

	t.Run("missing tool attribute", func(t *testing.T) {
		err := formatter.ValidateFormat(`<function_calls><invoke>{}</invoke></function_calls>`)
		if err == nil {
			t.Error("expected error for missing tool attribute")
		}
	})
}

func TestDeepSeekModelFamily(t *testing.T) {
	formatter := &DeepSeekFormatter{}
	if formatter.ModelFamily() != "deepseek" {
		t.Errorf("expected 'deepseek', got '%s'", formatter.ModelFamily())
	}
}

func TestDeepSeekFormatToolDefinitions(t *testing.T) {
	formatter := &DeepSeekFormatter{}

	t.Run("empty tools list", func(t *testing.T) {
		result := formatter.FormatToolDefinitions([]tools.Tool{})
		if result != "" {
			t.Errorf("expected empty string, got '%s'", result)
		}
	})

	t.Run("single tool", func(t *testing.T) {
		tool := &mockToolForFormat{name: "my_tool", description: "Does something"}
		result := formatter.FormatToolDefinitions([]tools.Tool{tool})
		if result == "" {
			t.Error("expected non-empty result")
		}
	})
}

func TestDeepSeekParseToolCalls(t *testing.T) {
	formatter := &DeepSeekFormatter{}

	t.Run("empty response", func(t *testing.T) {
		calls, err := formatter.ParseToolCalls("")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if calls != nil {
			t.Error("expected nil calls for empty response")
		}
	})

	t.Run("valid tool call", func(t *testing.T) {
		response := `[{"id":"call_1","name":"my_tool","arguments":{"arg1":"value1"}}]`
		calls, err := formatter.ParseToolCalls(response)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if len(calls) != 1 {
			t.Errorf("expected 1 call, got %d", len(calls))
		}
		if calls[0].Name != "my_tool" {
			t.Errorf("expected tool name 'my_tool', got '%s'", calls[0].Name)
		}
	})
}

func TestDeepSeekFormatToolResult(t *testing.T) {
	formatter := &DeepSeekFormatter{}

	t.Run("success result", func(t *testing.T) {
		result := &tools.Result{Success: true, Output: "executed successfully"}
		output := formatter.FormatToolResult("my_tool", result)
		if output == "" {
			t.Error("expected non-empty output")
		}
	})

	t.Run("error result", func(t *testing.T) {
		result := &tools.Result{Success: false, Error: "something failed"}
		output := formatter.FormatToolResult("my_tool", result)
		if output == "" {
			t.Error("expected non-empty output")
		}
	})
}

func TestDeepSeekValidateFormat(t *testing.T) {
	formatter := &DeepSeekFormatter{}

	t.Run("valid", func(t *testing.T) {
		err := formatter.ValidateFormat(`[{"id":"c1","name":"get_weather","arguments":{"loc":"NYC"}}]`)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	})

	t.Run("empty", func(t *testing.T) {
		err := formatter.ValidateFormat("")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	})

	t.Run("invalid JSON", func(t *testing.T) {
		err := formatter.ValidateFormat("not json")
		if err == nil {
			t.Error("expected error for invalid JSON")
		}
	})

	t.Run("missing name", func(t *testing.T) {
		err := formatter.ValidateFormat(`[{"id":"c1","arguments":{"loc":"NYC"}}]`)
		if err == nil {
			t.Error("expected error for missing name")
		}
	})
}

func TestGLMModelFamily(t *testing.T) {
	formatter := &GLMFormatter{}
	if formatter.ModelFamily() != "glm" {
		t.Errorf("expected 'glm', got '%s'", formatter.ModelFamily())
	}
}

func TestGLMFormatToolDefinitions(t *testing.T) {
	formatter := &GLMFormatter{}

	t.Run("empty tools list", func(t *testing.T) {
		result := formatter.FormatToolDefinitions([]tools.Tool{})
		if result != "" {
			t.Errorf("expected empty string, got '%s'", result)
		}
	})

	t.Run("single tool", func(t *testing.T) {
		tool := &mockToolForFormat{name: "my_tool", description: "Does something"}
		result := formatter.FormatToolDefinitions([]tools.Tool{tool})
		if result == "" {
			t.Error("expected non-empty result")
		}
	})
}

func TestGLMParseToolCalls(t *testing.T) {
	formatter := &GLMFormatter{}

	t.Run("empty response", func(t *testing.T) {
		calls, err := formatter.ParseToolCalls("")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if calls != nil {
			t.Error("expected nil calls for empty response")
		}
	})

	t.Run("valid tool call", func(t *testing.T) {
		response := `[{"id":"call_1","name":"my_tool","arguments":{"arg1":"value1"}}]`
		calls, err := formatter.ParseToolCalls(response)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if len(calls) != 1 {
			t.Errorf("expected 1 call, got %d", len(calls))
		}
		if calls[0].Name != "my_tool" {
			t.Errorf("expected tool name 'my_tool', got '%s'", calls[0].Name)
		}
	})
}

func TestGLMFormatToolResult(t *testing.T) {
	formatter := &GLMFormatter{}

	t.Run("success result", func(t *testing.T) {
		result := &tools.Result{Success: true, Output: "executed successfully"}
		output := formatter.FormatToolResult("my_tool", result)
		if output == "" {
			t.Error("expected non-empty output")
		}
	})

	t.Run("error result", func(t *testing.T) {
		result := &tools.Result{Success: false, Error: "something failed"}
		output := formatter.FormatToolResult("my_tool", result)
		if output == "" {
			t.Error("expected non-empty output")
		}
	})
}

func TestGLMValidateFormat(t *testing.T) {
	formatter := &GLMFormatter{}

	t.Run("valid", func(t *testing.T) {
		err := formatter.ValidateFormat(`[{"id":"c1","name":"get_weather","arguments":{"loc":"NYC"}}]`)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	})

	t.Run("empty", func(t *testing.T) {
		err := formatter.ValidateFormat("")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	})

	t.Run("invalid JSON", func(t *testing.T) {
		err := formatter.ValidateFormat("bad")
		if err == nil {
			t.Error("expected error for invalid JSON")
		}
	})

	t.Run("missing name", func(t *testing.T) {
		err := formatter.ValidateFormat(`[{"id":"c1","arguments":{"loc":"NYC"}}]`)
		if err == nil {
			t.Error("expected error for missing name")
		}
	})
}

func TestOpenAIModelFamily(t *testing.T) {
	formatter := &OpenAIFOrmatter{}
	if formatter.ModelFamily() != "openai" {
		t.Errorf("expected 'openai', got '%s'", formatter.ModelFamily())
	}
}

func TestGetFormatter(t *testing.T) {
	tests := []struct {
		modelName string
		expected  string
	}{
		{"qwen2.5", "qwen"},
		{"Qwen2.5", "qwen"},
		{"qwq-32b", "qwen"},
		{"QwQ-32B", "qwen"},
		{"llama-3", "llama"},
		{"Llama-3", "llama"},
		{"mistral-large", "mistral"},
		{"Mistral-Large", "mistral"},
		{"gpt-4", "openai"},
		{"claude-3", "anthropic"},
		{"Claude-3.5", "anthropic"},
		{"deepseek-chat", "deepseek"},
		{"DeepSeek-R1", "deepseek"},
		{"glm-4", "glm"},
		{"GLM-4", "glm"},
		{"chatglm-3", "glm"},
		{"o1-preview", "openai"},
		{"o3-mini", "openai"},
	}

	for _, tt := range tests {
		formatter := GetFormatter(tt.modelName)
		if formatter.ModelFamily() != tt.expected {
			t.Errorf("expected '%s' for model '%s', got '%s'", tt.expected, tt.modelName, formatter.ModelFamily())
		}
	}
}
