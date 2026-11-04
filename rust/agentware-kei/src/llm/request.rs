//! Request-side types (port of `go/llm/request.go`).

use super::response::ToolCall;

/// The kind of conversation turn a message carries (Go `MessageType`).
///
/// Go models this as a string with an empty zero value; unknown labels round
/// trip through [`MessageType::Other`], and the zero value is
/// [`MessageType::Other`] with an empty string.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum MessageType {
    SystemPrompt,
    UserInput,
    ToolCall,
    ToolResult,
    Reasoning,
    TextResponse,
    StepNudge,
    PrerequisiteNudge,
    RetryNudge,
    ContextWarning,
    Summary,
    /// Any label outside the known set, including Go's empty zero value.
    Other(String),
}

impl Default for MessageType {
    fn default() -> Self {
        // Go's zero MessageType is the empty string.
        MessageType::Other(String::new())
    }
}

impl MessageType {
    /// The Go wire/label spelling of this message type.
    pub fn as_str(&self) -> &str {
        match self {
            MessageType::SystemPrompt => "system_prompt",
            MessageType::UserInput => "user_input",
            MessageType::ToolCall => "tool_call",
            MessageType::ToolResult => "tool_result",
            MessageType::Reasoning => "reasoning",
            MessageType::TextResponse => "text_response",
            MessageType::StepNudge => "step_nudge",
            MessageType::PrerequisiteNudge => "prerequisite_nudge",
            MessageType::RetryNudge => "retry_nudge",
            MessageType::ContextWarning => "context_warning",
            MessageType::Summary => "summary",
            MessageType::Other(value) => value,
        }
    }
}

impl From<&str> for MessageType {
    fn from(value: &str) -> Self {
        match value {
            "system_prompt" => MessageType::SystemPrompt,
            "user_input" => MessageType::UserInput,
            "tool_call" => MessageType::ToolCall,
            "tool_result" => MessageType::ToolResult,
            "reasoning" => MessageType::Reasoning,
            "text_response" => MessageType::TextResponse,
            "step_nudge" => MessageType::StepNudge,
            "prerequisite_nudge" => MessageType::PrerequisiteNudge,
            "retry_nudge" => MessageType::RetryNudge,
            "context_warning" => MessageType::ContextWarning,
            "summary" => MessageType::Summary,
            _ => MessageType::Other(value.to_owned()),
        }
    }
}

/// Metadata attached to a message by the agent loop (Go `MessageMeta`).
///
/// All fields are optional pointers in Go; `message_type` is Go's string with
/// an empty zero value, represented as [`MessageType::Other`].
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct MessageMeta {
    pub message_type: MessageType,
    pub step_index: Option<i64>,
    pub original_type: Option<MessageType>,
    pub token_estimate: Option<i64>,
}

/// Extended-thinking configuration (Go `ThinkingConfig`).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ThinkingConfig {
    /// Go field `Type`.
    pub r#type: String,
    pub max_tokens: i64,
}

/// A single turn in a conversation (Go `Message`).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Message {
    pub role: Role,
    pub content: String,
    pub tool_call_id: String,
    pub tool_calls: Vec<ToolCall>,
    pub meta: MessageMeta,
}

/// The role of a message sender (Go `Role`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum Role {
    #[default]
    System,
    User,
    Assistant,
    Tool,
}

impl Role {
    /// The Go string spelling of this role.
    pub fn as_str(&self) -> &'static str {
        match self {
            Role::System => "system",
            Role::User => "user",
            Role::Assistant => "assistant",
            Role::Tool => "tool",
        }
    }
}

/// The schema for a tool, used in native tool-calling requests (Go
/// `ToolDefinition`).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ToolDefinition {
    pub name: String,
    pub description: String,
    pub input_schema: Option<serde_json::Map<String, serde_json::Value>>,
}

/// The input to a completion (Go `Request`).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Request {
    pub messages: Vec<Message>,
    pub tools: Vec<ToolDefinition>,
    pub temperature: f64,
    pub max_tokens: i64,
    /// Go `Stop`; `None` serializes as JSON `null` like Go's nil slice.
    pub stop: Option<Vec<String>>,
    pub thinking: Option<ThinkingConfig>,
}
