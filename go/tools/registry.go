package tools

import (
	"encoding/json"
	"errors"
	"sort"
	"strings"
)

type ToolRegistry struct {
	tools         map[string]Tool
	registrations map[string]KeiToolRegistration
}

func NewToolRegistry() *ToolRegistry {
	return &ToolRegistry{tools: make(map[string]Tool), registrations: make(map[string]KeiToolRegistration)}
}

// Register associates optional trusted route metadata with the same entry used for dispatch.
func (r *ToolRegistry) Register(t Tool, registration ...KeiToolRegistration) {
	r.tools[t.Name()] = t
	delete(r.registrations, t.Name())
	if len(registration) > 0 {
		r.registrations[t.Name()] = registration[0]
	}
}

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
	ManifestV3 ManifestVersion = 3
)

// KeiToolRegistration is trusted route metadata stored alongside a dispatch entry.
type KeiToolRegistration struct {
	Service              string            `json:"service"`
	Source               string            `json:"source"`
	OperationClass       string            `json:"operation_class"`
	Route                ToolRoute         `json:"route"`
	RequiredCapabilities []string          `json:"required_capabilities,omitempty"`
	ResourceTypes        []KeiResourceType `json:"resource_types,omitempty"`
}

type ToolRoute struct {
	ConnectorBinding *ConnectorBindingRoute `json:"connector_binding,omitempty"`
	HarnessExecutor  *HarnessExecutorRoute  `json:"harness_executor,omitempty"`
}
type ConnectorBindingRoute struct {
	AgentID     string `json:"agent_id"`
	ConnectorID string `json:"connector_id"`
}
type HarnessExecutorRoute struct {
	Executor     string `json:"executor"`
	Registration string `json:"registration"`
}

type manifestV3 struct {
	Schema string            `json:"schema"`
	Tools  []manifestV3Entry `json:"tools"`
}
type manifestV3Entry struct {
	Name                 string            `json:"name"`
	Service              string            `json:"service"`
	Source               string            `json:"source"`
	OperationClass       string            `json:"operation_class"`
	Route                ToolRoute         `json:"route"`
	RequiredCapabilities []string          `json:"required_capabilities,omitempty"`
	ResourceTypes        []KeiResourceType `json:"resource_types,omitempty"`
	Description          string            `json:"description"`
	Enabled              bool              `json:"enabled"`
}

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

// ExportKeiToolManifest returns deterministic v2 by default; select v1 or v3 explicitly.
func (r *ToolRegistry) ExportKeiToolManifest(version ...ManifestVersion) ([]byte, error) {
	selected := ManifestV2
	if len(version) > 1 {
		return nil, errors.New("at most one manifest version may be selected")
	}
	if len(version) == 1 {
		selected = version[0]
	}
	if selected != ManifestV1 && selected != ManifestV2 && selected != ManifestV3 {
		return nil, errors.New("unsupported manifest version")
	}

	if selected == ManifestV3 {
		return r.exportV3()
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

func (r *ToolRegistry) exportV3() ([]byte, error) {
	names := make([]string, 0, len(r.registrations))
	for name := range r.registrations {
		names = append(names, name)
	}
	sort.Strings(names)
	entries := make([]manifestV3Entry, 0, len(names))
	for _, name := range names {
		reg := r.registrations[name]
		if strings.TrimSpace(reg.Service) == "" || strings.TrimSpace(reg.Source) == "" {
			return nil, errors.New("v3 registration requires non-empty service and source")
		}
		if reg.OperationClass != "read" && reg.OperationClass != "write" {
			return nil, errors.New("invalid operation_class")
		}
		connector, harness := reg.Route.ConnectorBinding, reg.Route.HarnessExecutor
		if (connector == nil) == (harness == nil) {
			return nil, errors.New("route must contain exactly one branch")
		}
		entry := manifestV3Entry{Name: name, Service: reg.Service, Source: reg.Source, OperationClass: reg.OperationClass, Route: reg.Route, Description: r.tools[name].Description(), Enabled: true}
		if connector != nil {
			if strings.TrimSpace(connector.AgentID) == "" || strings.TrimSpace(connector.ConnectorID) == "" || len(reg.RequiredCapabilities) == 0 {
				return nil, errors.New("connector route requires binding and non-empty capabilities")
			}
			for _, cap := range reg.RequiredCapabilities {
				if strings.TrimSpace(cap) == "" {
					return nil, errors.New("connector capability must be non-empty")
				}
			}
			entry.RequiredCapabilities = append([]string{}, reg.RequiredCapabilities...)
			sort.Strings(entry.RequiredCapabilities)
			entry.ResourceTypes = append([]KeiResourceType{}, reg.ResourceTypes...)
			sort.Slice(entry.ResourceTypes, func(i, j int) bool {
				if entry.ResourceTypes[i].Type == entry.ResourceTypes[j].Type {
					return entry.ResourceTypes[i].ParentType < entry.ResourceTypes[j].ParentType
				}
				return entry.ResourceTypes[i].Type < entry.ResourceTypes[j].Type
			})
		} else if strings.TrimSpace(harness.Executor) == "" || strings.TrimSpace(harness.Registration) == "" || reg.RequiredCapabilities != nil || reg.ResourceTypes != nil {
			return nil, errors.New("harness route requires identity and omits connector-only fields")
		}
		entries = append(entries, entry)
	}
	return json.MarshalIndent(manifestV3{Schema: "kei.tool-manifest/v3", Tools: entries}, "", "  ")
}
