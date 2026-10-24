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

// Package calltemplate wraps kei-connector-contracts/calltemplate so harnesses
// can validate tool call templates, convert them to [toolspec.ToolSpec] for
// one-shot manifest registration, and (in a future change) build provider
// requests from the same entries after an allow.
//
// The public API exposes only agentware types — callers never import
// kei-connector-contracts directly.
package calltemplate

import (
	"encoding/json"
	"io/fs"
	"strings"

	contracts "github.com/HaikeiLabs/kei-connector-contracts/calltemplate"
)

// Template is one validated tool call template. Fields match
// kei-connector-contracts/calltemplate.Template exactly so the type alias
// is transparent.
type Template = contracts.Template

// Binding is the non-secret routing metadata of a template.
type Binding = contracts.Binding

// Transport describes the provider call.
type Transport = contracts.Transport

// Auth is credential placement only; the value is injected after the allow.
type Auth = contracts.Auth

// Arg maps a tool parameter onto the provider call.
type Arg = contracts.Arg

// Parse validates a template file body (a JSON array of templates) and returns
// the templates. Every problem found is reported, joined into one error.
func Parse(data []byte) ([]Template, error) {
	return contracts.Parse(data)
}

// LoadFile reads and validates a template file.
func LoadFile(path string) ([]Template, error) {
	return contracts.LoadFile(path)
}

// ParseFS reads and validates a template file from an fs.FS.
func ParseFS(fsys fs.FS, path string) ([]Template, error) {
	data, err := fs.ReadFile(fsys, path)
	if err != nil {
		return nil, err
	}
	return Parse(data)
}

// parseParameters converts a template's Parameters (which may be a parameter
// list or a JSON Schema object) into a map[string]any JSON Schema object
// suitable for toolspec.ToolSpec.Parameters.
func parseParameters(raw json.RawMessage) (map[string]any, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	// Try as a JSON Schema object first.
	var schema map[string]any
	if err := json.Unmarshal(raw, &schema); err == nil {
		if typ, ok := schema["type"]; ok && typ == "object" {
			return schema, nil
		}
		// It's a valid object but not type: object. That's fine — return as-is
		// for model formatting. The contracts validator already rejected
		// non-object schemas without additionalProperties: false.
		return schema, nil
	}
	// Try as a parameter list: [{name, type, description, required}, ...]
	var params []struct {
		Name        string          `json:"name"`
		Type        string          `json:"type"`
		Description string          `json:"description,omitempty"`
		Required    bool            `json:"required,omitempty"`
		Enum        []string        `json:"enum,omitempty"`
		Default     json.RawMessage `json:"default,omitempty"`
	}
	if err := json.Unmarshal(raw, &params); err == nil && len(params) > 0 {
		return paramListToSchema(params), nil
	}
	return nil, nil
}

// paramListToSchema converts a parameter list to a JSON Schema object.
func paramListToSchema(params []struct {
	Name        string          `json:"name"`
	Type        string          `json:"type"`
	Description string          `json:"description,omitempty"`
	Required    bool            `json:"required,omitempty"`
	Enum        []string        `json:"enum,omitempty"`
	Default     json.RawMessage `json:"default,omitempty"`
}) map[string]any {
	props := make(map[string]any, len(params))
	var required []string
	for _, p := range params {
		prop := make(map[string]any)
		if p.Type != "" {
			prop["type"] = p.Type
		} else {
			prop["type"] = "string"
		}
		if p.Description != "" {
			prop["description"] = p.Description
		}
		if len(p.Enum) > 0 {
			prop["enum"] = p.Enum
		}
		if p.Default != nil && string(p.Default) != "null" {
			var val any
			if json.Unmarshal(p.Default, &val) == nil {
				prop["default"] = val
			}
		}
		props[p.Name] = prop
		if p.Required {
			required = append(required, p.Name)
		}
	}
	schema := map[string]any{
		"type":       "object",
		"properties": props,
	}
	if len(required) > 0 {
		schema["required"] = required
	}
	schema["additionalProperties"] = false
	return schema
}

// permissionOperationClass derives the operation class from a permission string.
// Convention: if the permission ends with ".read" → "read", with ".write" → "write".
// Otherwise the last dot-separated component is checked.
func permissionOperationClass(permission string) string {
	if permission == "" {
		return "read"
	}
	parts := strings.Split(permission, ".")
	last := parts[len(parts)-1]
	switch last {
	case "read":
		return "read"
	case "write":
		return "write"
	default:
		// Fallback: check if the whole string contains a hint.
		if strings.Contains(permission, "write") {
			return "write"
		}
		return "read"
	}
}
