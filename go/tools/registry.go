package tools

import (
	"encoding/json"
	"errors"
	"fmt"
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
	ManifestV4 ManifestVersion = 4
)

// KeiToolRegistration is trusted route metadata stored alongside a dispatch entry.
type KeiToolRegistration struct {
	Plan                 *KeiToolPlan      `json:"plan,omitempty"`
	Service              string            `json:"service"`
	Source               string            `json:"source"`
	OperationClass       string            `json:"operation_class"`
	Route                ToolRoute         `json:"route"`
	RequiredCapabilities []string          `json:"required_capabilities,omitempty"`
	ResourceTypes        []KeiResourceType `json:"resource_types,omitempty"`
}

// KeiToolPlan is the declarative, connector-only v4 operation plan.
type KeiToolPlan struct {
	ContextSchema map[string]any     `json:"context_schema"`
	Operations    []KeiToolOperation `json:"operations"`
}
type KeiToolOperation struct {
	ID                       string             `json:"id"`
	Capability               string             `json:"capability"`
	Resource                 KeiPlannedResource `json:"resource"`
	ProviderResourceTemplate string             `json:"provider_resource_template"`
	ProviderInput            any                `json:"provider_input"`
}
type KeiPlannedResource struct {
	Type   string            `json:"type"`
	ID     KeiValueRef       `json:"id"`
	Parent *KeiPlannedParent `json:"parent,omitempty"`
}
type KeiPlannedParent struct {
	Type string      `json:"type"`
	ID   KeiValueRef `json:"id"`
}
type KeiValueRef struct {
	From    string `json:"from"`
	Pointer string `json:"pointer,omitempty"`
	Field   string `json:"field,omitempty"`
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

type manifestV4 struct {
	Schema string            `json:"schema"`
	Tools  []manifestV4Entry `json:"tools"`
}
type manifestV4Entry struct {
	Name                 string            `json:"name"`
	Service              string            `json:"service"`
	Source               string            `json:"source"`
	OperationClass       string            `json:"operation_class"`
	Route                ToolRoute         `json:"route"`
	RequiredCapabilities []string          `json:"required_capabilities,omitempty"`
	ResourceTypes        []KeiResourceType `json:"resource_types,omitempty"`
	Plan                 *manifestV4Plan   `json:"plan,omitempty"`
	Description          string            `json:"description"`
	Enabled              bool              `json:"enabled"`
}
type manifestV4Plan struct {
	ArgsSchema    map[string]any     `json:"args_schema"`
	ContextSchema map[string]any     `json:"context_schema"`
	Operations    []KeiToolOperation `json:"operations"`
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
	if selected != ManifestV1 && selected != ManifestV2 && selected != ManifestV3 && selected != ManifestV4 {
		return nil, errors.New("unsupported manifest version")
	}

	if selected == ManifestV3 {
		return r.exportV3()
	}
	if selected == ManifestV4 {
		return r.exportV4()
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

func (r *ToolRegistry) exportV4() ([]byte, error) {
	names := make([]string, 0, len(r.registrations))
	for name := range r.registrations {
		names = append(names, name)
	}
	sort.Strings(names)
	entries := make([]manifestV4Entry, 0, len(names))
	for _, name := range names {
		reg := r.registrations[name]
		connector, harness := reg.Route.ConnectorBinding, reg.Route.HarnessExecutor
		if strings.TrimSpace(reg.Service) == "" || strings.TrimSpace(reg.Source) == "" || (reg.OperationClass != "read" && reg.OperationClass != "write") {
			return nil, errors.New("v4 registration requires service, source and valid operation_class")
		}
		if (connector == nil) == (harness == nil) {
			return nil, errors.New("route must contain exactly one branch")
		}
		entry := manifestV4Entry{Name: name, Service: reg.Service, Source: reg.Source, OperationClass: reg.OperationClass, Route: reg.Route, Description: r.tools[name].Description(), Enabled: true}
		if harness != nil {
			if strings.TrimSpace(harness.Executor) == "" || strings.TrimSpace(harness.Registration) == "" || reg.Plan != nil || reg.RequiredCapabilities != nil || reg.ResourceTypes != nil {
				return nil, errors.New("harness route omits connector plan and fields")
			}
			entries = append(entries, entry)
			continue
		}
		if strings.TrimSpace(connector.AgentID) == "" || strings.TrimSpace(connector.ConnectorID) == "" || len(reg.RequiredCapabilities) == 0 || reg.Plan == nil {
			return nil, errors.New("v4 connector route requires binding, capabilities and plan")
		}
		extended, ok := r.tools[name].(ExtendedTool)
		if !ok {
			return nil, errors.New("v4 connector requires ExtendedTool input schema")
		}
		argsSchema := extended.InputSchema()
		if err := validateClosedObjectSchema(argsSchema); err != nil {
			return nil, fmt.Errorf("%s args_schema: %w", name, err)
		}
		if err := validateClosedObjectSchema(reg.Plan.ContextSchema); err != nil {
			return nil, fmt.Errorf("%s context_schema: %w", name, err)
		}
		if err := validateV4Plan(reg, reg.Plan, argsSchema); err != nil {
			return nil, fmt.Errorf("%s plan: %w", name, err)
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
		entry.Plan = &manifestV4Plan{ArgsSchema: argsSchema, ContextSchema: reg.Plan.ContextSchema, Operations: reg.Plan.Operations}
		entries = append(entries, entry)
	}
	return json.MarshalIndent(manifestV4{Schema: "kei.tool-manifest/v4", Tools: entries}, "", "  ")
}

func validateClosedObjectSchema(schema map[string]any) error {
	if schema == nil || schema["type"] != "object" || schema["additionalProperties"] != false {
		return errors.New("must be a closed object schema")
	}
	props, ok := schema["properties"].(map[string]any)
	if !ok {
		return errors.New("properties must be an object")
	}
	_ = props
	return nil
}
func validateV4Plan(reg KeiToolRegistration, p *KeiToolPlan, argsSchema map[string]any) error {
	if len(p.Operations) == 0 || len(p.Operations) > 32 {
		return errors.New("operations must contain 1..32 entries")
	}
	declared := map[string]bool{}
	for _, c := range reg.RequiredCapabilities {
		if strings.TrimSpace(c) == "" || declared[c] {
			return errors.New("empty or duplicate registered capability")
		}
		declared[c] = true
	}
	resources := map[string]bool{}
	for _, r := range reg.ResourceTypes {
		key := r.Type + "\\x00" + r.ParentType
		if r.Type == "" || resources[key] {
			return errors.New("empty or duplicate registered resource type")
		}
		resources[key] = true
	}
	caps, ids := map[string]bool{}, map[string]bool{}
	contextProps, _ := p.ContextSchema["properties"].(map[string]any)
	for _, op := range p.Operations {
		if op.ID == "" || ids[op.ID] || !declared[op.Capability] {
			return errors.New("operation id must be unique and capability declared")
		}
		ids[op.ID] = true
		caps[op.Capability] = true
		parentType := ""
		if op.Resource.Parent != nil {
			parentType = op.Resource.Parent.Type
		}
		if !resources[op.Resource.Type+"\\x00"+parentType] {
			return errors.New("resource/parent pair is not declared")
		}
		for _, ref := range []KeiValueRef{op.Resource.ID} {
			if err := validateV4Ref(ref, argsSchema, contextProps); err != nil {
				return err
			}
		}
		if op.Resource.Parent != nil {
			if err := validateV4Ref(op.Resource.Parent.ID, argsSchema, contextProps); err != nil {
				return err
			}
		}
		template := strings.ReplaceAll(op.ProviderResourceTemplate, "{resource.id}", "__RESOURCE_ID__")
		template = strings.ReplaceAll(template, "{parent.id}", "__PARENT_ID__")
		if !strings.Contains(template, "__RESOURCE_ID__") || strings.ContainsAny(template, "{}") || (op.Resource.Parent != nil && !strings.Contains(template, "__PARENT_ID__")) || (op.Resource.Parent == nil && strings.Contains(template, "__PARENT_ID__")) {
			return errors.New("provider resource template may interpolate only declared resource/parent ids")
		}
		if err := validateTemplateValue(op.ProviderInput, contextProps, argsSchema, 0); err != nil {
			return err
		}
	}
	if len(caps) != len(declared) {
		return errors.New("operation capability set must exactly cover registered capabilities")
	}
	for c := range declared {
		if !caps[c] {
			return errors.New("operation capability set must exactly cover registered capabilities")
		}
	}
	return nil
}

func validateV4Ref(ref KeiValueRef, args, context map[string]any) error {
	var props map[string]any
	var key string
	switch ref.From {
	case "args":
		if ref.Field != "" || !strings.HasPrefix(ref.Pointer, "/") || strings.Count(ref.Pointer, "/") != 1 {
			return errors.New("args refs require a direct top-level JSON Pointer")
		}
		key = strings.TrimPrefix(ref.Pointer, "/")
		props, _ = args["properties"].(map[string]any)
	case "context":
		if ref.Pointer != "" || strings.TrimSpace(ref.Field) == "" {
			return errors.New("context refs require a named field")
		}
		key = ref.Field
		props = context
	default:
		return errors.New("reference from must be args or context")
	}
	if _, ok := props[key]; !ok {
		return errors.New("reference field is not declared by its schema")
	}
	return nil
}
func validateTemplateValue(v any, context, args map[string]any, depth int) error {
	if depth > 16 {
		return errors.New("provider_input nesting exceeds 16")
	}
	switch x := v.(type) {
	case map[string]any:
		if _, ok := x["from"]; ok {
			b, e := json.Marshal(x)
			if e != nil {
				return e
			}
			var ref KeiValueRef
			if e = json.Unmarshal(b, &ref); e != nil {
				return e
			}
			if len(x) != 2 {
				return errors.New("reference must contain only from and pointer/field")
			}
			return validateV4Ref(ref, args, context)
		}
		for _, item := range x {
			if err := validateTemplateValue(item, context, args, depth+1); err != nil {
				return err
			}
		}
	case []any:
		for _, item := range x {
			if err := validateTemplateValue(item, context, args, depth+1); err != nil {
				return err
			}
		}
	case nil, string, bool, float64:
		return nil
	default:
		return errors.New("unsupported provider_input value")
	}
	return nil
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
