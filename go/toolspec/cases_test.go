package toolspec

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
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
