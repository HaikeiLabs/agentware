package tools

import (
	"encoding/json"
	"errors"
	"sort"
)

type ToolRegistry struct {
	tools map[string]Tool
}

func NewToolRegistry() *ToolRegistry {
	return &ToolRegistry{tools: make(map[string]Tool)}
}

func (r *ToolRegistry) Register(t Tool) { r.tools[t.Name()] = t }

func (r *ToolRegistry) Get(name string) (Tool, bool) {
	t, ok := r.tools[name]
	return t, ok
}

func (r *ToolRegistry) All() []Tool {
	names := r.Names()
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
		if extended, ok := t.(ExtendedTool); ok {
			schemas[t.Name()] = extended.InputSchema()
		}
	}
	return schemas
}

// KeiToolManifestEntry is a v2 tool declaration.
type KeiToolManifestEntry struct {
	Name                 string            `json:"name"`
	Source               string            `json:"source"`
	RequiredCapabilities []string          `json:"required_capabilities"`
	ResourceTypes        []KeiResourceType `json:"resource_types"`
	OperationClass       string            `json:"operation_class"`
	Service              string            `json:"service,omitempty"`
	Description          string            `json:"description"`
	Enabled              bool              `json:"enabled"`
}

// KeiToolManifest is the top-level v2 export object.
type KeiToolManifest struct {
	Schema string                 `json:"schema"`
	Tools  []KeiToolManifestEntry `json:"tools"`
}

// ManifestVersion selects the manifest wire schema.
type ManifestVersion int

const (
	ManifestV1 ManifestVersion = 1
	ManifestV2 ManifestVersion = 2
)

type legacyEntry struct {
	Name        string   `json:"name"`
	Service     string   `json:"service"`
	Description string   `json:"description"`
	Action      string   `json:"action"`
	Resources   []string `json:"resources"`
	Enabled     bool     `json:"enabled"`
}

type legacyManifest struct {
	Tools []legacyEntry `json:"tools"`
}

// ExportKeiToolManifest returns a deterministic v2 manifest. Pass ManifestV1
// explicitly for the transition export.
func (r *ToolRegistry) ExportKeiToolManifest(version ...ManifestVersion) ([]byte, error) {
	selected := ManifestV2
	if len(version) > 1 {
		return nil, errors.New("at most one manifest version may be selected")
	}
	if len(version) == 1 {
		selected = version[0]
	}
	if selected != ManifestV1 && selected != ManifestV2 {
		return nil, errors.New("unsupported manifest version")
	}

	names := make([]string, 0)
	for name, tool := range r.tools {
		if _, ok := tool.(GovernedTool); ok {
			names = append(names, name)
		}
	}
	sort.Strings(names)

	if selected == ManifestV1 {
		entries := make([]legacyEntry, 0, len(names))
		for _, name := range names {
			tool := r.tools[name].(GovernedTool)
			scope := tool.KeiScope()
			service := scope.Service
			if service == "" {
				service = scope.Source
			}
			resources := make([]string, 0, len(scope.ResourceTypes))
			for _, resource := range scope.ResourceTypes {
				resources = append(resources, resource.Type)
			}
			sort.Strings(resources)
			entries = append(entries, legacyEntry{
				Name: name, Service: service, Description: tool.Description(),
				Action: scope.OperationClass, Resources: resources, Enabled: true,
			})
		}
		return json.MarshalIndent(legacyManifest{Tools: entries}, "", "  ")
	}

	entries := make([]KeiToolManifestEntry, 0, len(names))
	for _, name := range names {
		tool := r.tools[name].(GovernedTool)
		scope := tool.KeiScope()
		capabilities := append([]string{}, scope.RequiredCapabilities...)
		sort.Strings(capabilities)
		resourceTypes := append([]KeiResourceType{}, scope.ResourceTypes...)
		sort.Slice(resourceTypes, func(i, j int) bool {
			if resourceTypes[i].Type == resourceTypes[j].Type {
				return resourceTypes[i].ParentType < resourceTypes[j].ParentType
			}
			return resourceTypes[i].Type < resourceTypes[j].Type
		})
		entries = append(entries, KeiToolManifestEntry{
			Name: name, Source: scope.Source, RequiredCapabilities: capabilities,
			ResourceTypes: resourceTypes, OperationClass: scope.OperationClass,
			Service: scope.Service, Description: tool.Description(), Enabled: true,
		})
	}
	return json.MarshalIndent(KeiToolManifest{Schema: "kei.tool-manifest/v2", Tools: entries}, "", "  ")
}
