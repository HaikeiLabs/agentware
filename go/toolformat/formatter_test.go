package toolformat

import (
	"context"
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/haikeilabs/agentware/go/tools"
	"github.com/haikeilabs/agentware/go/toolspec"
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

func TestFormattersAcceptToolSpecs(t *testing.T) {
	spec := toolspec.ToolSpec{
		Name: "get_weather", Description: "Get current weather",
		Parameters: map[string]any{"type": "object", "properties": map[string]any{"location": map[string]any{"type": "string"}}},
		Service:    "weather", Source: "weather", OperationClass: "read",
	}
	formatters := []ToolSpecFormatter{
		&GenericFormatter{}, &OpenAIFOrmatter{}, &LlamaFormatter{}, &QwenFormatter{},
		&AnthropicFormatter{}, &NemotronFormatter{}, &MistralFormatter{}, &MiniMaxFormatter{},
	}
	for _, formatter := range formatters {
		got := formatter.FormatToolSpecs([]toolspec.ToolSpec{spec})
		for _, want := range []string{"get_weather", "Get current weather", "location"} {
			if !strings.Contains(got, want) {
				t.Errorf("%T output %q does not contain %q", formatter, got, want)
			}
		}
		if strings.Contains(got, "operation_class") || strings.Contains(got, "service") {
			t.Errorf("%T emitted registration metadata: %q", formatter, got)
		}
	}
}

type toolSpecLegacyEquivalent struct {
	spec toolspec.ToolSpec
}

func (t toolSpecLegacyEquivalent) Name() string                { return t.spec.Name }
func (t toolSpecLegacyEquivalent) Description() string         { return t.spec.Description }
func (t toolSpecLegacyEquivalent) InputSchema() map[string]any { return t.spec.Parameters }
func (toolSpecLegacyEquivalent) Examples() []tools.ToolExample { return nil }
func (toolSpecLegacyEquivalent) Execute(context.Context, map[string]any) (*tools.Result, error) {
	return &tools.Result{Success: true}, nil
}

// jsonCallFormatters returns the formatters whose tool calls arrive as JSON
// with an `arguments` field (object or string-encoded object). GLM, DeepSeek
// and OpenAI share GenericFormatter's implementation; Nemotron duplicates it.
func jsonCallFormatters() []ToolFormatter {
	return []ToolFormatter{
		&GenericFormatter{}, &GLMFormatter{}, &DeepSeekFormatter{}, &OpenAIFOrmatter{}, &NemotronFormatter{},
	}
}

// TestParseToolCallsDecodesStringArguments pins the GLM-5.3-Flash shape seen
// on vLLM (fixtures/toolformat/glm-cases.json): function.arguments may arrive
// as a JSON string containing the object, and must be decoded instead of
// silently dropping the call.
func TestParseToolCallsDecodesStringArguments(t *testing.T) {
	response := `[{"id":"chatcmpl-tool-b740f94d8f903c3d","name":"get_weather","arguments":"{\"location\": \"Tokyo\"}"}]`
	wantArgs := map[string]any{"location": "Tokyo"}

	for _, formatter := range jsonCallFormatters() {
		calls, err := formatter.ParseToolCalls(response)
		if err != nil {
			t.Fatalf("%T: unexpected error: %v", formatter, err)
		}
		if len(calls) != 1 {
			t.Fatalf("%T: expected 1 call, got %d", formatter, len(calls))
		}
		got := calls[0]
		if got.ID != "chatcmpl-tool-b740f94d8f903c3d" || got.Name != "get_weather" {
			t.Errorf("%T: got id=%q name=%q", formatter, got.ID, got.Name)
		}
		if !reflect.DeepEqual(got.Args, wantArgs) {
			t.Errorf("%T: args = %#v, want %#v", formatter, got.Args, wantArgs)
		}
	}
}

// TestParseToolCallsMalformedArgumentsStringReturnsError asserts fail-closed
// behaviour: an arguments value that is neither a JSON object nor a JSON
// string containing one must surface as an error, never a silently empty
// call list.
func TestParseToolCallsMalformedArgumentsStringReturnsError(t *testing.T) {
	responses := []string{
		`[{"id":"c1","name":"get_weather","arguments":"{not json"}]`,
		`[{"id":"c1","name":"get_weather","arguments":"[1,2]"}]`,
	}
	for _, formatter := range jsonCallFormatters() {
		for _, response := range responses {
			calls, err := formatter.ParseToolCalls(response)
			if err == nil {
				t.Errorf("%T: expected error for %q, got calls %v", formatter, response, calls)
			}
		}
	}
}

// TestToolformatFixtureParseCalls replays the shared GLM toolformat fixture
// (fixtures/toolformat/glm-cases.json) through the Go formatters so the
// language implementations stay in parity.
func TestToolformatFixtureParseCalls(t *testing.T) {
	data, err := os.ReadFile("../../fixtures/toolformat/glm-cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Family     string `json:"family"`
		ParseCalls []struct {
			Name     string `json:"name"`
			Input    string `json:"input"`
			Expected []struct {
				ID   string         `json:"id"`
				Name string         `json:"name"`
				Args map[string]any `json:"args"`
			} `json:"expected"`
		} `json:"parse_calls"`
		ValidateFormat []struct {
			Name  string `json:"name"`
			Input string `json:"input"`
			Valid bool   `json:"valid"`
		} `json:"validate_format"`
	}
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatal(err)
	}
	if fixture.Family != "glm" {
		t.Fatalf("expected glm fixture, got family %q", fixture.Family)
	}

	formatters := map[string]ToolFormatter{
		"generic": &GenericFormatter{},
		"glm":     &GLMFormatter{},
	}
	for flavor, formatter := range formatters {
		for _, tc := range fixture.ParseCalls {
			calls, err := formatter.ParseToolCalls(tc.Input)
			if err != nil {
				t.Fatalf("%s/%s: unexpected error: %v", flavor, tc.Name, err)
			}
			if len(calls) != len(tc.Expected) {
				t.Errorf("%s/%s: got %d calls, want %d", flavor, tc.Name, len(calls), len(tc.Expected))
				continue
			}
			for i, want := range tc.Expected {
				got := calls[i]
				if got.ID != want.ID || got.Name != want.Name {
					t.Errorf("%s/%s call %d: got id=%q name=%q, want id=%q name=%q", flavor, tc.Name, i, got.ID, got.Name, want.ID, want.Name)
				}
				if !reflect.DeepEqual(got.Args, want.Args) {
					t.Errorf("%s/%s call %d: args = %#v, want %#v", flavor, tc.Name, i, got.Args, want.Args)
				}
			}
		}
		for _, tc := range fixture.ValidateFormat {
			err := formatter.ValidateFormat(tc.Input)
			if tc.Valid && err != nil {
				t.Errorf("%s/%s: expected valid, got error: %v", flavor, tc.Name, err)
			}
			if !tc.Valid && err == nil {
				t.Errorf("%s/%s: expected invalid, got no error", flavor, tc.Name)
			}
		}
	}
}

func TestToolSpecFormattingMatchesLegacyToolDefinitionForEveryFlavor(t *testing.T) {
	data, err := os.ReadFile("../../fixtures/toolspec/cases.v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases struct {
		Cases []struct {
			Name string            `json:"name"`
			Spec toolspec.ToolSpec `json:"spec"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	formatters := []struct {
		name   string
		legacy ToolFormatter
		spec   ToolSpecFormatter
	}{
		{"generic", &GenericFormatter{}, &GenericFormatter{}},
		{"openai", &OpenAIFOrmatter{}, &OpenAIFOrmatter{}},
		{"deepseek", &DeepSeekFormatter{}, &DeepSeekFormatter{}},
		{"glm", &GLMFormatter{}, &GLMFormatter{}},
		{"anthropic", &AnthropicFormatter{}, &AnthropicFormatter{}},
		{"qwen", &QwenFormatter{}, &QwenFormatter{}},
		{"llama", &LlamaFormatter{}, &LlamaFormatter{}},
		{"mistral", &MistralFormatter{}, &MistralFormatter{}},
		{"nemotron", &NemotronFormatter{}, &NemotronFormatter{}},
		{"minimax", &MiniMaxFormatter{}, &MiniMaxFormatter{}},
	}
	for _, tc := range cases.Cases {
		legacy := toolSpecLegacyEquivalent{spec: tc.Spec}
		for _, formatter := range formatters {
			got := formatter.spec.FormatToolSpecs([]toolspec.ToolSpec{tc.Spec})
			want := formatter.legacy.FormatToolDefinitions([]tools.Tool{legacy})
			if got != want {
				t.Errorf("case %q flavor %s: ToolSpec output differs from equivalent ToolDefinition\ngot: %q\nwant: %q", tc.Name, formatter.name, got, want)
			}
		}
	}
}
