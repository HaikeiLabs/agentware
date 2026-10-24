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

// ManifestVersion selects the manifest wire schema.
type ManifestVersion int

const ManifestV3 ManifestVersion = 3

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

// ExportKeiToolManifest returns the registry's v3 tool manifest.
func (r *ToolRegistry) ExportKeiToolManifest(version ...ManifestVersion) ([]byte, error) {
	if len(version) > 1 || (len(version) == 1 && version[0] != ManifestV3) {
		return nil, errors.New("only manifest v3 is supported")
	}
	return r.exportV3()
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
