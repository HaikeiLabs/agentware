package tools

import "context"

type Tool interface {
	Name() string
	Description() string
	Execute(ctx context.Context, args map[string]any) (*Result, error)
}

type ExtendedTool interface {
	Tool
	InputSchema() map[string]any
	Examples() []ToolExample
}

type ToolExample struct {
	Input       map[string]any
	Output      string
	Explanation string
}

// KeiScope declares the data source, capabilities, and resource types a tool uses.
type KeiScope struct {
	Source               string            `json:"source"`
	RequiredCapabilities []string          `json:"required_capabilities"`
	ResourceTypes        []KeiResourceType `json:"resource_types"`
	OperationClass       string            `json:"operation_class"`
	Service              string            `json:"service,omitempty"`
}

// KeiResourceType identifies a resource kind and optional parent kind.
type KeiResourceType struct {
	Type       string `json:"type"`
	ParentType string `json:"parent_type,omitempty"`
}

// GovernedTool is a Tool that declares its Kei governance scope. Only
// tools implementing GovernedTool are included in the exported manifest.
type GovernedTool interface {
	Tool
	KeiScope() KeiScope
}
