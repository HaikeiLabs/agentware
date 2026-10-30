//! Typed Kei tool-manifest/v3 definitions, serialization and loading.
use serde_json::{Map, Value, json};
use std::path::Path;

pub const MANIFEST_SCHEMA_V3: &str = "kei.tool-manifest/v3";

/// Error codes for a rejected manifest, per the shared loader contract in
/// `fixtures/toolspec/manifest-v3/README.md`.
pub const CODE_INVALID_JSON: &str = "invalid_json";
pub const CODE_INVALID_TYPE: &str = "invalid_type";
pub const CODE_UNSUPPORTED_SCHEMA: &str = "unsupported_schema";
pub const CODE_UNKNOWN_FIELD: &str = "unknown_field";
pub const CODE_MISSING_FIELD: &str = "missing_field";
pub const CODE_INVALID_OPERATION_CLASS: &str = "invalid_operation_class";
pub const CODE_INVALID_ROUTE: &str = "invalid_route";
pub const CODE_DUPLICATE_TOOL_NAME: &str = "duplicate_tool_name";

#[derive(Debug, Clone, PartialEq)]
pub struct ToolSpec {
    pub name: String,
    pub description: String,
    pub parameters: Option<Map<String, Value>>,
    pub service: String,
    pub source: String,
    pub operation_class: String,
    pub route: Route,
    pub required_capabilities: Vec<String>,
    pub resource_types: Vec<ResourceType>,
    pub enabled: Option<bool>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Route {
    HarnessExecutor(HarnessExecutor),
    ConnectorBinding(ConnectorBinding),
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HarnessExecutor {
    pub executor: String,
    pub registration: String,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConnectorBinding {
    pub agent_id: String,
    pub connector_id: String,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResourceType {
    pub r#type: String,
    pub parent_type: Option<String>,
}

impl ToolSpec {
    /// Convert to the manifest wire object. Model parameters are omitted.
    pub fn to_wire(&self) -> Result<Value, String> {
        for (field, value) in [
            ("name", &self.name),
            ("description", &self.description),
            ("service", &self.service),
            ("source", &self.source),
        ] {
            if value.is_empty() {
                return Err(format!(
                    "invalid ToolSpec for manifest v3: {field} is required"
                ));
            }
        }
        if self.operation_class != "read" && self.operation_class != "write" {
            return Err(
                "invalid ToolSpec for manifest v3: operation_class must be read or write".into(),
            );
        }
        let route = match &self.route {
            Route::HarnessExecutor(v) => {
                if v.executor.is_empty() || v.registration.is_empty() {
                    return Err("invalid ToolSpec for manifest v3: harness_executor executor and registration are required".into());
                }
                if !self.required_capabilities.is_empty() || !self.resource_types.is_empty() {
                    return Err("invalid ToolSpec for manifest v3: harness_executor routes omit connector-only required_capabilities and resource_types".into());
                }
                json!({"harness_executor":{"executor":v.executor,"registration":v.registration}})
            }
            Route::ConnectorBinding(v) => {
                if v.agent_id.is_empty() || v.connector_id.is_empty() {
                    return Err("invalid ToolSpec for manifest v3: connector_binding agent_id and connector_id are required".into());
                }
                if self.required_capabilities.is_empty() {
                    return Err("invalid ToolSpec for manifest v3: connector_binding required_capabilities must not be empty".into());
                }
                json!({"connector_binding":{"agent_id":v.agent_id,"connector_id":v.connector_id}})
            }
        };
        let mut out = json!({"name":self.name,"description":self.description,"service":self.service,"source":self.source,"operation_class":self.operation_class,"route":route,"enabled":self.enabled.unwrap_or(true)});
        let o = out.as_object_mut().expect("object");
        if let Route::ConnectorBinding(_) = self.route {
            o.insert(
                "required_capabilities".into(),
                json!(self.required_capabilities),
            );
            if !self.resource_types.is_empty() {
                o.insert(
                    "resource_types".into(),
                    json!(
                        self.resource_types
                            .iter()
                            .map(|r| {
                                let mut v = json!({"type":r.r#type});
                                if let Some(p) = &r.parent_type {
                                    v["parent_type"] = json!(p);
                                }
                                v
                            })
                            .collect::<Vec<_>>()
                    ),
                );
            }
        }
        Ok(out)
    }
}
pub fn marshal_tool_v3(spec: &ToolSpec) -> Result<Vec<u8>, String> {
    serde_json::to_vec(&spec.to_wire()?).map_err(|e| e.to_string())
}
pub fn marshal_manifest_v3(specs: &[ToolSpec]) -> Result<Vec<u8>, String> {
    let mut tools = Vec::with_capacity(specs.len());
    for (i, spec) in specs.iter().enumerate() {
        tools.push(
            spec.to_wire()
                .map_err(|e| format!("tool {i} ({:?}): {e}", spec.name))?,
        );
    }
    serde_json::to_vec(&json!({"schema":MANIFEST_SCHEMA_V3,"tools":tools}))
        .map_err(|e| e.to_string())
}

/// A rejected kei.tool-manifest/v3 document. `code()` is one of the `CODE_*`
/// contract codes; `path()` is a JSON pointer to the offending field and is
/// diagnostic only. The message never echoes field values.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManifestRejection {
    code: &'static str,
    path: String,
}

impl ManifestRejection {
    /// The contract error code, one of the `CODE_*` constants.
    pub fn code(&self) -> &'static str {
        self.code
    }

    /// JSON pointer to the offending field; diagnostic only.
    pub fn path(&self) -> &str {
        &self.path
    }
}

impl std::fmt::Display for ManifestRejection {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if self.path.is_empty() {
            write!(f, "tool manifest v3: {}", self.code)
        } else {
            write!(f, "tool manifest v3: {} at {}", self.code, self.path)
        }
    }
}

/// A kei.tool-manifest/v3 loading failure: a rejected document carrying the
/// contract code, or an I/O failure reading the file in `load_manifest_v3`
/// (the ordinary I/O error, not a manifest rejection).
#[derive(Debug)]
pub enum ManifestError {
    /// The document violates the loader contract.
    Invalid(ManifestRejection),
    /// Reading the manifest file failed.
    Io(std::io::Error),
}

impl ManifestError {
    /// The contract error code; `None` when reading the file failed.
    pub fn code(&self) -> Option<&'static str> {
        match self {
            Self::Invalid(rejection) => Some(rejection.code()),
            Self::Io(_) => None,
        }
    }

    /// JSON pointer to the offending field; diagnostic only, `None` when
    /// reading the file failed.
    pub fn path(&self) -> Option<&str> {
        match self {
            Self::Invalid(rejection) => Some(rejection.path()),
            Self::Io(_) => None,
        }
    }
}

impl std::fmt::Display for ManifestError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Invalid(rejection) => std::fmt::Display::fmt(rejection, f),
            Self::Io(error) => write!(f, "read tool manifest: {error}"),
        }
    }
}

impl std::error::Error for ManifestError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Invalid(_) => None,
            Self::Io(error) => Some(error),
        }
    }
}

fn manifest_error(code: &'static str, path: impl Into<String>) -> ManifestError {
    ManifestError::Invalid(ManifestRejection {
        code,
        path: path.into(),
    })
}

/// Load and validate a kei.tool-manifest/v3 file, returning the tools in file
/// order. An error reading the file is the ordinary I/O error, not a manifest
/// rejection.
pub fn load_manifest_v3(path: impl AsRef<Path>) -> Result<Vec<ToolSpec>, ManifestError> {
    let data = std::fs::read(path.as_ref()).map_err(ManifestError::Io)?;
    parse_manifest_v3(&data)
}

/// Parse and validate a kei.tool-manifest/v3 document, returning the tools in
/// file order. A rejected document is a `ManifestError::Invalid` carrying the
/// contract code.
pub fn parse_manifest_v3(data: &[u8]) -> Result<Vec<ToolSpec>, ManifestError> {
    let doc: Value =
        serde_json::from_slice(data).map_err(|_| manifest_error(CODE_INVALID_JSON, ""))?;
    parse_document(doc)
}

fn parse_document(doc: Value) -> Result<Vec<ToolSpec>, ManifestError> {
    let obj = doc
        .as_object()
        .ok_or_else(|| manifest_error(CODE_INVALID_TYPE, ""))?;
    match obj.get("schema") {
        Some(Value::String(schema)) if schema == MANIFEST_SCHEMA_V3 => {}
        _ => return Err(manifest_error(CODE_UNSUPPORTED_SCHEMA, "/schema")),
    }
    let tools_list = match obj.get("tools") {
        None => return Err(manifest_error(CODE_MISSING_FIELD, "/tools")),
        Some(raw) => raw
            .as_array()
            .ok_or_else(|| manifest_error(CODE_INVALID_TYPE, "/tools"))?,
    };
    for key in obj.keys() {
        if key != "schema" && key != "tools" {
            return Err(manifest_error(
                CODE_UNKNOWN_FIELD,
                format!("/{}", escape_pointer(key)),
            ));
        }
    }
    // Strict decode of the whole document first: objects, known fields only,
    // correct JSON types.
    let mut decoded = Vec::with_capacity(tools_list.len());
    for (i, raw) in tools_list.iter().enumerate() {
        decoded.push(decode_tool(raw, i)?);
    }
    // Then validate each tool in file order, and check names against earlier
    // tools.
    let mut specs = Vec::with_capacity(decoded.len());
    let mut seen = std::collections::HashSet::with_capacity(decoded.len());
    for (i, tool) in decoded.into_iter().enumerate() {
        let spec = validate_tool(tool, i)?;
        if !seen.insert(spec.name.clone()) {
            return Err(manifest_error(
                CODE_DUPLICATE_TOOL_NAME,
                tool_pointer(i, &["name"]),
            ));
        }
        specs.push(spec);
    }
    Ok(specs)
}

/// The tool fields the contract allows, all others are `unknown_field`.
const TOOL_FIELDS: [&str; 10] = [
    "name",
    "description",
    "service",
    "source",
    "operation_class",
    "route",
    "required_capabilities",
    "resource_types",
    "parameters",
    "enabled",
];

/// One strictly decoded manifest tool; `None` distinguishes an absent field
/// from an empty value.
#[derive(Default)]
struct WireTool {
    name: Option<String>,
    description: Option<String>,
    service: Option<String>,
    source: Option<String>,
    operation_class: Option<String>,
    route: Option<WireRoute>,
    required_capabilities: Option<Vec<String>>,
    resource_types: Option<Vec<WireResourceType>>,
    parameters: Option<Map<String, Value>>,
    enabled: Option<bool>,
}

#[derive(Default)]
struct WireRoute {
    harness_executor: Option<WireHarnessExecutor>,
    connector_binding: Option<WireConnectorBinding>,
}

#[derive(Default)]
struct WireHarnessExecutor {
    executor: Option<String>,
    registration: Option<String>,
}

#[derive(Default)]
struct WireConnectorBinding {
    agent_id: Option<String>,
    connector_id: Option<String>,
}

#[derive(Default)]
struct WireResourceType {
    r#type: Option<String>,
    parent_type: Option<String>,
}

fn decode_tool(raw: &Value, index: usize) -> Result<WireTool, ManifestError> {
    let obj = raw
        .as_object()
        .ok_or_else(|| manifest_error(CODE_INVALID_TYPE, tool_pointer(index, &[])))?;
    for key in obj.keys() {
        if !TOOL_FIELDS.contains(&key.as_str()) {
            return Err(manifest_error(
                CODE_UNKNOWN_FIELD,
                tool_pointer(index, &[key.as_str()]),
            ));
        }
    }
    let mut tool = WireTool::default();
    if let Some(v) = obj.get("name") {
        tool.name = Some(json_string(v, &tool_pointer(index, &["name"]))?);
    }
    if let Some(v) = obj.get("description") {
        tool.description = Some(json_string(v, &tool_pointer(index, &["description"]))?);
    }
    if let Some(v) = obj.get("service") {
        tool.service = Some(json_string(v, &tool_pointer(index, &["service"]))?);
    }
    if let Some(v) = obj.get("source") {
        tool.source = Some(json_string(v, &tool_pointer(index, &["source"]))?);
    }
    if let Some(v) = obj.get("operation_class") {
        tool.operation_class = Some(json_string(v, &tool_pointer(index, &["operation_class"]))?);
    }
    if let Some(v) = obj.get("route") {
        tool.route = Some(decode_route(v, index)?);
    }
    if let Some(v) = obj.get("required_capabilities") {
        tool.required_capabilities = Some(json_string_array(
            v,
            &tool_pointer(index, &["required_capabilities"]),
        )?);
    }
    if let Some(v) = obj.get("resource_types") {
        let elements = v.as_array().ok_or_else(|| {
            manifest_error(CODE_INVALID_TYPE, tool_pointer(index, &["resource_types"]))
        })?;
        let mut rts = Vec::with_capacity(elements.len());
        for (j, element) in elements.iter().enumerate() {
            rts.push(decode_resource_type(element, index, j)?);
        }
        tool.resource_types = Some(rts);
    }
    if let Some(v) = obj.get("parameters") {
        tool.parameters = Some(
            v.as_object()
                .ok_or_else(|| {
                    manifest_error(CODE_INVALID_TYPE, tool_pointer(index, &["parameters"]))
                })?
                .clone(),
        );
    }
    if let Some(v) = obj.get("enabled") {
        tool.enabled =
            Some(v.as_bool().ok_or_else(|| {
                manifest_error(CODE_INVALID_TYPE, tool_pointer(index, &["enabled"]))
            })?);
    }
    Ok(tool)
}

fn decode_route(raw: &Value, index: usize) -> Result<WireRoute, ManifestError> {
    let obj = raw
        .as_object()
        .ok_or_else(|| manifest_error(CODE_INVALID_TYPE, tool_pointer(index, &["route"])))?;
    for key in obj.keys() {
        if key != "harness_executor" && key != "connector_binding" {
            return Err(manifest_error(
                CODE_UNKNOWN_FIELD,
                tool_pointer(index, &["route", key.as_str()]),
            ));
        }
    }
    let mut route = WireRoute::default();
    if let Some(v) = obj.get("harness_executor") {
        let he = v.as_object().ok_or_else(|| {
            manifest_error(
                CODE_INVALID_TYPE,
                tool_pointer(index, &["route", "harness_executor"]),
            )
        })?;
        for key in he.keys() {
            if key != "executor" && key != "registration" {
                return Err(manifest_error(
                    CODE_UNKNOWN_FIELD,
                    tool_pointer(index, &["route", "harness_executor", key.as_str()]),
                ));
            }
        }
        let mut executor = None;
        let mut registration = None;
        if let Some(v) = he.get("executor") {
            executor = Some(json_string(
                v,
                &tool_pointer(index, &["route", "harness_executor", "executor"]),
            )?);
        }
        if let Some(v) = he.get("registration") {
            registration = Some(json_string(
                v,
                &tool_pointer(index, &["route", "harness_executor", "registration"]),
            )?);
        }
        route.harness_executor = Some(WireHarnessExecutor {
            executor,
            registration,
        });
    }
    if let Some(v) = obj.get("connector_binding") {
        let cb = v.as_object().ok_or_else(|| {
            manifest_error(
                CODE_INVALID_TYPE,
                tool_pointer(index, &["route", "connector_binding"]),
            )
        })?;
        for key in cb.keys() {
            if key != "agent_id" && key != "connector_id" {
                return Err(manifest_error(
                    CODE_UNKNOWN_FIELD,
                    tool_pointer(index, &["route", "connector_binding", key.as_str()]),
                ));
            }
        }
        let mut agent_id = None;
        let mut connector_id = None;
        if let Some(v) = cb.get("agent_id") {
            agent_id = Some(json_string(
                v,
                &tool_pointer(index, &["route", "connector_binding", "agent_id"]),
            )?);
        }
        if let Some(v) = cb.get("connector_id") {
            connector_id = Some(json_string(
                v,
                &tool_pointer(index, &["route", "connector_binding", "connector_id"]),
            )?);
        }
        route.connector_binding = Some(WireConnectorBinding {
            agent_id,
            connector_id,
        });
    }
    Ok(route)
}

fn decode_resource_type(
    raw: &Value,
    index: usize,
    j: usize,
) -> Result<WireResourceType, ManifestError> {
    let base = format!("/tools/{index}/resource_types/{j}");
    let obj = raw
        .as_object()
        .ok_or_else(|| manifest_error(CODE_INVALID_TYPE, &base))?;
    for key in obj.keys() {
        if key != "type" && key != "parent_type" {
            return Err(manifest_error(
                CODE_UNKNOWN_FIELD,
                format!("{base}/{}", escape_pointer(key)),
            ));
        }
    }
    let r#type = match obj.get("type") {
        Some(v) => Some(json_string(v, &format!("{base}/type"))?),
        None => None,
    };
    let parent_type = match obj.get("parent_type") {
        Some(v) => Some(json_string(v, &format!("{base}/parent_type"))?),
        None => None,
    };
    Ok(WireResourceType {
        r#type,
        parent_type,
    })
}

/// Runs the per-tool semantic checks in contract order: name, description,
/// service, source, operation_class, route, then the route rules and resource
/// types. The duplicate-name check runs after, in `parse_document`.
fn validate_tool(tool: WireTool, index: usize) -> Result<ToolSpec, ManifestError> {
    let name = required_string(tool.name, &tool_pointer(index, &["name"]))?;
    let description = required_string(tool.description, &tool_pointer(index, &["description"]))?;
    let service = required_string(tool.service, &tool_pointer(index, &["service"]))?;
    let source = required_string(tool.source, &tool_pointer(index, &["source"]))?;
    let operation_class = match tool.operation_class.as_deref() {
        None | Some("") => {
            return Err(manifest_error(
                CODE_MISSING_FIELD,
                tool_pointer(index, &["operation_class"]),
            ));
        }
        Some("read") | Some("write") => tool.operation_class.expect("checked"),
        Some(_) => {
            return Err(manifest_error(
                CODE_INVALID_OPERATION_CLASS,
                tool_pointer(index, &["operation_class"]),
            ));
        }
    };
    let wire_route = tool
        .route
        .ok_or_else(|| manifest_error(CODE_MISSING_FIELD, tool_pointer(index, &["route"])))?;
    let mut kinds = 0;
    let mut route = None;
    if let Some(he) = wire_route.harness_executor {
        kinds += 1;
        if he.executor.as_deref().is_none_or(str::is_empty)
            || he.registration.as_deref().is_none_or(str::is_empty)
        {
            return Err(manifest_error(
                CODE_INVALID_ROUTE,
                tool_pointer(index, &["route", "harness_executor"]),
            ));
        }
        route = Some(Route::HarnessExecutor(HarnessExecutor {
            executor: he.executor.expect("checked"),
            registration: he.registration.expect("checked"),
        }));
    }
    if let Some(cb) = wire_route.connector_binding {
        kinds += 1;
        if cb.agent_id.as_deref().is_none_or(str::is_empty)
            || cb.connector_id.as_deref().is_none_or(str::is_empty)
        {
            return Err(manifest_error(
                CODE_INVALID_ROUTE,
                tool_pointer(index, &["route", "connector_binding"]),
            ));
        }
        route = Some(Route::ConnectorBinding(ConnectorBinding {
            agent_id: cb.agent_id.expect("checked"),
            connector_id: cb.connector_id.expect("checked"),
        }));
    }
    if kinds != 1 {
        return Err(manifest_error(
            CODE_INVALID_ROUTE,
            tool_pointer(index, &["route"]),
        ));
    }
    let route = route.expect("exactly one kind");
    // Route rules, in contract order: a harness tool carries neither
    // connector-only field, not even empty; a connector tool needs a
    // non-empty required_capabilities and, when present, non-empty
    // resource_types.
    let required_capabilities = match &route {
        Route::HarnessExecutor(_) => {
            if tool.required_capabilities.is_some() {
                return Err(manifest_error(
                    CODE_INVALID_ROUTE,
                    tool_pointer(index, &["required_capabilities"]),
                ));
            }
            if tool.resource_types.is_some() {
                return Err(manifest_error(
                    CODE_INVALID_ROUTE,
                    tool_pointer(index, &["resource_types"]),
                ));
            }
            Vec::new()
        }
        Route::ConnectorBinding(_) => {
            let capabilities = match &tool.required_capabilities {
                Some(caps) if !caps.is_empty() => caps.clone(),
                _ => {
                    return Err(manifest_error(
                        CODE_INVALID_ROUTE,
                        tool_pointer(index, &["required_capabilities"]),
                    ));
                }
            };
            if tool.resource_types.as_ref().is_some_and(Vec::is_empty) {
                return Err(manifest_error(
                    CODE_INVALID_ROUTE,
                    tool_pointer(index, &["resource_types"]),
                ));
            }
            capabilities
        }
    };
    let mut resource_types = Vec::new();
    if let Some(rts) = &tool.resource_types {
        for (j, rt) in rts.iter().enumerate() {
            let type_name = match &rt.r#type {
                Some(name) if !name.is_empty() => name.clone(),
                _ => {
                    return Err(manifest_error(
                        CODE_MISSING_FIELD,
                        format!("/tools/{index}/resource_types/{j}/type"),
                    ));
                }
            };
            resource_types.push(ResourceType {
                r#type: type_name,
                parent_type: rt.parent_type.clone(),
            });
        }
    }
    Ok(ToolSpec {
        name,
        description,
        parameters: tool.parameters,
        service,
        source,
        operation_class,
        route,
        required_capabilities,
        resource_types,
        enabled: tool.enabled,
    })
}

fn json_string(value: &Value, path: &str) -> Result<String, ManifestError> {
    value
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| manifest_error(CODE_INVALID_TYPE, path))
}

fn json_string_array(value: &Value, path: &str) -> Result<Vec<String>, ManifestError> {
    let elements = value
        .as_array()
        .ok_or_else(|| manifest_error(CODE_INVALID_TYPE, path))?;
    let mut out = Vec::with_capacity(elements.len());
    for (i, element) in elements.iter().enumerate() {
        out.push(json_string(element, &format!("{path}/{i}"))?);
    }
    Ok(out)
}

/// A required string must be present and non-empty; both are `missing_field`.
fn required_string(value: Option<String>, path: &str) -> Result<String, ManifestError> {
    match value {
        Some(s) if !s.is_empty() => Ok(s),
        _ => Err(manifest_error(CODE_MISSING_FIELD, path)),
    }
}

fn tool_pointer(index: usize, fields: &[&str]) -> String {
    let mut pointer = format!("/tools/{index}");
    for field in fields {
        pointer.push('/');
        pointer.push_str(&escape_pointer(field));
    }
    pointer
}

/// Escapes a JSON pointer token (RFC 6901).
fn escape_pointer(s: &str) -> String {
    s.replace('~', "~0").replace('/', "~1")
}
