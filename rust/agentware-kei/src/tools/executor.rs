//! Registry-backed tool dispatch (port of `go/tools/executor.go`).

use std::sync::Arc;

use serde_json::{Map, Value, json};

use super::registry::ToolRegistry;
use super::result::ToolResult;
use crate::evaluator::CallerContext;

/// A dispatch error with a stable code (Go `ToolError`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolError {
    pub code: String,
    pub message: String,
    /// The wrapped cause (Go `Err error`).
    pub source: Option<String>,
}

impl ToolError {
    /// Go `Error()`: `code: message` or `code: message: err`.
    pub fn error_string(&self) -> String {
        match &self.source {
            Some(source) => format!("{}: {}: {}", self.code, self.message, source),
            None => format!("{}: {}", self.code, self.message),
        }
    }
}

impl std::fmt::Display for ToolError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.error_string())
    }
}

/// Returned when a tool is not found in the registry (Go `ErrToolNotFound`:
/// `&ToolError{Code: "TOOL_NOT_FOUND", Message: "tool not found in registry"}`).
pub fn err_tool_not_found() -> ToolError {
    ToolError {
        code: "TOOL_NOT_FOUND".into(),
        message: "tool not found in registry".into(),
        source: None,
    }
}

/// A tool executor that dispatches to tools in a registry (Go
/// `RegistryExecutor`).
pub struct RegistryExecutor {
    registry: Arc<ToolRegistry>,
}

impl RegistryExecutor {
    /// Go `NewRegistryExecutor`.
    pub fn new(registry: Arc<ToolRegistry>) -> Self {
        Self { registry }
    }

    /// Looks up the tool in the registry and executes it (Go `Execute`).
    /// Missing tools return the [`err_tool_not_found`] error string; tool
    /// errors propagate unchanged.
    pub fn execute(
        &self,
        tool_name: &str,
        args: &Map<String, Value>,
    ) -> Result<ToolResult, String> {
        let tool = self
            .registry
            .get(tool_name)
            .ok_or_else(|| err_tool_not_found().error_string())?;
        tool.execute(args)
    }
}

/// The dispatch seam the inference loop consumes (Go `middleware.ToolExecutor`,
/// `Execute(ctx, toolName, args) (*tools.Result, error)`; the caller travels
/// in Go's `ctx` via `middleware.WithCallerContext` and is an explicit
/// argument here).
pub trait ToolExecutor: Send + Sync {
    fn execute(
        &self,
        tool_name: &str,
        args: &Value,
        caller: &CallerContext,
    ) -> Result<ToolResult, String>;
}

impl ToolExecutor for RegistryExecutor {
    fn execute(
        &self,
        tool_name: &str,
        args: &Value,
        _caller: &CallerContext,
    ) -> Result<ToolResult, String> {
        let Some(args) = args.as_object() else {
            return Err("tool arguments must be a JSON object".into());
        };
        self.execute(tool_name, args)
    }
}

/// Adapter to the crate middleware's simplified `Value` seam: Go's middleware
/// passes the `*tools.Result` through structurally; this port serializes it
/// with Go's default (untagged) `encoding/json` field names for
/// `tools.Result`, so the mapping is citeable to `go/tools/result.go`.
impl crate::middleware::ToolExecutor for RegistryExecutor {
    fn execute(&self, tool_name: &str, args: &Value) -> Result<Value, String> {
        let Some(args) = args.as_object() else {
            return Err("tool arguments must be a JSON object".into());
        };
        let result = self.execute(tool_name, args)?;
        Ok(json!({
            "Success": result.success,
            "Output": result.output,
            "Error": result.error,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tools::tool::Tool;
    use serde_json::json;
    use std::sync::Mutex;

    type ExecuteFn = Box<dyn Fn(&Map<String, Value>) -> Result<ToolResult, String> + Send + Sync>;

    struct MockTool {
        name: String,
        description: String,
        execute_fn: Mutex<ExecuteFn>,
    }
    impl Tool for MockTool {
        fn name(&self) -> &str {
            &self.name
        }
        fn description(&self) -> &str {
            &self.description
        }
        fn execute(&self, args: &Map<String, Value>) -> Result<ToolResult, String> {
            (self.execute_fn.lock().unwrap())(args)
        }
    }

    fn tool_with(
        name: &str,
        f: impl Fn(&Map<String, Value>) -> Result<ToolResult, String> + Send + Sync + 'static,
    ) -> Arc<dyn Tool> {
        Arc::new(MockTool {
            name: name.into(),
            description: "A test tool".into(),
            execute_fn: Mutex::new(Box::new(f)),
        })
    }

    #[test]
    fn executes_registered_tool() {
        let mut registry = ToolRegistry::new();
        registry.register(
            tool_with("test_tool", |_| {
                Ok(ToolResult {
                    success: true,
                    output: "executed".into(),
                    ..Default::default()
                })
            }),
            None,
        );
        let executor = RegistryExecutor::new(Arc::new(registry));

        let result = ToolExecutor::execute(
            &executor,
            "test_tool",
            &json!({"key": "value"}),
            &CallerContext::default(),
        )
        .expect("execute");
        assert!(result.success);
        assert_eq!(result.output, "executed");
    }

    #[test]
    fn missing_tool_is_not_found() {
        let executor = RegistryExecutor::new(Arc::new(ToolRegistry::new()));
        let error =
            ToolExecutor::execute(&executor, "missing", &json!({}), &CallerContext::default())
                .unwrap_err();
        assert_eq!(error, "TOOL_NOT_FOUND: tool not found in registry");
    }

    #[test]
    fn tool_error_formats_with_source() {
        let error = ToolError {
            code: "CODE".into(),
            message: "message".into(),
            source: Some("cause".into()),
        };
        assert_eq!(error.error_string(), "CODE: message: cause");

        let plain = ToolError {
            code: "CODE".into(),
            message: "message".into(),
            source: None,
        };
        assert_eq!(plain.error_string(), "CODE: message");
    }

    #[test]
    fn value_seem_uses_go_default_field_names() {
        let mut registry = ToolRegistry::new();
        registry.register(
            tool_with("t", |_| {
                Ok(ToolResult {
                    success: true,
                    output: "out".into(),
                    ..Default::default()
                })
            }),
            None,
        );
        let executor = RegistryExecutor::new(Arc::new(registry));

        let value =
            crate::middleware::ToolExecutor::execute(&executor, "t", &json!({})).expect("execute");
        assert_eq!(
            value,
            json!({"Success": true, "Output": "out", "Error": ""})
        );
    }
}
