package tools

import (
	"encoding/json"
	"sort"
)

type ToolRegistry struct {
	tools map[string]Tool
}

func NewToolRegistry() *ToolRegistry {
	return &ToolRegistry{
		tools: make(map[string]Tool),
	}
}

func (r *ToolRegistry) Register(t Tool) {
	r.tools[t.Name()] = t
}

func (r *ToolRegistry) Get(name string) (Tool, bool) {
	t, ok := r.tools[name]
	return t, ok
}

func (r *ToolRegistry) All() []Tool {
	names := make([]string, 0, len(r.tools))
	for name := range r.tools {
		names = append(names, name)
	}
	sort.Strings(names)

	result := make([]Tool, 0, len(names))
	for _, name := range names {
		result = append(result, r.tools[name])
	}
	return result
}

func (r *ToolRegistry) Names() []string {
	names := make([]string, 0, len(r.tools))
	for name := range r.tools {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func (r *ToolRegistry) Schemas() map[string]map[string]any {
	schemas := make(map[string]map[string]any)
	for _, t := range r.tools {
		if et, ok := t.(ExtendedTool); ok {
			schemas[t.Name()] = et.InputSchema()
		}
	}
	return schemas
}

// KeiToolManifestEntry is one tool in the Kei tool manifest, matching the
// catalog's POST /api/v1/tools create body (HAI-271). ID, workspace, org,
// version, and timestamps are server-generated and omitted from the export.
type KeiToolManifestEntry struct {
	Name        string   `json:"name"`
	Service     string   `json:"service"`
	Description string   `json:"description"`
	Action      string   `json:"action"`
	Resources   []string `json:"resources"`
	Enabled     bool     `json:"enabled"`
}

// KeiToolManifest is the top-level export object.
type KeiToolManifest struct {
	Tools []KeiToolManifestEntry `json:"tools"`
}

// ExportKeiToolManifest returns a JSON manifest of all governed tools in the
// registry. Only tools implementing GovernedTool are included. The output is
// deterministic (sorted by name). The manifest is what an admin loads into
// the Kei policy catalog's tool registry via the kei CLI or a skill.
func (r *ToolRegistry) ExportKeiToolManifest() ([]byte, error) {
	entries := make([]KeiToolManifestEntry, 0)
	names := make([]string, 0)
	for name, t := range r.tools {
		if _, ok := t.(GovernedTool); ok {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	for _, name := range names {
		t := r.tools[name].(GovernedTool)
		scope := t.KeiScope()
		entry := KeiToolManifestEntry{
			Name:        name,
			Service:     scope.Service,
			Description: t.Description(),
			Action:      scope.Action,
			Resources:   scope.Resources,
			Enabled:     true,
		}
		if entry.Resources == nil {
			entry.Resources = []string{}
		}
		entries = append(entries, entry)
	}
	manifest := KeiToolManifest{Tools: entries}
	return json.MarshalIndent(manifest, "", "  ")
}
