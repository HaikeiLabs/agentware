package toolspec_test

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/haikeilabs/agentware/go/internal/conformance"
	"github.com/haikeilabs/agentware/go/toolspec"
)

// manifestV3Fixtures is the shared loader fixture set; its README.md is the
// loader contract every language must pass.
const manifestV3Fixtures = "../../fixtures/toolspec/manifest-v3"

func TestManifestV3LoaderFixtures(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(manifestV3Fixtures, "index.json"))
	if err != nil {
		t.Fatal(err)
	}
	var index struct {
		Valid []struct {
			Name      string `json:"name"`
			Input     string `json:"input"`
			Expected  string `json:"expected"`
			Canonical string `json:"canonical"`
		} `json:"valid"`
		Invalid []struct {
			Name  string `json:"name"`
			Input string `json:"input"`
			Error string `json:"error"`
		} `json:"invalid"`
	}
	if err := json.Unmarshal(data, &index); err != nil {
		t.Fatal(err)
	}
	if len(index.Valid) == 0 || len(index.Invalid) == 0 {
		t.Fatalf("index must list valid and invalid cases: valid=%d invalid=%d", len(index.Valid), len(index.Invalid))
	}

	for _, tc := range index.Valid {
		t.Run("valid/"+tc.Name, func(t *testing.T) {
			conformance.Record(t, "toolspec/manifest-v3/index.json", "valid", tc.Name)
			specs, err := toolspec.LoadManifestV3(filepath.Join(manifestV3Fixtures, tc.Input))
			if err != nil {
				t.Fatalf("LoadManifestV3: %v", err)
			}
			got := normalizedManifest(specs)
			want, err := readFixtureJSON(tc.Expected)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("loaded specs differ from expected\n got:  %s\n want: %s", marshalForDiff(got), marshalForDiff(want))
			}
			out, err := toolspec.MarshalManifestV3(specs)
			if err != nil {
				t.Fatalf("MarshalManifestV3: %v", err)
			}
			var roundTripped any
			if err := json.Unmarshal(out, &roundTripped); err != nil {
				t.Fatal(err)
			}
			canonical, err := readFixtureJSON(tc.Canonical)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(roundTripped, canonical) {
				t.Errorf("round trip differs from canonical\n got:  %s\n want: %s", out, marshalForDiff(canonical))
			}
		})
	}

	for _, tc := range index.Invalid {
		t.Run("invalid/"+tc.Name, func(t *testing.T) {
			conformance.Record(t, "toolspec/manifest-v3/index.json", "invalid", tc.Name)
			data, err := os.ReadFile(filepath.Join(manifestV3Fixtures, tc.Input))
			if err != nil {
				t.Fatal(err)
			}
			_, err = toolspec.ParseManifestV3(data)
			if err == nil {
				t.Fatal("ParseManifestV3 accepted an invalid manifest")
			}
			var me *toolspec.ManifestError
			if !errors.As(err, &me) {
				t.Fatalf("error %v is not a *toolspec.ManifestError", err)
			}
			if me.Code != tc.Error {
				t.Errorf("error code = %q, want %q (error: %v)", me.Code, tc.Error, err)
			}
		})
	}
}

// normalizedManifest renders loaded specs in the README's normalized expected
// form, {"tools": [...]}, so nil versus empty does not differ by language.
// Values use the same dynamic types json.Unmarshal produces (maps are
// map[string]any, arrays are []any) so the comparison with the parsed
// expected file is a plain reflect.DeepEqual.
func normalizedManifest(specs []toolspec.ToolSpec) any {
	tools := make([]any, 0, len(specs))
	for _, spec := range specs {
		enabled := true
		if spec.Enabled != nil {
			enabled = *spec.Enabled
		}
		parameters := spec.Parameters
		if parameters == nil {
			parameters = map[string]any{}
		}
		capabilities := make([]any, 0, len(spec.RequiredCapabilities))
		for _, c := range spec.RequiredCapabilities {
			capabilities = append(capabilities, c)
		}
		resourceTypes := make([]any, 0, len(spec.ResourceTypes))
		for _, rt := range spec.ResourceTypes {
			entry := map[string]any{"type": rt.Type}
			if rt.ParentType != "" {
				entry["parent_type"] = rt.ParentType
			}
			resourceTypes = append(resourceTypes, entry)
		}
		route := map[string]any{}
		if spec.Route != nil {
			if spec.Route.HarnessExecutor != nil {
				route["harness_executor"] = map[string]any{
					"executor":     spec.Route.HarnessExecutor.Executor,
					"registration": spec.Route.HarnessExecutor.Registration,
				}
			}
			if spec.Route.ConnectorBinding != nil {
				route["connector_binding"] = map[string]any{
					"agent_id":     spec.Route.ConnectorBinding.AgentID,
					"connector_id": spec.Route.ConnectorBinding.ConnectorID,
				}
			}
		}
		tools = append(tools, map[string]any{
			"name":                  spec.Name,
			"description":           spec.Description,
			"parameters":            parameters,
			"service":               spec.Service,
			"source":                spec.Source,
			"operation_class":       spec.OperationClass,
			"route":                 route,
			"required_capabilities": capabilities,
			"resource_types":        resourceTypes,
			"enabled":               enabled,
		})
	}
	return map[string]any{"tools": tools}
}

func readFixtureJSON(path string) (any, error) {
	data, err := os.ReadFile(filepath.Join(manifestV3Fixtures, path))
	if err != nil {
		return nil, err
	}
	var v any
	if err := json.Unmarshal(data, &v); err != nil {
		return nil, err
	}
	return v, nil
}

func marshalForDiff(v any) string {
	data, err := json.Marshal(v)
	if err != nil {
		return "<unmarshalable>"
	}
	return string(data)
}
