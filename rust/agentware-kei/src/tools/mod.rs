//! Tool registry, results, governance declarations, dispatch, and manifest
//! linting (port of `go/tools`).

pub mod executor;
pub mod lint;
pub mod registry;
pub mod result;
pub mod tool;

pub use executor::{RegistryExecutor, ToolError, ToolExecutor, err_tool_not_found};
pub use lint::lint_kei_tool_manifest;
pub use registry::{
    ConnectorBindingRoute, HarnessExecutorRoute, KeiToolRegistration, MANIFEST_V3, ManifestVersion,
    ToolRegistry, ToolRoute,
};
pub use result::ToolResult;
pub use tool::{ExtendedTool, GovernedTool, KeiResourceType, KeiScope, Tool, ToolExample};
