package toolspec_test

import (
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/haikeilabs/agentware/go/internal/conformance"
	"github.com/haikeilabs/agentware/go/toolformat"
	"github.com/haikeilabs/agentware/go/toolspec"
)

func TestCasesV1ReplayManifestWire(t *testing.T) {
	data, err := os.ReadFile("../../fixtures/toolspec/cases.v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases struct {
		Cases []struct {
			Name         string          `json:"name"`
			ExpectedWire json.RawMessage `json:"expected_wire"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	wireData, err := os.ReadFile("../../fixtures/kei/tool-manifest.v3.json")
	if err != nil {
		t.Fatal(err)
	}
	var manifest struct {
		Tools []json.RawMessage `json:"tools"`
	}
	if err := json.Unmarshal(wireData, &manifest); err != nil {
		t.Fatal(err)
	}
	if len(cases.Cases) < 4 {
		t.Fatalf("got %d cases, want at least 4", len(cases.Cases))
	}
	foundRoutes := map[string]bool{}
	matchedManifestTools := 0
	for _, tc := range cases.Cases {
		if len(tc.ExpectedWire) == 0 {
			continue
		}
		var got any
		if err := json.Unmarshal(tc.ExpectedWire, &got); err != nil {
			t.Fatalf("%s expected wire: %v", tc.Name, err)
		}
		matched := false
		for _, manifestTool := range manifest.Tools {
			var want any
			if err := json.Unmarshal(manifestTool, &want); err != nil {
				t.Fatal(err)
			}
			if reflect.DeepEqual(got, want) {
				matched = true
				matchedManifestTools++
				break
			}
		}
		if !matched {
			t.Errorf("%s expected wire does not equal a tool in tool-manifest.v3.json", tc.Name)
		}
		var tool map[string]any
		if err := json.Unmarshal(tc.ExpectedWire, &tool); err != nil {
			t.Fatal(err)
		}
		route, _ := tool["route"].(map[string]any)
		if route["harness_executor"] != nil {
			foundRoutes["harness_executor"] = true
		}
		if route["connector_binding"] != nil {
			foundRoutes["connector_binding"] = true
		}
	}
	if matchedManifestTools != 2 || !foundRoutes["harness_executor"] || !foundRoutes["connector_binding"] {
		t.Fatalf("cases must include both manifest routes; matched=%d routes=%v", matchedManifestTools, foundRoutes)
	}
}

func TestMarshalManifestV3UsesContractCases(t *testing.T) {
	data, err := os.ReadFile("../../fixtures/toolspec/cases.v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var input struct {
		Cases []struct {
			Name          string            `json:"name"`
			Spec          toolspec.ToolSpec `json:"spec"`
			ExpectedWire  json.RawMessage   `json:"expected_wire"`
			ExpectedError string            `json:"expected_error"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(data, &input); err != nil {
		t.Fatal(err)
	}
	for _, tc := range input.Cases {
		conformance.Record(t, "toolspec/cases.v1.json", "cases", tc.Name)
		if tc.ExpectedError != "" {
			_, err := toolspec.MarshalManifestV3([]toolspec.ToolSpec{tc.Spec})
			if err == nil || !strings.Contains(err.Error(), tc.ExpectedError) {
				t.Errorf("expected %q error, got %v", tc.ExpectedError, err)
			}
			continue
		}
		if len(tc.ExpectedWire) == 0 {
			continue
		}
		got, err := toolspec.MarshalToolV3(tc.Spec)
		if err != nil {
			t.Fatalf("marshal %s: %v", tc.Spec.Name, err)
		}
		var gotValue, wantValue any
		if err := json.Unmarshal(got, &gotValue); err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(tc.ExpectedWire, &wantValue); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(gotValue, wantValue) {
			t.Errorf("wire for %s differs\ngot:  %s\nwant: %s", tc.Spec.Name, got, tc.ExpectedWire)
		}
	}
}

func TestGenericToolSpecFormatterUsesModelFieldsOnly(t *testing.T) {
	var cases struct {
		Compare map[string]string `json:"toolformat_compare"`
		Cases   []struct {
			Name     string            `json:"name"`
			Spec     toolspec.ToolSpec `json:"spec"`
			Expected map[string]string `json:"expected_toolformat"`
		} `json:"cases"`
	}
	data, err := os.ReadFile("../../fixtures/toolspec/cases.v1.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	formatters := map[string]toolformat.ToolSpecFormatter{
		"generic": &toolformat.GenericFormatter{}, "openai": &toolformat.OpenAIFOrmatter{},
		"deepseek": &toolformat.DeepSeekFormatter{}, "glm": &toolformat.GLMFormatter{},
		"anthropic": &toolformat.AnthropicFormatter{}, "qwen": &toolformat.QwenFormatter{},
	}
	for _, tc := range cases.Cases {
		for flavor, expected := range tc.Expected {
			formatter := formatters[flavor]
			if formatter == nil {
				t.Fatalf("no formatter registered for fixture flavor %q", flavor)
			}
			got := formatter.FormatToolSpecs([]toolspec.ToolSpec{tc.Spec})
			switch cases.Compare[flavor] {
			case "json":
				var gotJSON, expectedJSON any
				if err := json.Unmarshal([]byte(got), &gotJSON); err != nil {
					t.Errorf("%s/%s output is not JSON: %v", tc.Name, flavor, err)
					continue
				}
				if err := json.Unmarshal([]byte(expected), &expectedJSON); err != nil {
					t.Fatalf("%s/%s fixture is not JSON: %v", tc.Name, flavor, err)
				}
				if !reflect.DeepEqual(gotJSON, expectedJSON) {
					t.Errorf("%s/%s JSON output differs semantically: got %s, want %s", tc.Name, flavor, got, expected)
				}
			case "text":
				if got != expected {
					t.Errorf("%s/%s text output differs: got %q, want %q", tc.Name, flavor, got, expected)
				}
			default:
				t.Fatalf("missing or invalid comparison mode for %q: %q", flavor, cases.Compare[flavor])
			}
		}
	}
}

func TestMarshalManifestV3DefaultsEnabledAndOmitsParameters(t *testing.T) {
	data, err := toolspec.MarshalToolV3(toolspec.ToolSpec{
		Name: "local.tool", Description: "Local only", Parameters: map[string]any{"type": "object"},
		Service: "local", Source: "local", OperationClass: "write",
		Route: &toolspec.Route{HarnessExecutor: &toolspec.HarnessExecutor{Executor: "test", Registration: "local.tool"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	var wire map[string]any
	if err := json.Unmarshal(data, &wire); err != nil {
		t.Fatal(err)
	}
	if wire["enabled"] != true {
		t.Errorf("enabled = %v, want true", wire["enabled"])
	}
	if _, ok := wire["parameters"]; ok {
		t.Error("model parameters leaked into manifest wire")
	}
}

func TestMarshalManifestV3EnforcesRouteSpecificFields(t *testing.T) {
	base := toolspec.ToolSpec{Name: "t", Description: "d", Service: "s", Source: "s", OperationClass: "read"}
	t.Run("connector requires capabilities", func(t *testing.T) {
		spec := base
		spec.Route = &toolspec.Route{ConnectorBinding: &toolspec.ConnectorBinding{AgentID: "a", ConnectorID: "c"}}
		if _, err := toolspec.MarshalToolV3(spec); err == nil || !strings.Contains(err.Error(), "required_capabilities must not be empty") {
			t.Fatalf("got %v", err)
		}
	})
	t.Run("harness omits connector fields", func(t *testing.T) {
		spec := base
		spec.Route = &toolspec.Route{HarnessExecutor: &toolspec.HarnessExecutor{Executor: "h", Registration: "r"}}
		spec.RequiredCapabilities = []string{"x"}
		if _, err := toolspec.MarshalToolV3(spec); err == nil || !strings.Contains(err.Error(), "omit connector-only") {
			t.Fatalf("got %v", err)
		}
	})
}
