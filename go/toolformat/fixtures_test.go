package toolformat

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"testing"

	"github.com/haikeilabs/agentware/go/internal/conformance"
	"github.com/haikeilabs/agentware/go/tools"
)

// toolformatFixtures is the shared toolformat fixture set. Every
// <family>-cases.json file is replayed through that family's formatter so the
// language implementations stay in parity (see docs/testing.md).
const toolformatFixtures = "../../fixtures/toolformat"

// fixtureTool is a minimal tools.ExtendedTool built from the shared
// fixtures/toolformat/tools.json definitions.
type fixtureTool struct {
	name        string
	description string
	inputSchema map[string]any
}

func (f fixtureTool) Name() string                  { return f.name }
func (f fixtureTool) Description() string           { return f.description }
func (f fixtureTool) InputSchema() map[string]any   { return f.inputSchema }
func (f fixtureTool) Examples() []tools.ToolExample { return nil }
func (f fixtureTool) Execute(context.Context, map[string]any) (*tools.Result, error) {
	return &tools.Result{Success: true}, nil
}

// formattersByFamily maps a fixture's family field to the Go formatter that
// implements it. The generic family has no fixture file.
var formattersByFamily = map[string]ToolFormatter{
	"openai":    &OpenAIFOrmatter{},
	"glm":       &GLMFormatter{},
	"deepseek":  &DeepSeekFormatter{},
	"anthropic": &AnthropicFormatter{},
	"qwen":      &QwenFormatter{},
}

type toolformatFixture struct {
	Family            string `json:"family"`
	FormatDefinitions []struct {
		Name     string `json:"name"`
		Expected string `json:"expected"`
	} `json:"format_definitions"`
	ParseCalls []struct {
		Name     string `json:"name"`
		Input    string `json:"input"`
		Expected []struct {
			ID   string         `json:"id"`
			Name string         `json:"name"`
			Args map[string]any `json:"args"`
		} `json:"expected"`
	} `json:"parse_calls"`
	FormatResult []struct {
		Name     string `json:"name"`
		ToolName string `json:"tool_name"`
		Result   struct {
			Success bool   `json:"success"`
			Output  string `json:"output"`
			Error   string `json:"error"`
		} `json:"result"`
		Expected string `json:"expected"`
	} `json:"format_result"`
	ValidateFormat []struct {
		Name  string `json:"name"`
		Input string `json:"input"`
		Valid bool   `json:"valid"`
	} `json:"validate_format"`
}

// TestToolformatFixtures replays every case in every shared toolformat fixture
// through the matching Go formatter and records each case for the
// equal-coverage gate.
func TestToolformatFixtures(t *testing.T) {
	fixtureTools := loadFixtureTools(t)

	files, err := filepath.Glob(filepath.Join(toolformatFixtures, "*-cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(files) == 0 {
		t.Fatal("no toolformat fixture files found")
	}

	for _, file := range files {
		data, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		var fx toolformatFixture
		if err := json.Unmarshal(data, &fx); err != nil {
			t.Fatal(err)
		}
		formatter, ok := formattersByFamily[fx.Family]
		if !ok {
			t.Fatalf("%s: no Go formatter for family %q", file, fx.Family)
		}
		fixtureName := "toolformat/" + filepath.Base(file)

		for _, tc := range fx.FormatDefinitions {
			tc := tc
			t.Run(fx.Family+"/format_definitions/"+tc.Name, func(t *testing.T) {
				conformance.Record(t, fixtureName, "format_definitions", tc.Name)
				compareFormatDefinitions(t, tc.Expected, formatter.FormatToolDefinitions(fixtureTools))
			})
		}
		for _, tc := range fx.ParseCalls {
			tc := tc
			t.Run(fx.Family+"/parse_calls/"+tc.Name, func(t *testing.T) {
				conformance.Record(t, fixtureName, "parse_calls", tc.Name)
				calls, err := formatter.ParseToolCalls(tc.Input)
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				if len(calls) != len(tc.Expected) {
					t.Fatalf("got %d calls, want %d", len(calls), len(tc.Expected))
				}
				for i, want := range tc.Expected {
					got := calls[i]
					if got.Name != want.Name {
						t.Errorf("call %d: name = %q, want %q", i, got.Name, want.Name)
					}
					if got.ID != want.ID {
						t.Errorf("call %d: id = %q, want %q", i, got.ID, want.ID)
					}
					if !reflect.DeepEqual(got.Args, want.Args) {
						t.Errorf("call %d: args = %#v, want %#v", i, got.Args, want.Args)
					}
				}
			})
		}
		for _, tc := range fx.FormatResult {
			tc := tc
			t.Run(fx.Family+"/format_result/"+tc.Name, func(t *testing.T) {
				conformance.Record(t, fixtureName, "format_result", tc.Name)
				result := &tools.Result{Success: tc.Result.Success, Output: tc.Result.Output, Error: tc.Result.Error}
				if got := formatter.FormatToolResult(tc.ToolName, result); got != tc.Expected {
					t.Errorf("format_result = %q, want %q", got, tc.Expected)
				}
			})
		}
		for _, tc := range fx.ValidateFormat {
			tc := tc
			t.Run(fx.Family+"/validate_format/"+tc.Name, func(t *testing.T) {
				conformance.Record(t, fixtureName, "validate_format", tc.Name)
				err := formatter.ValidateFormat(tc.Input)
				if tc.Valid && err != nil {
					t.Errorf("expected valid, got error: %v", err)
				}
				if !tc.Valid && err == nil {
					t.Errorf("expected invalid, got no error")
				}
			})
		}
	}
}

// loadFixtureTools builds the shared tool definitions from tools.json.
func loadFixtureTools(t *testing.T) []tools.Tool {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(toolformatFixtures, "tools.json"))
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Tools []struct {
			Name        string         `json:"name"`
			Description string         `json:"description"`
			InputSchema map[string]any `json:"input_schema"`
		} `json:"tools"`
	}
	if err := json.Unmarshal(data, &file); err != nil {
		t.Fatal(err)
	}
	toolList := make([]tools.Tool, 0, len(file.Tools))
	for _, tt := range file.Tools {
		toolList = append(toolList, fixtureTool{name: tt.Name, description: tt.Description, inputSchema: tt.InputSchema})
	}
	return toolList
}

// compareFormatDefinitions compares a formatter's tool-definition output to the
// fixture's expected value. The fixture strings are written in a specific key
// order (matching the Python/TypeScript emitters), which Go's json.Marshal
// cannot reproduce because it sorts map keys. So when the expected value is
// JSON it is compared as a JSON value; otherwise (the qwen XML form) the
// embedded <parameters> JSON is normalized and the strings compared.
func compareFormatDefinitions(t *testing.T, expected, actual string) {
	t.Helper()
	var want any
	if err := json.Unmarshal([]byte(expected), &want); err == nil {
		var got any
		if err := json.Unmarshal([]byte(actual), &got); err != nil {
			t.Errorf("actual is not valid JSON: %v\nactual: %s", err, actual)
			return
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("format_definitions differ\n got:  %s\n want: %s", canonicalJSON(got), canonicalJSON(want))
		}
		return
	}
	if normalizeParams(expected) != normalizeParams(actual) {
		t.Errorf("format_definitions differ\n got:  %s\n want: %s", actual, expected)
	}
}

var paramsTagRe = regexp.MustCompile(`(<parameters>)(.*?)(</parameters>)`)

// normalizeParams re-marshals each embedded <parameters> JSON blob in canonical
// (key-sorted) order so two XML definitions that differ only in JSON key order
// compare equal.
func normalizeParams(s string) string {
	return paramsTagRe.ReplaceAllStringFunc(s, func(m string) string {
		sub := paramsTagRe.FindStringSubmatch(m)
		var v any
		if err := json.Unmarshal([]byte(sub[2]), &v); err != nil {
			return m
		}
		b, _ := json.Marshal(v)
		return sub[1] + string(b) + sub[3]
	})
}

func canonicalJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}
