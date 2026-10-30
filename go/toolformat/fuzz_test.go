package toolformat

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// fuzzFamilyModels maps a fixture family to a model name that GetFormatter
// routes to that family's formatter, so the fuzz target exercises every
// formatter through the selector.
var fuzzFamilyModels = map[string]string{
	"openai":    "gpt-4o",
	"glm":       "glm-4",
	"deepseek":  "deepseek-chat",
	"anthropic": "claude-3",
	"qwen":      "qwen-2",
}

// FuzzParseToolCalls fuzzes every formatter family, selected through
// GetFormatter, with the shared fixture inputs and arbitrary input.
//
// Properties: parsing never panics, and whenever ParseToolCalls returns
// without an error every returned call carries a non-empty name and
// arguments that marshal to a JSON object.
func FuzzParseToolCalls(f *testing.F) {
	files, err := filepath.Glob(filepath.Join(toolformatFixtures, "*-cases.json"))
	if err != nil {
		f.Fatal(err)
	}
	if len(files) == 0 {
		f.Fatal("no toolformat fixture files found")
	}
	for _, file := range files {
		data, err := os.ReadFile(file)
		if err != nil {
			f.Fatal(err)
		}
		var fx toolformatFixture
		if err := json.Unmarshal(data, &fx); err != nil {
			f.Fatal(err)
		}
		model, ok := fuzzFamilyModels[fx.Family]
		if !ok {
			f.Fatalf("%s: no selector model name for family %q", file, fx.Family)
		}
		for _, tc := range fx.ParseCalls {
			f.Add(model, tc.Input)
		}
	}
	// Seed the families without a fixture file plus a few edge shapes.
	for _, seed := range []struct{ model, response string }{
		{"llama-3", `<|python_tag|>get_weather({"location": "Tokyo"})`},
		{"mistral-7b", `[TOOL_CALLS][tool_call>get_weather: {"location": "Tokyo"}][/TOOL_CALLS]`},
		{"nemotron-70b", `[{"id":"1","name":"get_weather","arguments":{"location":"Tokyo"}}]`},
		{"gpt-4o", `[{"id":"1","name":"get_weather","arguments":"{\"location\":\"Tokyo\"}"}]`},
		{"gpt-4o", `[{"name":""}]`},
		{"gpt-4o", `[{"name":"x","arguments":null}]`},
		{"qwen-2", ``},
		{"minimax-7b", ``},
		{"some-model", `[{"name":"x","arguments":{"a":1}}]`},
		{"some-model", "not json at all"},
	} {
		f.Add(seed.model, seed.response)
	}

	f.Fuzz(func(t *testing.T, modelName, response string) {
		formatter := GetFormatter(modelName)
		calls, err := formatter.ParseToolCalls(response)
		if err != nil {
			return
		}
		for _, call := range calls {
			if call.Name == "" {
				t.Errorf("family %s: returned a call with an empty name", formatter.ModelFamily())
				continue
			}
			if call.Args == nil {
				t.Errorf("family %s: returned call %q with nil arguments", formatter.ModelFamily(), call.Name)
				continue
			}
			if _, err := json.Marshal(call.Args); err != nil {
				t.Errorf("family %s: arguments of call %q do not marshal: %v", formatter.ModelFamily(), call.Name, err)
			}
		}
	})
}

// TestParseToolCallsFuzzRegressions covers the inputs FuzzParseToolCalls
// found where a parser returned a malformed call without an error.
func TestParseToolCallsFuzzRegressions(t *testing.T) {
	tests := []struct {
		name      string
		formatter ToolFormatter
		input     string
		wantErr   bool
		wantCalls int
		wantArgs  map[string]any
	}{
		{
			name:      "generic rejects empty name",
			formatter: &GenericFormatter{},
			input:     `[{"name":"","arguments":{}}]`,
			wantErr:   true,
		},
		{
			name:      "generic rejects null arguments",
			formatter: &GenericFormatter{},
			input:     `[{"name":"my_tool","arguments":null}]`,
			wantErr:   true,
		},
		{
			name:      "generic rejects arguments string holding null",
			formatter: &GenericFormatter{},
			input:     `[{"name":"my_tool","arguments":"null"}]`,
			wantErr:   true,
		},
		{
			name:      "qwen carries null arguments as raw",
			formatter: &QwenFormatter{},
			input:     `<tool_call><tool name="my_tool">null</tool></tool_call>`,
			wantCalls: 1,
			wantArgs:  map[string]any{"_raw": "null"},
		},
		{
			name:      "nemotron rejects empty name",
			formatter: &NemotronFormatter{},
			input:     `[{"Arguments":{}}]`,
			wantErr:   true,
		},
		{
			name:      "minimax skips empty tool name",
			formatter: &MiniMaxFormatter{},
			input:     ``,
			wantCalls: 0,
		},
		{
			name:      "minimax skips null arguments",
			formatter: &MiniMaxFormatter{},
			input:     `[{"name":"my_tool","arguments":null}]`,
			wantCalls: 0,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			calls, err := tc.formatter.ParseToolCalls(tc.input)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected an error, got calls %+v", calls)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if len(calls) != tc.wantCalls {
				t.Fatalf("got %d calls, want %d", len(calls), tc.wantCalls)
			}
			if tc.wantArgs != nil && len(calls) == 1 && !reflect.DeepEqual(calls[0].Args, tc.wantArgs) {
				t.Errorf("args = %#v, want %#v", calls[0].Args, tc.wantArgs)
			}
		})
	}
}
