//! Manifest lint rules (port of `go/tools/lint.go`).

use serde_json::Value;
use std::collections::HashSet;

/// Retired approval keys (Go `retiredKeys`).
const RETIRED_KEYS: [&str; 5] = [
    "allow",
    "approval",
    "approval_id",
    "approval_required",
    "requires_approval",
];

/// Go `resourceIdentifier` regex: `^[A-Za-z][A-Za-z0-9_.-]*$`.
fn is_resource_identifier(text: &str) -> bool {
    let mut chars = text.chars();
    match chars.next() {
        Some(first) if first.is_ascii_alphabetic() => {}
        _ => return false,
    }
    chars.all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '.' || c == '-')
}

/// Returns the v3 manifest rule diagnostics in stable (sorted) order (Go
/// `LintKeiToolManifest`). `capability_file` mirrors the Go variadic: `None`
/// resolves the default `fixtures/kei/connector-capabilities.v0.5.0.json`
/// table relative to the repository root.
pub fn lint_kei_tool_manifest(raw: &[u8], capability_file: Option<&str>) -> Vec<String> {
    let manifest: Value = match serde_json::from_slice(raw) {
        Ok(value) => value,
        Err(error) => return vec![format!("invalid JSON: {error}")],
    };
    if manifest.get("schema").and_then(Value::as_str) != Some("kei.tool-manifest/v3") {
        return vec!["schema must be kei.tool-manifest/v3".into()];
    }

    let capability_path = match capability_file {
        Some(path) => std::path::PathBuf::from(path),
        None => {
            // Go: fixtures/kei/... with a ../ fallback for its package
            // working directory; the Rust port resolves from the repository
            // root.
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .and_then(std::path::Path::parent)
                .expect("crate lives two levels below the repository root")
                .join("fixtures/kei/connector-capabilities.v0.5.0.json")
        }
    };
    let capability_data = match std::fs::read(&capability_path) {
        Ok(data) => data,
        Err(error) => {
            return vec![format!(
                "cannot read capability table: {}",
                io_error(&capability_path, &error)
            )];
        }
    };
    let caps: Value = match serde_json::from_slice(&capability_data) {
        Ok(value) => value,
        Err(_) => return vec!["invalid capability table".into()],
    };

    let mut errors: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    let mut names: Vec<String> = Vec::new();

    let tools = manifest
        .get("tools")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    for (index, tool) in tools.iter().enumerate() {
        let prefix = format!("tools[{index}]");
        let name = tool.get("name").and_then(Value::as_str).unwrap_or("");
        names.push(name.to_owned());
        if seen.contains(name) {
            errors.push(format!("duplicate tool name: {name}"));
        }
        seen.insert(name.to_owned());

        let source = tool.get("source").and_then(Value::as_str).unwrap_or("");
        if source.is_empty() {
            errors.push(format!("{prefix} is missing source"));
        }

        let route = tool.get("route").and_then(Value::as_object);
        let connector = route.and_then(|route| route.get("connector_binding"));
        let harness = route.and_then(|route| route.get("harness_executor"));
        let has_connector = connector.is_some();
        let has_harness = harness.is_some();
        if has_connector == has_harness {
            errors.push(format!(
                "{prefix} route must contain exactly one route kind"
            ));
        }
        if has_connector && string_is_empty_or_missing_blank(connector, "agent_id", "connector_id")
        {
            errors.push(format!(
                "{prefix} connector_binding requires agent_id and connector_id"
            ));
        }
        if has_harness && string_is_empty_or_missing_blank(harness, "executor", "registration") {
            errors.push(format!(
                "{prefix} harness_executor requires executor and registration"
            ));
        }

        let capabilities = tool.get("required_capabilities").and_then(Value::as_array);
        if has_connector && capabilities.is_none_or(|values| values.is_empty()) {
            errors.push(format!("{prefix} is missing required_capabilities"));
        } else if let Some(values) = capabilities {
            if has_connector {
                let mut capability_names: Vec<String> = Vec::with_capacity(values.len());
                for raw in values {
                    let capability = raw.as_str().unwrap_or("");
                    capability_names.push(capability.to_owned());
                    if !declares_capability(&caps, source, capability) {
                        errors.push(format!(
                            "{prefix} capability {capability:?} is not declared for source {source:?}"
                        ));
                    }
                }
                if !is_sorted(&capability_names) {
                    errors.push(format!("{prefix} required_capabilities are not sorted"));
                }
            }
        }

        let resources = tool.get("resource_types").and_then(Value::as_array);
        let mut resource_keys: Vec<String> = Vec::new();
        if let Some(resources) = resources {
            for value in resources {
                let item = value.as_object();
                let type_name = item
                    .and_then(|item| item.get("type"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let parent_name = item
                    .and_then(|item| item.get("parent_type"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                resource_keys.push(format!("{type_name}\0{parent_name}"));

                // Go checks "type" always (a missing key prints `<nil>`) and
                // "parent_type" only when present; both print `%v` of the
                // raw value.
                let type_value = item.and_then(|item| item.get("type"));
                if !is_resource_identifier(type_value.and_then(Value::as_str).unwrap_or("")) {
                    errors.push(format!(
                        "{prefix} invalid resource type type: {}",
                        type_value
                            .map(format_go_value)
                            .unwrap_or_else(|| "<nil>".into())
                    ));
                }
                if let Some(parent_value) = item.and_then(|item| item.get("parent_type")) {
                    if !is_resource_identifier(parent_value.as_str().unwrap_or("")) {
                        errors.push(format!(
                            "{prefix} invalid resource type parent_type: {}",
                            format_go_value(parent_value)
                        ));
                    }
                }
            }
        }
        if !is_sorted(&resource_keys) {
            errors.push(format!("{prefix} resource_types are not sorted"));
        }

        let operation_class = tool.get("operation_class").and_then(Value::as_str);
        if operation_class != Some("read") && operation_class != Some("write") {
            errors.push(format!("{prefix} operation_class must be read or write"));
        }

        if has_retired(tool) {
            errors.push(format!(
                "{prefix} contains allow or a retired approval field"
            ));
        }
    }

    if !is_sorted(&names) {
        errors.push("tools are not sorted by name".into());
    }
    errors.sort();
    errors
}

/// Go formats OS errors as `open <path>: <err>` through os.ReadFile.
fn io_error(path: &std::path::Path, error: &std::io::Error) -> String {
    format!("open {}: {}", path.display(), error)
}

/// Go compares `connector["agent_id"] == ""`: only an empty STRING triggers
/// the error; a missing key or a non-string value does not.
fn string_is_empty_or_missing_blank(value: Option<&Value>, first: &str, second: &str) -> bool {
    let is_empty_string =
        |field: Option<&Value>| matches!(field, Some(Value::String(text)) if text.is_empty());
    let object = match value.and_then(Value::as_object) {
        Some(object) => object,
        None => return false,
    };
    is_empty_string(object.get(first)) || is_empty_string(object.get(second))
}

/// Whether the capability table declares `capability` for `source`.
fn declares_capability(caps: &Value, source: &str, capability: &str) -> bool {
    caps.get(source)
        .and_then(Value::as_array)
        .map(|values| {
            values
                .iter()
                .any(|value| value.as_str() == Some(capability))
        })
        .unwrap_or(false)
}

/// Go `sort.StringsAreSorted`.
fn is_sorted(values: &[String]) -> bool {
    values.windows(2).all(|window| window[0] <= window[1])
}

/// Formats a value like Go's `%v` for the diagnostic text.
fn format_go_value(value: &Value) -> String {
    match value {
        Value::Null => "<nil>".into(),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

/// Go `hasRetired`: any key that is retired or contains "approval"
/// (case-insensitive), recursively; or a string value equal-fold "allow".
fn has_retired(value: &Value) -> bool {
    match value {
        Value::Object(map) => {
            for (key, child) in map {
                let lower = key.to_lowercase();
                if RETIRED_KEYS.contains(&lower.as_str()) || lower.contains("approval") {
                    return true;
                }
                if has_retired(child) {
                    return true;
                }
            }
            false
        }
        Value::Array(items) => items.iter().any(has_retired),
        Value::String(text) => text.eq_ignore_ascii_case("allow"),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn fixture_path(name: &str) -> String {
        std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(std::path::Path::parent)
            .expect("crate lives two levels below the repository root")
            .join("fixtures/kei")
            .join(name)
            .to_string_lossy()
            .to_string()
    }

    #[test]
    fn shared_fixture_lints_clean() {
        let raw = std::fs::read(
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .and_then(std::path::Path::parent)
                .expect("repo root")
                .join("fixtures/kei/tool-manifest.v3.json"),
        )
        .expect("fixture");
        let errors = lint_kei_tool_manifest(
            &raw,
            Some(&fixture_path("connector-capabilities.v0.5.0.json")),
        );
        assert!(errors.is_empty(), "unexpected lint errors: {errors:?}");
    }

    #[test]
    fn rejects_unsupported_schema() {
        let errors = lint_kei_tool_manifest(br#"{"schema":"unsupported","tools":[]}"#, None);
        assert_eq!(errors, vec!["schema must be kei.tool-manifest/v3"]);
    }

    #[test]
    fn rejects_invalid_json() {
        let errors = lint_kei_tool_manifest(b"{not json", None);
        assert_eq!(errors.len(), 1);
        assert!(errors[0].starts_with("invalid JSON: "));
    }

    #[test]
    fn rejects_retired_approval_and_invalid_capability() {
        let manifest = json!({
            "schema": "kei.tool-manifest/v3",
            "tools": [{
                "name": "bad",
                "source": "github",
                "required_capabilities": ["admin.raw_sql"],
                "resource_types": [{"type": "repo:org/*"}],
                "operation_class": "read",
                "allow": true,
                "route": {"connector_binding": {"agent_id": "agent", "connector_id": "binding"}},
            }],
        });
        let raw = serde_json::to_vec(&manifest).unwrap();
        let errors = lint_kei_tool_manifest(
            &raw,
            Some(&fixture_path("connector-capabilities.v0.5.0.json")),
        );
        assert!(
            errors.len() >= 3,
            "expected capability, resource, and allow errors; got {errors:?}"
        );
        assert!(
            errors
                .iter()
                .any(|error| error.contains("is not declared for source"))
        );
        assert!(
            errors
                .iter()
                .any(|error| error.contains("invalid resource type"))
        );
        assert!(
            errors
                .iter()
                .any(|error| error.contains("contains allow or a retired approval field"))
        );
    }

    #[test]
    fn empty_string_route_fields_do_not_trigger_go_missing_key_quirk() {
        // Go: only an empty STRING triggers the connector/harness field
        // errors; a missing key does not.
        let manifest = json!({
            "schema": "kei.tool-manifest/v3",
            "tools": [{
                "name": "t",
                "source": "github",
                "operation_class": "read",
                "route": {"connector_binding": {}},
                "required_capabilities": ["issue.read"],
            }],
        });
        let raw = serde_json::to_vec(&manifest).unwrap();
        let errors = lint_kei_tool_manifest(
            &raw,
            Some(&fixture_path("connector-capabilities.v0.5.0.json")),
        );
        // No "connector_binding requires agent_id" error for missing keys.
        assert!(
            !errors
                .iter()
                .any(|error| error.contains("connector_binding requires")),
            "{errors:?}"
        );
    }

    #[test]
    fn unsorted_tools_and_capabilities_are_reported() {
        let manifest = json!({
            "schema": "kei.tool-manifest/v3",
            "tools": [
                {"name": "b", "source": "github", "operation_class": "read",
                 "route": {"connector_binding": {"agent_id": "a", "connector_id": "c"}},
                 "required_capabilities": ["issue.write", "issue.read"]},
                {"name": "a", "source": "github", "operation_class": "read",
                 "route": {"connector_binding": {"agent_id": "a", "connector_id": "c"}},
                 "required_capabilities": ["issue.read"]},
            ],
        });
        let raw = serde_json::to_vec(&manifest).unwrap();
        let errors = lint_kei_tool_manifest(
            &raw,
            Some(&fixture_path("connector-capabilities.v0.5.0.json")),
        );
        assert!(
            errors
                .iter()
                .any(|error| error == "tools are not sorted by name")
        );
        assert!(
            errors
                .iter()
                .any(|error| error.contains("required_capabilities are not sorted"))
        );
    }

    #[test]
    fn retired_key_nested_in_args_is_caught() {
        let manifest = json!({
            "schema": "kei.tool-manifest/v3",
            "tools": [{
                "name": "t",
                "source": "github",
                "operation_class": "read",
                "route": {"connector_binding": {"agent_id": "a", "connector_id": "c"}},
                "required_capabilities": ["issue.read"],
                "nested": {"requires_approval": false},
            }],
        });
        let raw = serde_json::to_vec(&manifest).unwrap();
        let errors = lint_kei_tool_manifest(
            &raw,
            Some(&fixture_path("connector-capabilities.v0.5.0.json")),
        );
        assert!(
            errors
                .iter()
                .any(|error| error.contains("contains allow or a retired approval field"))
        );
    }
}
