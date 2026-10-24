// Copyright 2026 Haikei Labs
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package calltemplate_test

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/haikeilabs/agentware/go/kei/calltemplate"
	"github.com/haikeilabs/agentware/go/toolspec"
)

// loadFixture reads a contracts schema file for test use.
// It resolves the path by asking go list for the module directory,
// which works both locally and in CI (where the module is in the
// module cache after go mod download).
func loadFixture(t *testing.T, rel ...string) []byte {
	t.Helper()

	// Use 'go list -m -f {{.Dir}}' to find the contracts module directory.
	// This works on any machine where the module has been resolved.
	cmd := exec.Command("go", "list", "-m", "-f", "{{.Dir}}",
		"github.com/HaikeiLabs/kei-connector-contracts")
	cmd.Stderr = os.Stderr
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("go list -m for contracts: %v", err)
	}
	modDir := strings.TrimSpace(string(out))
	if modDir == "" {
		t.Fatal("go list returned empty module directory")
	}

	path := filepath.Join(append([]string{modDir}, rel...)...)
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("cannot read fixture %s from %s: %v",
			filepath.Join(rel...), modDir, err)
	}
	return data
}

func TestToToolSpecs_github_file_write(t *testing.T) {
	data := loadFixture(t, "schemas", "github.tool-definition.v1.json")
	tpls, err := calltemplate.Parse(data)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}

	specs, err := calltemplate.ToToolSpecs(tpls, "test-agent-id")
	if err != nil {
		t.Fatalf("ToToolSpecs: %v", err)
	}

	// Find github.file_write in the result.
	var found *toolspec.ToolSpec
	for i := range specs {
		if specs[i].Name == "github.file_write" {
			found = &specs[i]
			break
		}
	}
	if found == nil {
		t.Fatal("github.file_write not found among converted ToolSpecs")
	}

	if found.Description == "" {
		t.Error("description is empty")
	}
	if found.Service != "github" {
		t.Errorf("service = %q, want %q", found.Service, "github")
	}
	if found.Source != "github" {
		t.Errorf("source = %q, want %q", found.Source, "github")
	}
	if found.OperationClass != "write" {
		t.Errorf("operation_class = %q, want %q", found.OperationClass, "write")
	}
	if len(found.RequiredCapabilities) != 1 || found.RequiredCapabilities[0] != "file.write" {
		t.Errorf("required_capabilities = %v, want [file.write]", found.RequiredCapabilities)
	}
	if found.Route == nil {
		t.Fatal("route is nil")
	}
	if found.Route.ConnectorBinding == nil {
		t.Fatal("connector_binding is nil")
	}
	if found.Route.ConnectorBinding.AgentID != "test-agent-id" {
		t.Errorf("agent_id = %q, want %q", found.Route.ConnectorBinding.AgentID, "test-agent-id")
	}
	if found.Route.ConnectorBinding.ConnectorID == "" {
		t.Error("connector_id is empty")
	}
	// Parameters should be a JSON Schema object.
	if found.Parameters == nil {
		t.Error("parameters is nil")
	}
	if typ, ok := found.Parameters["type"]; !ok || typ != "object" {
		t.Errorf("parameters.type = %v, want object", typ)
	}
}

func TestToToolSpecs_permission_to_operation_class(t *testing.T) {
	data := loadFixture(t, "schemas", "github.tool-definition.v1.json")
	tpls, err := calltemplate.Parse(data)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}

	specs, err := calltemplate.ToToolSpecs(tpls, "test-agent-id")
	if err != nil {
		t.Fatalf("ToToolSpecs: %v", err)
	}

	for _, spec := range specs {
		// Find the matching template to check permission mapping.
		var matchedTpl *calltemplate.Template
		for _, tpl := range tpls {
			if tpl.Name == spec.Name {
				matchedTpl = &tpl
				break
			}
		}
		if matchedTpl == nil {
			t.Errorf("%s: no matching template found", spec.Name)
			continue
		}

		// RequiredCapabilities must contain the permission string.
		if len(spec.RequiredCapabilities) != 1 || spec.RequiredCapabilities[0] != matchedTpl.Permission {
			t.Errorf("%s: required_capabilities = %v, want [%s]",
				spec.Name, spec.RequiredCapabilities, matchedTpl.Permission)
		}

		// OperationClass must match the permission suffix.
		expectedOC := deriveExpectedClass(matchedTpl.Permission)
		if spec.OperationClass != expectedOC {
			t.Errorf("%s: operation_class = %q, want %q (from permission %q)",
				spec.Name, spec.OperationClass, expectedOC, matchedTpl.Permission)
		}
	}
}

// deriveExpectedClass returns the operation class expected from a permission string.
func deriveExpectedClass(permission string) string {
	if strings.HasSuffix(permission, ".write") {
		return "write"
	}
	if strings.HasSuffix(permission, ".read") {
		return "read"
	}
	if strings.Contains(permission, "write") {
		return "write"
	}
	return "read"
}

func TestToToolSpecs_missing_name(t *testing.T) {
	_, err := calltemplate.ToToolSpecs([]calltemplate.Template{
		{Name: "", Description: "no name", Schema: "kei.tool-definition/v1", Permission: "test.read"},
	}, "agent-id")
	if err == nil {
		t.Fatal("expected error for missing name, got nil")
	}
	if !strings.Contains(err.Error(), "name") {
		t.Errorf("error does not mention name: %v", err)
	}
}

func TestToToolSpecs_missing_permission(t *testing.T) {
	_, err := calltemplate.ToToolSpecs([]calltemplate.Template{
		{Name: "test.tool", Description: "no permission", Schema: "kei.tool-definition/v1"},
	}, "agent-id")
	if err == nil {
		t.Fatal("expected error for missing permission, got nil")
	}
	if !strings.Contains(err.Error(), "permission") {
		t.Errorf("error does not mention permission: %v", err)
	}
}

func TestParse_rejects_invalid_template(t *testing.T) {
	_, err := calltemplate.Parse([]byte(`[{"schema":"kei.tool-definition/v1","name":""}]`))
	if err == nil {
		t.Fatal("expected Parse error for invalid template, got nil")
	}
	// The contracts package should report all problems.
	if !strings.Contains(err.Error(), "name") {
		t.Errorf("error does not mention name: %v", err)
	}
}

func TestParse_rejects_non_array(t *testing.T) {
	_, err := calltemplate.Parse([]byte(`{"not":"an array"}`))
	if err == nil {
		t.Fatal("expected Parse error for non-array JSON, got nil")
	}
	if !strings.Contains(err.Error(), "array") {
		t.Errorf("error does not mention array: %v", err)
	}
}

func TestToToolSpecs_all_templates_round_trip_manifest(t *testing.T) {
	data := loadFixture(t, "schemas", "github.tool-definition.v1.json")
	tpls, err := calltemplate.Parse(data)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}

	specs, err := calltemplate.ToToolSpecs(tpls, "test-agent-id")
	if err != nil {
		t.Fatalf("ToToolSpecs: %v", err)
	}

	// MarshalManifestV3 must accept the output.
	manifest, err := toolspec.MarshalManifestV3(specs)
	if err != nil {
		t.Fatalf("MarshalManifestV3: %v", err)
	}

	// Verify it decodes back to a valid manifest.
	var parsed struct {
		Schema string            `json:"schema"`
		Tools  []json.RawMessage `json:"tools"`
	}
	if err := json.Unmarshal(manifest, &parsed); err != nil {
		t.Fatalf("manifest is not valid JSON: %v", err)
	}
	if parsed.Schema != "kei.tool-manifest/v3" {
		t.Errorf("schema = %q, want kei.tool-manifest/v3", parsed.Schema)
	}
	if len(parsed.Tools) == 0 {
		t.Fatal("manifest has 0 tools")
	}

	// Every tool should have the required wire fields.
	for i, raw := range parsed.Tools {
		var tool map[string]any
		if err := json.Unmarshal(raw, &tool); err != nil {
			t.Fatalf("tool %d: %v", i, err)
		}
		if tool["name"] == "" {
			t.Errorf("tool %d: name is empty", i)
		}
		if tool["service"] == "" {
			t.Errorf("tool %d: service is empty", i)
		}
		if tool["source"] == "" {
			t.Errorf("tool %d: source is empty", i)
		}
		oc, _ := tool["operation_class"].(string)
		if oc != "read" && oc != "write" {
			t.Errorf("tool %d: operation_class = %q", i, oc)
		}
		if tool["description"] == "" {
			t.Errorf("tool %d: description is empty", i)
		}
		if tool["enabled"] != true {
			t.Errorf("tool %d: enabled = %v, want true", i, tool["enabled"])
		}
		route, ok := tool["route"].(map[string]any)
		if !ok {
			t.Errorf("tool %d: route missing or not an object", i)
		} else if cb, ok2 := route["connector_binding"].(map[string]any); ok2 {
			if cb["agent_id"] != "test-agent-id" {
				t.Errorf("tool %d: agent_id = %v, want test-agent-id", i, cb["agent_id"])
			}
			if cb["connector_id"] == "" {
				t.Errorf("tool %d: connector_id is empty", i)
			}
		} else {
			t.Errorf("tool %d: route.connector_binding missing", i)
		}
		caps, _ := tool["required_capabilities"].([]any)
		if len(caps) == 0 {
			t.Errorf("tool %d: required_capabilities is empty", i)
		}
	}
}

// Test that a consumer can import only agentware (not contracts) and use the API.
func TestConsumerUsesAgentwareOnly(t *testing.T) {
	// This test verifies that the public API of go/kei/calltemplate does not
	// require importing the contracts package. We construct a Template literal
	// using only the agentware types and call ToToolSpecs.
	specs, err := calltemplate.ToToolSpecs([]calltemplate.Template{
		{
			Name:        "test.tool",
			Description: "A test tool using only agentware types",
			Schema:      "kei.tool-definition/v1",
			Permission:  "test.read",
			Service:     "test",
			Parameters:  json.RawMessage(`{"type":"object","properties":{"x":{"type":"string"}},"additionalProperties":false}`),
			Binding: &calltemplate.Binding{
				ConnectorID: "conn_test_1",
				Transport: &calltemplate.Transport{
					Method:      "GET",
					URLTemplate: "https://example.com/{x}",
					Args:        map[string]calltemplate.Arg{"x": {In: "path"}},
				},
			},
		},
	}, "agent-id")
	if err != nil {
		t.Fatalf("ToToolSpecs with agentware-only types: %v", err)
	}
	if len(specs) != 1 {
		t.Fatalf("got %d specs, want 1", len(specs))
	}
	if specs[0].Name != "test.tool" {
		t.Errorf("name = %q, want test.tool", specs[0].Name)
	}
	if specs[0].OperationClass != "read" {
		t.Errorf("operation_class = %q, want read", specs[0].OperationClass)
	}
	if len(specs[0].RequiredCapabilities) != 1 || specs[0].RequiredCapabilities[0] != "test.read" {
		t.Errorf("required_capabilities = %v, want [test.read]", specs[0].RequiredCapabilities)
	}
	if specs[0].Route.ConnectorBinding.AgentID != "agent-id" {
		t.Errorf("agent_id = %q, want agent-id", specs[0].Route.ConnectorBinding.AgentID)
	}
	if specs[0].Route.ConnectorBinding.ConnectorID != "conn_test_1" {
		t.Errorf("connector_id = %q, want conn_test_1", specs[0].Route.ConnectorBinding.ConnectorID)
	}
}
