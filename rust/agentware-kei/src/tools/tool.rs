//! Tool traits and Kei governance declarations (port of `go/tools/tool.go`).

use serde_json::{Map, Value};

use super::result::ToolResult;

/// A callable tool (Go `Tool`).
///
/// Go's `Execute(ctx, args map[string]any)` carries the caller in `ctx`; the
/// Rust executor seam passes the caller explicitly (see
/// `crate::executor::ToolExecutor`), so the tool itself only sees args.
pub trait Tool: Send + Sync {
    fn name(&self) -> &str;
    fn description(&self) -> &str;
    fn execute(&self, args: &Map<String, Value>) -> Result<ToolResult, String>;

    /// Rust analogue of Go's `t.(ExtendedTool)` type assertion: extended
    /// tools override this to return `Some(self)`.
    fn as_extended(&self) -> Option<&dyn ExtendedTool> {
        None
    }
}

/// A tool that also declares its JSON input schema and examples (Go
/// `ExtendedTool`).
pub trait ExtendedTool: Tool {
    /// Go returns a nilable `map[string]any`.
    fn input_schema(&self) -> Option<Map<String, Value>>;
    fn examples(&self) -> Vec<ToolExample>;
}

/// A usage example attached to an extended tool (Go `ToolExample`).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ToolExample {
    pub input: Map<String, Value>,
    pub output: String,
    pub explanation: String,
}

/// Declares the data source, capabilities, and resource types a tool uses
/// (Go `KeiScope`; json-tagged in Go for the legacy manifest path).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct KeiScope {
    pub source: String,
    pub required_capabilities: Vec<String>,
    pub resource_types: Vec<KeiResourceType>,
    pub operation_class: String,
    /// Go `json:"service,omitempty"`.
    pub service: String,
}

/// Identifies a resource kind and optional parent kind (Go
/// `KeiResourceType`).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct KeiResourceType {
    /// Go field `Type`.
    pub r#type: String,
    /// Go `json:"parent_type,omitempty"`.
    pub parent_type: String,
}

/// A tool that declares its Kei governance scope. Only governed tools were
/// included in the legacy (0.11.0) manifest export; the current export is
/// driven by `KeiToolRegistration` metadata (Go `GovernedTool`).
pub trait GovernedTool: Tool {
    fn kei_scope(&self) -> KeiScope;
}
