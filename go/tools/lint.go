package tools

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

type lintManifest struct {
	Schema string           `json:"schema"`
	Tools  []map[string]any `json:"tools"`
}

var resourceIdentifier = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_.-]*$`)
var retiredKeys = map[string]bool{"allow": true, "approval": true, "approval_id": true, "approval_required": true, "requires_approval": true}

// LintKeiToolManifest returns the v2 manifest rule diagnostics in stable order.
func LintKeiToolManifest(raw []byte, capabilityFile string) []string {
	var manifest lintManifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return []string{"invalid JSON: " + err.Error()}
	}
	if manifest.Schema != "kei.tool-manifest/v2" {
		return []string{"schema must be kei.tool-manifest/v2"}
	}
	var caps map[string][]string
	if capabilityFile == "" {
		capabilityFile = filepath.Join("fixtures", "kei", "connector-capabilities.v0.5.0.json")
		if _, err := os.Stat(capabilityFile); err != nil {
			capabilityFile = filepath.Join("..", capabilityFile)
		}
	}
	data, err := os.ReadFile(capabilityFile)
	if err != nil {
		return []string{"cannot read capability table: " + err.Error()}
	}
	if err := json.Unmarshal(data, &caps); err != nil {
		return []string{"invalid capability table"}
	}
	var errors []string
	seen := map[string]bool{}
	names := make([]string, 0, len(manifest.Tools))
	for i, tool := range manifest.Tools {
		prefix := fmt.Sprintf("tools[%d]", i)
		name, _ := tool["name"].(string)
		names = append(names, name)
		if seen[name] {
			errors = append(errors, "duplicate tool name: "+name)
		}
		seen[name] = true
		source, _ := tool["source"].(string)
		if source == "" {
			errors = append(errors, prefix+" is missing source")
		}
		values, ok := tool["required_capabilities"].([]any)
		if !ok || len(values) == 0 {
			errors = append(errors, prefix+" is missing required_capabilities")
		} else {
			capNames := make([]string, 0, len(values))
			for _, rawCap := range values {
				cap, _ := rawCap.(string)
				capNames = append(capNames, cap)
				if !contains(caps[source], cap) {
					errors = append(errors, fmt.Sprintf("%s capability %q is not declared for source %q", prefix, cap, source))
				}
			}
			if !sort.StringsAreSorted(capNames) {
				errors = append(errors, prefix+" required_capabilities are not sorted")
			}
		}
		resources, _ := tool["resource_types"].([]any)
		resourceKeys := make([]string, 0, len(resources))
		for _, value := range resources {
			item, _ := value.(map[string]any)
			typeName, _ := item["type"].(string)
			parentName, _ := item["parent_type"].(string)
			resourceKeys = append(resourceKeys, typeName+"\x00"+parentName)
			for _, key := range []string{"type", "parent_type"} {
				v, exists := item[key]
				if key == "parent_type" && !exists {
					continue
				}
				text, _ := v.(string)
				if !resourceIdentifier.MatchString(text) {
					errors = append(errors, fmt.Sprintf("%s invalid resource type %s: %v", prefix, key, v))
				}
			}
		}
		if !sort.StringsAreSorted(resourceKeys) {
			errors = append(errors, prefix+" resource_types are not sorted")
		}
		if tool["operation_class"] != "read" && tool["operation_class"] != "write" {
			errors = append(errors, prefix+" operation_class must be read or write")
		}
		if hasRetired(tool) {
			errors = append(errors, prefix+" contains allow or a retired approval field")
		}
	}
	if !sort.StringsAreSorted(names) {
		errors = append(errors, "tools are not sorted by name")
	}
	sort.Strings(errors)
	return errors
}
func contains(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}
func hasRetired(value any) bool {
	switch item := value.(type) {
	case map[string]any:
		for key, child := range item {
			if retiredKeys[strings.ToLower(key)] || strings.Contains(strings.ToLower(key), "approval") || hasRetired(child) {
				return true
			}
		}
	case []any:
		for _, child := range item {
			if hasRetired(child) {
				return true
			}
		}
	case string:
		return strings.EqualFold(item, "allow")
	}
	return false
}
