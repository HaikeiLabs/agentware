//! The tool registry and Kei tool-manifest v3 export (port of
//! `go/tools/registry.go`).

use std::collections::HashMap;
use std::sync::Arc;

use serde_json::{Map, Value, json};

use super::tool::{KeiResourceType, Tool};

/// A registered tool with optional trusted route metadata (Go `ToolRegistry`;
/// the maps are keyed by tool name).
#[derive(Default)]
pub struct ToolRegistry {
    tools: HashMap<String, Arc<dyn Tool>>,
    registrations: HashMap<String, KeiToolRegistration>,
}

impl ToolRegistry {
    /// Go `NewToolRegistry`.
    pub fn new() -> Self {
        Self::default()
    }

    /// Associates optional trusted route metadata with the same entry used
    /// for dispatch (Go `Register`): the tool replaces any previous entry
    /// under its name and a stale registration is deleted first.
    pub fn register(&mut self, tool: Arc<dyn Tool>, registration: Option<KeiToolRegistration>) {
        let name = tool.name().to_owned();
        self.tools.insert(name.clone(), tool);
        self.registrations.remove(&name);
        if let Some(registration) = registration {
            self.registrations.insert(name, registration);
        }
    }

    /// Go `Get`.
    pub fn get(&self, name: &str) -> Option<Arc<dyn Tool>> {
        self.tools.get(name).cloned()
    }

    /// All tools, sorted by name (Go `All`).
    pub fn all(&self) -> Vec<Arc<dyn Tool>> {
        self.names()
            .into_iter()
            .filter_map(|name| self.tools.get(&name).cloned())
            .collect()
    }

    /// Sorted tool names (Go `Names`).
    pub fn names(&self) -> Vec<String> {
        let mut names: Vec<String> = self.tools.keys().cloned().collect();
        names.sort();
        names
    }

    /// Input schemas of extended tools only (Go `Schemas`).
    pub fn schemas(&self) -> HashMap<String, Map<String, Value>> {
        let mut schemas = HashMap::new();
        for tool in self.tools.values() {
            if let Some(extended) = tool.as_extended() {
                if let Some(schema) = extended.input_schema() {
                    schemas.insert(tool.name().to_owned(), schema);
                }
            }
        }
        schemas
    }

    /// Returns the registry's v3 tool manifest (Go `ExportKeiToolManifest`).
    /// `version` mirrors the Go variadic: `None` or `Some(MANIFEST_V3)` is
    /// accepted, anything else errors.
    pub fn export_kei_tool_manifest(
        &self,
        version: Option<ManifestVersion>,
    ) -> Result<Vec<u8>, String> {
        if let Some(version) = version {
            if version != MANIFEST_V3 {
                return Err("only manifest v3 is supported".into());
            }
        }
        self.export_v3()
    }

    fn export_v3(&self) -> Result<Vec<u8>, String> {
        let mut names: Vec<&String> = self.registrations.keys().collect();
        names.sort();

        let mut entries = Vec::with_capacity(names.len());
        for name in names {
            let registration = &self.registrations[name];
            if registration.service.trim().is_empty() || registration.source.trim().is_empty() {
                return Err("v3 registration requires non-empty service and source".into());
            }
            if registration.operation_class != "read" && registration.operation_class != "write" {
                return Err("invalid operation_class".into());
            }
            entries.push(self.build_entry(name, registration)?);
        }

        let manifest = json!({
            "schema": "kei.tool-manifest/v3",
            "tools": entries,
        });
        // Go: json.MarshalIndent(manifestV3{...}, "", "  ").
        serde_json::to_vec_pretty(&manifest).map_err(|error| error.to_string())
    }

    /// Validates one registration and builds its manifest entry
    /// (`go/tools/registry.go` exportV3 body). Keys are emitted in Go's
    /// struct-field order so the bytes match `json.MarshalIndent`.
    fn build_entry(&self, name: &str, registration: &KeiToolRegistration) -> Result<Value, String> {
        let connector = registration.route.connector_binding.as_ref();
        let harness = registration.route.harness_executor.as_ref();

        if connector.is_none() == harness.is_none() {
            return Err("route must contain exactly one branch".into());
        }

        let description = self
            .tools
            .get(name)
            .expect("registration implies a registered tool")
            .description();

        let mut object = Map::new();
        object.insert("name".into(), json!(name));
        object.insert("service".into(), json!(registration.service));
        object.insert("source".into(), json!(registration.source));
        object.insert(
            "operation_class".into(),
            json!(registration.operation_class),
        );
        object.insert("route".into(), registration.route.to_wire());

        if let Some(connector) = connector {
            let Some(capabilities) = &registration.required_capabilities else {
                return Err("connector route requires binding and non-empty capabilities".into());
            };
            if connector.agent_id.trim().is_empty()
                || connector.connector_id.trim().is_empty()
                || capabilities.is_empty()
            {
                return Err("connector route requires binding and non-empty capabilities".into());
            }
            for capability in capabilities {
                if capability.trim().is_empty() {
                    return Err("connector capability must be non-empty".into());
                }
            }
            let mut sorted_capabilities = capabilities.clone();
            sorted_capabilities.sort();
            object.insert("required_capabilities".into(), json!(sorted_capabilities));
            // Go emits resource_types when non-empty (omitempty), sorted by
            // type then parent type.
            let resource_types = match &registration.resource_types {
                Some(types) if !types.is_empty() => {
                    let mut sorted = types.clone();
                    sorted.sort();
                    json!(sorted.iter().map(resource_type_to_wire).collect::<Vec<_>>())
                }
                _ => json!([]),
            };
            object.insert("resource_types".into(), resource_types);
        } else {
            let harness = harness.expect("exactly one branch checked above");
            if harness.executor.trim().is_empty()
                || harness.registration.trim().is_empty()
                || registration.required_capabilities.is_some()
                || registration.resource_types.is_some()
            {
                return Err(
                    "harness route requires identity and omits connector-only fields".into(),
                );
            }
        }

        object.insert("description".into(), json!(description));
        object.insert("enabled".into(), json!(true));

        Ok(Value::Object(object))
    }
}

/// Selects the manifest wire schema (Go `ManifestVersion int`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ManifestVersion(pub i64);

/// Go `ManifestV3`.
pub const MANIFEST_V3: ManifestVersion = ManifestVersion(3);

/// Trusted route metadata stored alongside a dispatch entry (Go
/// `KeiToolRegistration`). The capability and resource fields mirror Go's
/// nilable slices: `None` is Go's nil (harness routes must have nil), while
/// an empty `Some` is an explicitly empty slice.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct KeiToolRegistration {
    pub service: String,
    pub source: String,
    pub operation_class: String,
    pub route: ToolRoute,
    pub required_capabilities: Option<Vec<String>>,
    pub resource_types: Option<Vec<KeiResourceType>>,
}

/// Exactly one branch is set (Go `ToolRoute`).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ToolRoute {
    pub connector_binding: Option<ConnectorBindingRoute>,
    pub harness_executor: Option<HarnessExecutorRoute>,
}

impl ToolRoute {
    /// The wire object: Go marshals the route with omitempty on both
    /// branches, so exactly one key appears.
    fn to_wire(&self) -> Value {
        if let Some(connector) = &self.connector_binding {
            return json!({
                "connector_binding": {
                    "agent_id": connector.agent_id,
                    "connector_id": connector.connector_id,
                },
            });
        }
        if let Some(harness) = &self.harness_executor {
            return json!({
                "harness_executor": {
                    "executor": harness.executor,
                    "registration": harness.registration,
                },
            });
        }
        json!({})
    }
}

/// Go `ConnectorBindingRoute`.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ConnectorBindingRoute {
    /// Go field `AgentID`.
    pub agent_id: String,
    /// Go field `ConnectorID`.
    pub connector_id: String,
}

/// Go `HarnessExecutorRoute`.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct HarnessExecutorRoute {
    pub executor: String,
    pub registration: String,
}

/// Sorted resource types: by type, then parent type (Go's sort.Slice).
impl Ord for KeiResourceType {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.r#type
            .cmp(&other.r#type)
            .then_with(|| self.parent_type.cmp(&other.parent_type))
    }
}

impl PartialOrd for KeiResourceType {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

/// The wire object for one resource type; Go's omitempty drops an empty
/// `parent_type`.
fn resource_type_to_wire(resource_type: &KeiResourceType) -> Value {
    let mut object = Map::new();
    object.insert("type".into(), json!(resource_type.r#type));
    if !resource_type.parent_type.is_empty() {
        object.insert("parent_type".into(), json!(resource_type.parent_type));
    }
    Value::Object(object)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tools::result::ToolResult;
    use crate::tools::tool::{ExtendedTool, Tool, ToolExample};
    use serde_json::json;

    struct TestTool {
        name: String,
        description: String,
    }
    impl Tool for TestTool {
        fn name(&self) -> &str {
            &self.name
        }
        fn description(&self) -> &str {
            &self.description
        }
        fn execute(&self, _args: &Map<String, Value>) -> Result<ToolResult, String> {
            Ok(ToolResult {
                success: true,
                ..Default::default()
            })
        }
    }

    struct TestExtendedTool {
        inner: TestTool,
    }
    impl Tool for TestExtendedTool {
        fn name(&self) -> &str {
            self.inner.name()
        }
        fn description(&self) -> &str {
            self.inner.description()
        }
        fn execute(&self, args: &Map<String, Value>) -> Result<ToolResult, String> {
            self.inner.execute(args)
        }
        fn as_extended(&self) -> Option<&dyn ExtendedTool> {
            Some(self)
        }
    }
    impl ExtendedTool for TestExtendedTool {
        fn input_schema(&self) -> Option<Map<String, Value>> {
            Some(serde_json::from_value(json!({"type": "object"})).unwrap())
        }
        fn examples(&self) -> Vec<ToolExample> {
            Vec::new()
        }
    }

    fn plain(name: &str, description: &str) -> Arc<dyn Tool> {
        Arc::new(TestTool {
            name: name.into(),
            description: description.into(),
        })
    }

    fn connector_registration() -> KeiToolRegistration {
        KeiToolRegistration {
            service: "github".into(),
            source: "github".into(),
            operation_class: "read".into(),
            route: ToolRoute {
                connector_binding: Some(ConnectorBindingRoute {
                    agent_id: "11111111-1111-4111-8111-111111111111".into(),
                    connector_id: "22222222-2222-4222-8222-222222222222".into(),
                }),
                harness_executor: None,
            },
            required_capabilities: Some(vec!["issue.read".into()]),
            resource_types: Some(vec![KeiResourceType {
                r#type: "issue".into(),
                parent_type: "repository".into(),
            }]),
        }
    }

    fn harness_registration() -> KeiToolRegistration {
        KeiToolRegistration {
            service: "codex".into(),
            source: "codex".into(),
            operation_class: "write".into(),
            route: ToolRoute {
                connector_binding: None,
                harness_executor: Some(HarnessExecutorRoute {
                    executor: "codex".into(),
                    registration: "codex.shell".into(),
                }),
            },
            required_capabilities: None,
            resource_types: None,
        }
    }

    fn fixture_text() -> String {
        let fixture_path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(std::path::Path::parent)
            .expect("crate lives two levels below the repository root")
            .join("fixtures/kei/tool-manifest.v3.json");
        std::fs::read_to_string(&fixture_path).expect("fixture")
    }

    #[test]
    fn schemas_only_for_extended_tools() {
        let mut registry = ToolRegistry::new();
        registry.register(plain("basic_tool", "Basic"), None);
        registry.register(
            Arc::new(TestExtendedTool {
                inner: TestTool {
                    name: "extended_tool".into(),
                    description: "Extended".into(),
                },
            }),
            None,
        );

        let schemas = registry.schemas();
        assert_eq!(schemas.len(), 1);
        assert_eq!(schemas["extended_tool"]["type"], json!("object"));
        assert!(!schemas.contains_key("basic_tool"));
    }

    /// The exported manifest must be byte-identical to the shared fixture
    /// (Go: TestExportKeiToolManifest_FixtureMatch compares trimmed bytes).
    #[test]
    fn export_matches_shared_fixture_bytes() {
        let mut registry = ToolRegistry::new();
        registry.register(
            plain(
                "github.get_issue",
                "Fetch an issue from a GitHub repository",
            ),
            Some(connector_registration()),
        );
        registry.register(
            plain("codex.shell", "Run a local harness action"),
            Some(harness_registration()),
        );

        let manifest = registry.export_kei_tool_manifest(None).expect("export");
        let manifest_text = String::from_utf8(manifest).unwrap();

        assert_eq!(
            manifest_text.trim(),
            fixture_text().trim(),
            "serialized manifest differs from shared fixture"
        );
    }

    #[test]
    fn export_empty_registry() {
        let registry = ToolRegistry::new();
        let manifest = registry.export_kei_tool_manifest(None).expect("export");
        let value: Value = serde_json::from_slice(&manifest).unwrap();
        assert_eq!(value["schema"], json!("kei.tool-manifest/v3"));
        assert_eq!(value["tools"], json!([]));
    }

    #[test]
    fn export_rejects_unsupported_version() {
        let registry = ToolRegistry::new();
        let error = registry
            .export_kei_tool_manifest(Some(ManifestVersion(2)))
            .unwrap_err();
        assert_eq!(error, "only manifest v3 is supported");
    }

    #[test]
    fn connector_route_wins_over_local_dispatch() {
        let mut registry = ToolRegistry::new();
        registry.register(
            plain("local.echo", "Echo"),
            Some(harness_registration_for("local.echo", "Echo")),
        );
        registry.register(
            plain("github.get_issue", "Get issue"),
            Some(KeiToolRegistration {
                service: "github".into(),
                source: "github".into(),
                operation_class: "read".into(),
                route: ToolRoute {
                    connector_binding: Some(ConnectorBindingRoute {
                        agent_id: "agent-1".into(),
                        connector_id: "binding-1".into(),
                    }),
                    harness_executor: None,
                },
                required_capabilities: Some(vec!["issue.read".into()]),
                resource_types: Some(vec![KeiResourceType {
                    r#type: "issue".into(),
                    parent_type: String::new(),
                }]),
            }),
        );

        let manifest: Value = serde_json::from_slice(
            &registry
                .export_kei_tool_manifest(Some(MANIFEST_V3))
                .unwrap(),
        )
        .unwrap();
        assert_eq!(manifest["schema"], json!("kei.tool-manifest/v3"));
        let entries = manifest["tools"].as_array().unwrap();

        // The connector-bound tool kept its local dispatch handler.
        assert!(registry.get("github.get_issue").is_some());
        let connector = &entries[0];
        assert!(connector["route"].get("harness_executor").is_none());
        assert!(connector["route"].get("connector_binding").is_some());
        // parent_type omitted when empty (Go omitempty).
        assert!(connector["resource_types"][0].get("parent_type").is_none());

        let harness = &entries[1];
        assert!(harness.get("required_capabilities").is_none());
        assert!(harness.get("resource_types").is_none());
    }

    fn harness_registration_for(name: &str, description: &str) -> KeiToolRegistration {
        let _ = description;
        KeiToolRegistration {
            service: "local".into(),
            source: "harness".into(),
            operation_class: "write".into(),
            route: ToolRoute {
                connector_binding: None,
                harness_executor: Some(HarnessExecutorRoute {
                    executor: "pi".into(),
                    registration: name.into(),
                }),
            },
            required_capabilities: None,
            resource_types: None,
        }
    }

    #[test]
    fn export_rejects_connector_route_without_agent_id() {
        let mut registry = ToolRegistry::new();
        registry.register(
            plain("github.read", "Read"),
            Some(KeiToolRegistration {
                service: "github".into(),
                source: "github".into(),
                operation_class: "read".into(),
                route: ToolRoute {
                    connector_binding: Some(ConnectorBindingRoute {
                        agent_id: String::new(),
                        connector_id: "binding-1".into(),
                    }),
                    harness_executor: None,
                },
                required_capabilities: Some(vec!["issue.read".into()]),
                ..Default::default()
            }),
        );
        assert!(
            registry
                .export_kei_tool_manifest(Some(MANIFEST_V3))
                .is_err()
        );
    }

    #[test]
    fn export_rejects_empty_service() {
        let mut registry = ToolRegistry::new();
        registry.register(
            plain("local.echo", "Echo"),
            Some(KeiToolRegistration {
                service: String::new(),
                source: "harness".into(),
                operation_class: "write".into(),
                route: ToolRoute {
                    connector_binding: None,
                    harness_executor: Some(HarnessExecutorRoute {
                        executor: "pi".into(),
                        registration: "local.echo".into(),
                    }),
                },
                ..Default::default()
            }),
        );
        assert!(
            registry
                .export_kei_tool_manifest(Some(MANIFEST_V3))
                .is_err()
        );
    }

    #[test]
    fn export_excludes_ungoverned_tools() {
        let mut registry = ToolRegistry::new();
        registry.register(plain("local_echo", "Echo"), None);
        let manifest: Value =
            serde_json::from_slice(&registry.export_kei_tool_manifest(None).unwrap()).unwrap();
        assert_eq!(manifest["schema"], json!("kei.tool-manifest/v3"));
        assert_eq!(manifest["tools"], json!([]));
    }

    #[test]
    fn export_rejects_both_route_branches() {
        let mut registry = ToolRegistry::new();
        registry.register(
            plain("t", "T"),
            Some(KeiToolRegistration {
                service: "s".into(),
                source: "s".into(),
                operation_class: "read".into(),
                route: ToolRoute {
                    connector_binding: Some(ConnectorBindingRoute {
                        agent_id: "a".into(),
                        connector_id: "c".into(),
                    }),
                    harness_executor: Some(HarnessExecutorRoute {
                        executor: "e".into(),
                        registration: "r".into(),
                    }),
                },
                required_capabilities: Some(vec!["cap".into()]),
                ..Default::default()
            }),
        );
        let error = registry.export_kei_tool_manifest(None).unwrap_err();
        assert_eq!(error, "route must contain exactly one branch");
    }

    #[test]
    fn export_rejects_invalid_operation_class() {
        let mut registry = ToolRegistry::new();
        registry.register(
            plain("t", "T"),
            Some(KeiToolRegistration {
                service: "s".into(),
                source: "s".into(),
                operation_class: "admin".into(),
                route: ToolRoute {
                    connector_binding: None,
                    harness_executor: Some(HarnessExecutorRoute {
                        executor: "e".into(),
                        registration: "r".into(),
                    }),
                },
                ..Default::default()
            }),
        );
        let error = registry.export_kei_tool_manifest(None).unwrap_err();
        assert_eq!(error, "invalid operation_class");
    }

    #[test]
    fn export_rejects_harness_route_with_connector_fields() {
        let mut registry = ToolRegistry::new();
        registry.register(
            plain("t", "T"),
            Some(KeiToolRegistration {
                service: "s".into(),
                source: "s".into(),
                operation_class: "read".into(),
                route: ToolRoute {
                    connector_binding: None,
                    harness_executor: Some(HarnessExecutorRoute {
                        executor: "e".into(),
                        registration: "r".into(),
                    }),
                },
                required_capabilities: Some(vec!["cap".into()]),
                ..Default::default()
            }),
        );
        let error = registry.export_kei_tool_manifest(None).unwrap_err();
        assert_eq!(
            error,
            "harness route requires identity and omits connector-only fields"
        );
    }

    #[test]
    fn export_rejects_empty_capability() {
        let mut registry = ToolRegistry::new();
        registry.register(
            plain("t", "T"),
            Some(KeiToolRegistration {
                service: "s".into(),
                source: "s".into(),
                operation_class: "read".into(),
                route: ToolRoute {
                    connector_binding: Some(ConnectorBindingRoute {
                        agent_id: "a".into(),
                        connector_id: "c".into(),
                    }),
                    harness_executor: None,
                },
                required_capabilities: Some(vec!["  ".into()]),
                ..Default::default()
            }),
        );
        let error = registry.export_kei_tool_manifest(None).unwrap_err();
        assert_eq!(error, "connector capability must be non-empty");
    }

    #[test]
    fn register_replaces_stale_registration() {
        let mut registry = ToolRegistry::new();
        registry.register(plain("t", "T"), Some(connector_registration()));
        // Re-register without a registration: the stale one is deleted.
        registry.register(plain("t", "T"), None);
        let manifest: Value =
            serde_json::from_slice(&registry.export_kei_tool_manifest(None).unwrap()).unwrap();
        assert_eq!(manifest["tools"], json!([]));
    }
}
