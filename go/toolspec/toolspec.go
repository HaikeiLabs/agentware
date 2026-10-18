// Package toolspec defines the language-neutral tool contract and its Kei
// tool-manifest v3 serialization.
package toolspec

import (
	"encoding/json"
	"errors"
	"fmt"
)

// ManifestSchemaV3 is the schema identifier for the runtime tool manifest.
const ManifestSchemaV3 = "kei.tool-manifest/v3"

// ToolSpec is the shared definition used by model formatting and runtime
// registration. Model formatters use Name, Description, and Parameters;
// registrars use the remaining wire metadata. ConnectorBinding.AgentID is
// catalog registration metadata, not the caller agent_id on authorize.
type ToolSpec struct {
	Name                 string         `json:"name"`
	Description          string         `json:"description"`
	Parameters           map[string]any `json:"parameters,omitempty"`
	Service              string         `json:"service,omitempty"`
	Source               string         `json:"source,omitempty"`
	OperationClass       string         `json:"operation_class,omitempty"`
	Route                *Route         `json:"route,omitempty"`
	RequiredCapabilities []string       `json:"required_capabilities,omitempty"`
	ResourceTypes        []ResourceType `json:"resource_types,omitempty"`
	Enabled              *bool          `json:"enabled,omitempty"`
}

// Route contains exactly one supported runtime route.
type Route struct {
	HarnessExecutor  *HarnessExecutor  `json:"harness_executor,omitempty"`
	ConnectorBinding *ConnectorBinding `json:"connector_binding,omitempty"`
}

// HarnessExecutor identifies a local harness registration.
type HarnessExecutor struct {
	Executor     string `json:"executor"`
	Registration string `json:"registration"`
}

// ConnectorBinding identifies a connector catalog binding.
type ConnectorBinding struct {
	AgentID     string `json:"agent_id"`
	ConnectorID string `json:"connector_id"`
}

// ResourceType declares the governed resource type and optional parent type.
type ResourceType struct {
	Type       string `json:"type"`
	ParentType string `json:"parent_type,omitempty"`
}

type manifestV3 struct {
	Schema string   `json:"schema"`
	Tools  []toolV3 `json:"tools"`
}

type toolV3 struct {
	Name                 string         `json:"name"`
	Service              string         `json:"service"`
	Source               string         `json:"source"`
	OperationClass       string         `json:"operation_class"`
	Route                Route          `json:"route"`
	RequiredCapabilities []string       `json:"required_capabilities,omitempty"`
	ResourceTypes        []ResourceType `json:"resource_types,omitempty"`
	Description          string         `json:"description"`
	Enabled              bool           `json:"enabled"`
}

// MarshalToolV3 validates and serializes one ToolSpec to its manifest v3 wire
// object. Parameters are model metadata and are intentionally omitted.
func MarshalToolV3(spec ToolSpec) ([]byte, error) {
	wire, err := toWire(spec)
	if err != nil {
		return nil, err
	}
	return json.Marshal(wire)
}

// MarshalManifestV3 validates and serializes ToolSpecs as a manifest v3.
func MarshalManifestV3(specs []ToolSpec) ([]byte, error) {
	manifest := manifestV3{Schema: ManifestSchemaV3, Tools: make([]toolV3, 0, len(specs))}
	for i, spec := range specs {
		wire, err := toWire(spec)
		if err != nil {
			return nil, fmt.Errorf("tool %d (%q): %w", i, spec.Name, err)
		}
		manifest.Tools = append(manifest.Tools, wire)
	}
	return json.Marshal(manifest)
}

var errInvalidSpec = errors.New("invalid ToolSpec for manifest v3")

func toWire(spec ToolSpec) (toolV3, error) {
	if spec.Name == "" {
		return toolV3{}, fmt.Errorf("%w: name is required", errInvalidSpec)
	}
	if spec.Description == "" {
		return toolV3{}, fmt.Errorf("%w: description is required", errInvalidSpec)
	}
	if spec.Service == "" {
		return toolV3{}, fmt.Errorf("%w: service is required", errInvalidSpec)
	}
	if spec.Source == "" {
		return toolV3{}, fmt.Errorf("%w: source is required", errInvalidSpec)
	}
	if spec.OperationClass != "read" && spec.OperationClass != "write" {
		return toolV3{}, fmt.Errorf("%w: operation_class must be read or write", errInvalidSpec)
	}
	if spec.Route == nil {
		return toolV3{}, fmt.Errorf("%w: route is required", errInvalidSpec)
	}
	route := *spec.Route
	if (route.HarnessExecutor == nil) == (route.ConnectorBinding == nil) {
		return toolV3{}, fmt.Errorf("%w: route must contain exactly one route kind", errInvalidSpec)
	}
	if route.HarnessExecutor != nil {
		if route.HarnessExecutor.Executor == "" || route.HarnessExecutor.Registration == "" {
			return toolV3{}, fmt.Errorf("%w: harness_executor executor and registration are required", errInvalidSpec)
		}
		if len(spec.RequiredCapabilities) > 0 || len(spec.ResourceTypes) > 0 {
			return toolV3{}, fmt.Errorf("%w: harness_executor routes omit connector-only required_capabilities and resource_types", errInvalidSpec)
		}
	}
	if route.ConnectorBinding != nil {
		if route.ConnectorBinding.AgentID == "" || route.ConnectorBinding.ConnectorID == "" {
			return toolV3{}, fmt.Errorf("%w: connector_binding agent_id and connector_id are required", errInvalidSpec)
		}
		if len(spec.RequiredCapabilities) == 0 {
			return toolV3{}, fmt.Errorf("%w: connector_binding required_capabilities must not be empty", errInvalidSpec)
		}
	}
	enabled := true
	if spec.Enabled != nil {
		enabled = *spec.Enabled
	}
	return toolV3{Name: spec.Name, Service: spec.Service, Source: spec.Source, OperationClass: spec.OperationClass, Route: route, RequiredCapabilities: spec.RequiredCapabilities, ResourceTypes: spec.ResourceTypes, Description: spec.Description, Enabled: enabled}, nil
}
