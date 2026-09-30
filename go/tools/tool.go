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

// KeiScope declares which Kei service, action, and resource patterns a tool
// touches. Tools that declare a scope are included in the Kei tool manifest
// that an admin loads into the catalog's tool registry. The harness never
// passes authorize resources; the catalog decides resources from the scope.
type KeiScope struct {
	Service   string   `json:"service"`
	Action    string   `json:"action"`
	Resources []string `json:"resources"`
}

// GovernedTool is a Tool that declares its Kei governance scope. Only
// tools implementing GovernedTool are included in the exported manifest.
type GovernedTool interface {
	Tool
	KeiScope() KeiScope
}
