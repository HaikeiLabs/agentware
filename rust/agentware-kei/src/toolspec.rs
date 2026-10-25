//! Typed Kei tool-manifest/v3 definitions and serialization.
use serde_json::{Map, Value, json};

pub const MANIFEST_SCHEMA_V3: &str = "kei.tool-manifest/v3";

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
