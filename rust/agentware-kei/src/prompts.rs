//! Minimal port of `go/prompts` — only the piece `go/executor` needs.
//!
//! `prompts.GenerateToolSection` (go/prompts/tool_section.go) is ported
//! here; `generator.go` and `schema.go` are out of scope for HAI-871 and
//! split to follow-up work.

use crate::llm::tokens::format_go_value;
use crate::tools::registry::ToolRegistry;

/// Builds the tool section of a system prompt (Go `GenerateToolSection`).
/// An empty registry renders as an empty string.
pub fn generate_tool_section(registry: &ToolRegistry) -> String {
    let tools = registry.all();
    if tools.is_empty() {
        return String::new();
    }

    let mut result = String::from("## Available Tools\n\n");
    for tool in tools {
        result.push_str(&format!("### {}\n{}\n\n", tool.name(), tool.description()));

        if let Some(extended) = tool.as_extended() {
            let examples = extended.examples();
            if !examples.is_empty() {
                result.push_str("Examples:\n");
                for example in &examples {
                    // Go: fmt.Sprintf("- Input: %v → Output: %s\n",
                    // ex.Input, ex.Output) — %v of the args map prints
                    // `map[k:v ...]` with keys sorted.
                    result.push_str(&format!(
                        "- Input: {} → Output: {}\n",
                        format_go_value(&serde_json::Value::Object(example.input.clone())),
                        example.output
                    ));
                }
                result.push('\n');
            }
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tools::result::ToolResult;
    use crate::tools::tool::{ExtendedTool, Tool, ToolExample};
    use serde_json::{Map, Value, json};
    use std::sync::Arc;

    struct BasicTool {
        name: String,
        description: String,
    }
    impl Tool for BasicTool {
        fn name(&self) -> &str {
            &self.name
        }
        fn description(&self) -> &str {
            &self.description
        }
        fn execute(&self, _args: &Map<String, Value>) -> Result<ToolResult, String> {
            Ok(ToolResult::default())
        }
    }

    struct ExtendedBasicTool {
        inner: BasicTool,
        examples: Vec<ToolExample>,
    }
    impl Tool for ExtendedBasicTool {
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
    impl ExtendedTool for ExtendedBasicTool {
        fn input_schema(&self) -> Option<Map<String, Value>> {
            None
        }
        fn examples(&self) -> Vec<ToolExample> {
            self.examples.clone()
        }
    }

    #[test]
    fn empty_registry_renders_empty() {
        assert_eq!(generate_tool_section(&ToolRegistry::new()), "");
    }

    #[test]
    fn renders_name_and_description_sorted() {
        let mut registry = ToolRegistry::new();
        registry.register(
            Arc::new(BasicTool {
                name: "tool_b".into(),
                description: "Second".into(),
            }),
            None,
        );
        registry.register(
            Arc::new(BasicTool {
                name: "tool_a".into(),
                description: "First".into(),
            }),
            None,
        );

        assert_eq!(
            generate_tool_section(&registry),
            "## Available Tools\n\n### tool_a\nFirst\n\n### tool_b\nSecond\n\n"
        );
    }

    #[test]
    fn renders_examples_with_go_map_format() {
        let mut input = Map::new();
        input.insert("path".into(), json!("a/b.go"));
        input.insert("mode".into(), json!("write"));
        let mut registry = ToolRegistry::new();
        registry.register(
            Arc::new(ExtendedBasicTool {
                inner: BasicTool {
                    name: "edit".into(),
                    description: "Edit a file".into(),
                },
                examples: vec![ToolExample {
                    input,
                    output: "ok".into(),
                    explanation: String::new(),
                }],
            }),
            None,
        );

        // Go %v of the input map: keys sorted, `map[k:v ...]`.
        assert_eq!(
            generate_tool_section(&registry),
            "## Available Tools\n\n### edit\nEdit a file\n\nExamples:\n\
             - Input: map[mode:write path:a/b.go] → Output: ok\n\n"
        );
    }
}
