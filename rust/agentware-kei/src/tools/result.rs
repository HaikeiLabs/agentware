//! Tool execution results (port of `go/tools/result.go`).
//!
//! Go names the type `tools.Result`; the Rust port follows the crate-wide
//! convention (`toolformat::ToolResult`, `middleware::ToolResult`) and names
//! it `ToolResult`.

use serde_json::{Map, Value};

/// The result of a tool execution (Go `Result`).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ToolResult {
    pub success: bool,
    pub output: String,
    pub error: String,
    pub modified_files: Vec<String>,
    /// Go nilable `map[string]any`.
    pub metadata: Option<Map<String, Value>>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn result_fields_round_trip() {
        let mut metadata = Map::new();
        metadata.insert("duration".into(), json!(100));
        let result = ToolResult {
            success: true,
            output: "test output".into(),
            error: String::new(),
            modified_files: vec!["file1.go".into(), "file2.go".into()],
            metadata: Some(metadata),
        };

        assert!(result.success);
        assert_eq!(result.output, "test output");
        assert_eq!(result.modified_files.len(), 2);
        assert_eq!(result.metadata.as_ref().unwrap()["duration"], json!(100));
    }

    #[test]
    fn result_error() {
        let result = ToolResult {
            success: false,
            error: "something went wrong".into(),
            ..Default::default()
        };

        assert!(!result.success);
        assert_eq!(result.error, "something went wrong");
    }
}
