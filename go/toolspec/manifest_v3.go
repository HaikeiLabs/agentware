package toolspec

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"
)

// Manifest v3 loader error codes, per the shared loader contract in
// fixtures/toolspec/manifest-v3/README.md.
const (
	CodeInvalidJSON           = "invalid_json"
	CodeInvalidType           = "invalid_type"
	CodeUnsupportedSchema     = "unsupported_schema"
	CodeUnknownField          = "unknown_field"
	CodeMissingField          = "missing_field"
	CodeInvalidOperationClass = "invalid_operation_class"
	CodeInvalidRoute          = "invalid_route"
	CodeDuplicateToolName     = "duplicate_tool_name"
)

// ManifestError is a rejected kei.tool-manifest/v3 document. Code is one of
// the contract codes; Path is a JSON pointer to the offending field and is
// diagnostic only. The message never echoes field values.
type ManifestError struct {
	Code string
	Path string
}

// Error implements error.
func (e *ManifestError) Error() string {
	if e.Path == "" {
		return "tool manifest v3: " + e.Code
	}
	return "tool manifest v3: " + e.Code + " at " + e.Path
}

func manifestError(code, path string) error { return &ManifestError{Code: code, Path: path} }

// LoadManifestV3 reads and parses a kei.tool-manifest/v3 file, returning the
// tools in file order. Errors reading the file are ordinary I/O errors; a
// rejected document is a *ManifestError carrying the contract code.
func LoadManifestV3(path string) ([]ToolSpec, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return ParseManifestV3(data)
}

// ParseManifestV3 parses a kei.tool-manifest/v3 document, returning the tools
// in file order. A rejected document is a *ManifestError carrying the
// contract code.
func ParseManifestV3(data []byte) ([]ToolSpec, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	var doc json.RawMessage
	if err := dec.Decode(&doc); err != nil {
		return nil, manifestError(CodeInvalidJSON, "")
	}
	var trailing json.RawMessage
	if err := dec.Decode(&trailing); err == nil {
		return nil, manifestError(CodeInvalidJSON, "")
	}
	return parseManifestV3(doc)
}

func parseManifestV3(doc json.RawMessage) ([]ToolSpec, error) {
	obj, ok := jsonObject(doc)
	if !ok {
		return nil, manifestError(CodeInvalidType, "")
	}
	schemaRaw, ok := obj["schema"]
	if !ok {
		return nil, manifestError(CodeUnsupportedSchema, "/schema")
	}
	schema, ok := jsonString(schemaRaw)
	if !ok || schema != ManifestSchemaV3 {
		return nil, manifestError(CodeUnsupportedSchema, "/schema")
	}
	toolsRaw, ok := obj["tools"]
	if !ok {
		return nil, manifestError(CodeMissingField, "/tools")
	}
	toolsList, ok := jsonArray(toolsRaw)
	if !ok {
		return nil, manifestError(CodeInvalidType, "/tools")
	}
	for _, key := range sortedKeys(obj) {
		if key != "schema" && key != "tools" {
			return nil, manifestError(CodeUnknownField, "/"+escapePointer(key))
		}
	}
	// Strict decode of the whole document first: objects, known fields only,
	// correct JSON types.
	decoded := make([]wireTool, len(toolsList))
	for i, raw := range toolsList {
		tool, err := decodeTool(raw, i)
		if err != nil {
			return nil, err
		}
		decoded[i] = tool
	}
	// Then validate each tool in file order.
	specs := make([]ToolSpec, 0, len(decoded))
	seen := make(map[string]bool, len(decoded))
	for i, tool := range decoded {
		spec, err := validateTool(tool, i)
		if err != nil {
			return nil, err
		}
		if seen[spec.Name] {
			return nil, manifestError(CodeDuplicateToolName, toolPointer(i, "name"))
		}
		seen[spec.Name] = true
		specs = append(specs, spec)
	}
	return specs, nil
}

// wireTool is one strictly decoded manifest tool.
type wireTool struct {
	name                 string
	hasName              bool
	description          string
	hasDescription       bool
	service              string
	hasService           bool
	source               string
	hasSource            bool
	operationClass       string
	hasOperationClass    bool
	route                *wireRoute
	requiredCapabilities []string
	hasRequiredCaps      bool
	resourceTypes        []wireResourceType
	hasResourceTypes     bool
	parameters           map[string]any
	enabled              bool
	hasEnabled           bool
}

type wireRoute struct {
	harnessExecutor  *wireHarnessExecutor
	connectorBinding *wireConnectorBinding
}

type wireHarnessExecutor struct {
	executor        string
	hasExecutor     bool
	registration    string
	hasRegistration bool
}

type wireConnectorBinding struct {
	agentID        string
	hasAgentID     bool
	connectorID    string
	hasConnectorID bool
}

type wireResourceType struct {
	typeName      string
	hasType       bool
	parentType    string
	hasParentType bool
}

var toolFieldNames = map[string]bool{
	"name": true, "description": true, "service": true, "source": true,
	"operation_class": true, "route": true, "required_capabilities": true,
	"resource_types": true, "parameters": true, "enabled": true,
}

func decodeTool(raw json.RawMessage, index int) (wireTool, error) {
	obj, ok := jsonObject(raw)
	if !ok {
		return wireTool{}, manifestError(CodeInvalidType, toolPointer(index))
	}
	var tool wireTool
	for _, key := range sortedKeys(obj) {
		if !toolFieldNames[key] {
			return wireTool{}, manifestError(CodeUnknownField, toolPointer(index, key))
		}
	}
	if r, present := obj["name"]; present {
		s, ok := jsonString(r)
		if !ok {
			return wireTool{}, manifestError(CodeInvalidType, toolPointer(index, "name"))
		}
		tool.name, tool.hasName = s, true
	}
	if r, present := obj["description"]; present {
		s, ok := jsonString(r)
		if !ok {
			return wireTool{}, manifestError(CodeInvalidType, toolPointer(index, "description"))
		}
		tool.description, tool.hasDescription = s, true
	}
	if r, present := obj["service"]; present {
		s, ok := jsonString(r)
		if !ok {
			return wireTool{}, manifestError(CodeInvalidType, toolPointer(index, "service"))
		}
		tool.service, tool.hasService = s, true
	}
	if r, present := obj["source"]; present {
		s, ok := jsonString(r)
		if !ok {
			return wireTool{}, manifestError(CodeInvalidType, toolPointer(index, "source"))
		}
		tool.source, tool.hasSource = s, true
	}
	if r, present := obj["operation_class"]; present {
		s, ok := jsonString(r)
		if !ok {
			return wireTool{}, manifestError(CodeInvalidType, toolPointer(index, "operation_class"))
		}
		tool.operationClass, tool.hasOperationClass = s, true
	}
	if r, present := obj["route"]; present {
		route, err := decodeRoute(r, index)
		if err != nil {
			return wireTool{}, err
		}
		tool.route = route
	}
	if r, present := obj["required_capabilities"]; present {
		elems, ok := jsonArray(r)
		if !ok {
			return wireTool{}, manifestError(CodeInvalidType, toolPointer(index, "required_capabilities"))
		}
		caps := make([]string, 0, len(elems))
		for j, e := range elems {
			s, ok := jsonString(e)
			if !ok {
				return wireTool{}, manifestError(CodeInvalidType, fmt.Sprintf("/tools/%d/required_capabilities/%d", index, j))
			}
			caps = append(caps, s)
		}
		tool.requiredCapabilities, tool.hasRequiredCaps = caps, true
	}
	if r, present := obj["resource_types"]; present {
		elems, ok := jsonArray(r)
		if !ok {
			return wireTool{}, manifestError(CodeInvalidType, toolPointer(index, "resource_types"))
		}
		rts := make([]wireResourceType, 0, len(elems))
		for j, e := range elems {
			rt, err := decodeResourceType(e, index, j)
			if err != nil {
				return wireTool{}, err
			}
			rts = append(rts, rt)
		}
		tool.resourceTypes, tool.hasResourceTypes = rts, true
	}
	if r, present := obj["parameters"]; present {
		params, ok := jsonAnyObject(r)
		if !ok {
			return wireTool{}, manifestError(CodeInvalidType, toolPointer(index, "parameters"))
		}
		tool.parameters = params
	}
	if r, present := obj["enabled"]; present {
		var b bool
		if err := json.Unmarshal(r, &b); err != nil {
			return wireTool{}, manifestError(CodeInvalidType, toolPointer(index, "enabled"))
		}
		tool.enabled, tool.hasEnabled = b, true
	}
	return tool, nil
}

func decodeRoute(raw json.RawMessage, index int) (*wireRoute, error) {
	obj, ok := jsonObject(raw)
	if !ok {
		return nil, manifestError(CodeInvalidType, toolPointer(index, "route"))
	}
	var route wireRoute
	for _, key := range sortedKeys(obj) {
		if key != "harness_executor" && key != "connector_binding" {
			return nil, manifestError(CodeUnknownField, toolPointer(index, "route", key))
		}
	}
	if r, present := obj["harness_executor"]; present {
		he, err := decodeHarnessExecutor(r, index)
		if err != nil {
			return nil, err
		}
		route.harnessExecutor = he
	}
	if r, present := obj["connector_binding"]; present {
		cb, err := decodeConnectorBinding(r, index)
		if err != nil {
			return nil, err
		}
		route.connectorBinding = cb
	}
	return &route, nil
}

func decodeHarnessExecutor(raw json.RawMessage, index int) (*wireHarnessExecutor, error) {
	obj, ok := jsonObject(raw)
	if !ok {
		return nil, manifestError(CodeInvalidType, toolPointer(index, "route", "harness_executor"))
	}
	var he wireHarnessExecutor
	for _, key := range sortedKeys(obj) {
		if key != "executor" && key != "registration" {
			return nil, manifestError(CodeUnknownField, toolPointer(index, "route", "harness_executor", key))
		}
	}
	if r, present := obj["executor"]; present {
		s, ok := jsonString(r)
		if !ok {
			return nil, manifestError(CodeInvalidType, toolPointer(index, "route", "harness_executor", "executor"))
		}
		he.executor, he.hasExecutor = s, true
	}
	if r, present := obj["registration"]; present {
		s, ok := jsonString(r)
		if !ok {
			return nil, manifestError(CodeInvalidType, toolPointer(index, "route", "harness_executor", "registration"))
		}
		he.registration, he.hasRegistration = s, true
	}
	return &he, nil
}

func decodeConnectorBinding(raw json.RawMessage, index int) (*wireConnectorBinding, error) {
	obj, ok := jsonObject(raw)
	if !ok {
		return nil, manifestError(CodeInvalidType, toolPointer(index, "route", "connector_binding"))
	}
	var cb wireConnectorBinding
	for _, key := range sortedKeys(obj) {
		if key != "agent_id" && key != "connector_id" {
			return nil, manifestError(CodeUnknownField, toolPointer(index, "route", "connector_binding", key))
		}
	}
	if r, present := obj["agent_id"]; present {
		s, ok := jsonString(r)
		if !ok {
			return nil, manifestError(CodeInvalidType, toolPointer(index, "route", "connector_binding", "agent_id"))
		}
		cb.agentID, cb.hasAgentID = s, true
	}
	if r, present := obj["connector_id"]; present {
		s, ok := jsonString(r)
		if !ok {
			return nil, manifestError(CodeInvalidType, toolPointer(index, "route", "connector_binding", "connector_id"))
		}
		cb.connectorID, cb.hasConnectorID = s, true
	}
	return &cb, nil
}

func decodeResourceType(raw json.RawMessage, index, j int) (wireResourceType, error) {
	obj, ok := jsonObject(raw)
	if !ok {
		return wireResourceType{}, manifestError(CodeInvalidType, fmt.Sprintf("/tools/%d/resource_types/%d", index, j))
	}
	var rt wireResourceType
	for _, key := range sortedKeys(obj) {
		if key != "type" && key != "parent_type" {
			return wireResourceType{}, manifestError(CodeUnknownField, fmt.Sprintf("/tools/%d/resource_types/%d/%s", index, j, escapePointer(key)))
		}
	}
	if r, present := obj["type"]; present {
		s, ok := jsonString(r)
		if !ok {
			return wireResourceType{}, manifestError(CodeInvalidType, fmt.Sprintf("/tools/%d/resource_types/%d/type", index, j))
		}
		rt.typeName, rt.hasType = s, true
	}
	if r, present := obj["parent_type"]; present {
		s, ok := jsonString(r)
		if !ok {
			return wireResourceType{}, manifestError(CodeInvalidType, fmt.Sprintf("/tools/%d/resource_types/%d/parent_type", index, j))
		}
		rt.parentType, rt.hasParentType = s, true
	}
	return rt, nil
}

// validateTool runs the per-tool semantic checks in contract order: name,
// description, service, source, operation_class, route, then the route rules
// and resource types. The duplicate-name check runs after, in parseManifestV3.
func validateTool(tool wireTool, index int) (ToolSpec, error) {
	if !tool.hasName || tool.name == "" {
		return ToolSpec{}, manifestError(CodeMissingField, toolPointer(index, "name"))
	}
	if !tool.hasDescription || tool.description == "" {
		return ToolSpec{}, manifestError(CodeMissingField, toolPointer(index, "description"))
	}
	if !tool.hasService || tool.service == "" {
		return ToolSpec{}, manifestError(CodeMissingField, toolPointer(index, "service"))
	}
	if !tool.hasSource || tool.source == "" {
		return ToolSpec{}, manifestError(CodeMissingField, toolPointer(index, "source"))
	}
	if !tool.hasOperationClass || tool.operationClass == "" {
		return ToolSpec{}, manifestError(CodeMissingField, toolPointer(index, "operation_class"))
	}
	if tool.operationClass != "read" && tool.operationClass != "write" {
		return ToolSpec{}, manifestError(CodeInvalidOperationClass, toolPointer(index, "operation_class"))
	}
	if tool.route == nil {
		return ToolSpec{}, manifestError(CodeMissingField, toolPointer(index, "route"))
	}
	route := &Route{}
	kinds := 0
	if tool.route.harnessExecutor != nil {
		kinds++
		he := tool.route.harnessExecutor
		if !he.hasExecutor || he.executor == "" || !he.hasRegistration || he.registration == "" {
			return ToolSpec{}, manifestError(CodeInvalidRoute, toolPointer(index, "route", "harness_executor"))
		}
		route.HarnessExecutor = &HarnessExecutor{Executor: he.executor, Registration: he.registration}
	}
	if tool.route.connectorBinding != nil {
		kinds++
		cb := tool.route.connectorBinding
		if !cb.hasAgentID || cb.agentID == "" || !cb.hasConnectorID || cb.connectorID == "" {
			return ToolSpec{}, manifestError(CodeInvalidRoute, toolPointer(index, "route", "connector_binding"))
		}
		route.ConnectorBinding = &ConnectorBinding{AgentID: cb.agentID, ConnectorID: cb.connectorID}
	}
	if kinds != 1 {
		return ToolSpec{}, manifestError(CodeInvalidRoute, toolPointer(index, "route"))
	}
	if route.HarnessExecutor != nil {
		// A harness tool carries neither connector-only field, not even empty.
		if tool.hasRequiredCaps {
			return ToolSpec{}, manifestError(CodeInvalidRoute, toolPointer(index, "required_capabilities"))
		}
		if tool.hasResourceTypes {
			return ToolSpec{}, manifestError(CodeInvalidRoute, toolPointer(index, "resource_types"))
		}
	}
	if route.ConnectorBinding != nil {
		if !tool.hasRequiredCaps || len(tool.requiredCapabilities) == 0 {
			return ToolSpec{}, manifestError(CodeInvalidRoute, toolPointer(index, "required_capabilities"))
		}
		if tool.hasResourceTypes && len(tool.resourceTypes) == 0 {
			return ToolSpec{}, manifestError(CodeInvalidRoute, toolPointer(index, "resource_types"))
		}
	}
	resourceTypes := make([]ResourceType, 0, len(tool.resourceTypes))
	for j, rt := range tool.resourceTypes {
		if !rt.hasType || rt.typeName == "" {
			return ToolSpec{}, manifestError(CodeMissingField, fmt.Sprintf("/tools/%d/resource_types/%d/type", index, j))
		}
		entry := ResourceType{Type: rt.typeName}
		if rt.hasParentType {
			entry.ParentType = rt.parentType
		}
		resourceTypes = append(resourceTypes, entry)
	}
	enabled := true
	if tool.hasEnabled {
		enabled = tool.enabled
	}
	parameters := tool.parameters
	if parameters == nil {
		parameters = map[string]any{}
	}
	capabilities := tool.requiredCapabilities
	if capabilities == nil {
		capabilities = []string{}
	}
	return ToolSpec{
		Name:                 tool.name,
		Description:          tool.description,
		Parameters:           parameters,
		Service:              tool.service,
		Source:               tool.source,
		OperationClass:       tool.operationClass,
		Route:                route,
		RequiredCapabilities: capabilities,
		ResourceTypes:        resourceTypes,
		Enabled:              &enabled,
	}, nil
}

func jsonObject(raw json.RawMessage) (map[string]json.RawMessage, bool) {
	var m map[string]json.RawMessage
	if err := json.Unmarshal(raw, &m); err != nil || m == nil {
		return nil, false
	}
	return m, true
}

func jsonAnyObject(raw json.RawMessage) (map[string]any, bool) {
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil || m == nil {
		return nil, false
	}
	return m, true
}

func jsonArray(raw json.RawMessage) ([]json.RawMessage, bool) {
	var a []json.RawMessage
	if err := json.Unmarshal(raw, &a); err != nil || a == nil {
		return nil, false
	}
	return a, true
}

// jsonString rejects null: a JSON null is not a string.
func jsonString(raw json.RawMessage) (string, bool) {
	if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		return "", false
	}
	var s string
	if err := json.Unmarshal(raw, &s); err != nil {
		return "", false
	}
	return s, true
}

func sortedKeys(m map[string]json.RawMessage) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func toolPointer(index int, fields ...string) string {
	p := fmt.Sprintf("/tools/%d", index)
	for _, f := range fields {
		p += "/" + escapePointer(f)
	}
	return p
}

// escapePointer escapes a JSON pointer token (RFC 6901).
func escapePointer(s string) string {
	s = strings.ReplaceAll(s, "~", "~0")
	return strings.ReplaceAll(s, "/", "~1")
}
