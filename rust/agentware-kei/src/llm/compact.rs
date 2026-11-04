//! Compaction strategies (port of `go/llm/compact.go`).

use super::request::{Message, MessageType};
use super::tokens::estimate_messages_tokens;
use std::sync::Arc;

/// Sentinel for compacting an empty conversation (Go `ErrEmptyMessages`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CompactError {
    /// Go `ErrEmptyMessages`: "no messages to compact".
    EmptyMessages,
}

impl std::fmt::Display for CompactError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CompactError::EmptyMessages => write!(f, "no messages to compact"),
        }
    }
}

/// Counts the tokens of a conversation (Go `TokenCounter func([]Message) int`).
pub type TokenCounter = std::sync::Arc<dyn Fn(&[Message]) -> i64 + Send + Sync>;

/// Emitted after a compaction (Go `CompactEvent`).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct CompactEvent {
    pub step_index: i64,
    pub tokens_before: i64,
    pub tokens_after: i64,
    pub budget_tokens: i64,
    pub messages_before: i64,
    pub messages_after: i64,
    pub phase_reached: i64,
    pub strategy_name: String,
}

/// A compaction strategy (Go `CompactionStrategy`).
pub trait CompactionStrategy {
    /// Compacts `messages` down toward `target_tokens`, counted with
    /// `counter`.
    fn compact(
        &mut self,
        messages: &[Message],
        target_tokens: i64,
        counter: &TokenCounter,
    ) -> Result<Vec<Message>, CompactError>;

    /// The strategy name reported in [`CompactEvent::strategy_name`].
    fn name(&self) -> &'static str;
}

/// Reports the deepest phase a strategy reached (Go `PhaseReporter`).
pub trait PhaseReporter {
    fn last_phase(&self) -> i64;
}

/// Three-phase compaction (Go `TieredCompact`).
///
/// Phase 1 drops nudge messages and truncates tool results, phase 2 drops
/// tool results, phase 3 drops reasoning and text responses. The first two
/// messages (system prompt and the first user input) and the most recent
/// `keep_recent` step indexes are always protected.
#[derive(Debug, Clone)]
pub struct TieredCompact {
    pub keep_recent: i64,
    /// Declared in Go but unused by its `Compact` implementation; kept for
    /// shape parity.
    pub phase_thresholds: [f64; 3],
    pub truncate_chars: usize,
    last_phase: i64,
}

impl Default for TieredCompact {
    fn default() -> Self {
        Self::new()
    }
}

impl TieredCompact {
    /// Go `NewTieredCompact`: KeepRecent 2, PhaseThresholds {0.75, 0.75,
    /// 0.75}, TruncateChars 200.
    pub fn new() -> Self {
        Self {
            keep_recent: 2,
            phase_thresholds: [0.75, 0.75, 0.75],
            truncate_chars: 200,
            last_phase: 0,
        }
    }
}

impl PhaseReporter for TieredCompact {
    fn last_phase(&self) -> i64 {
        self.last_phase
    }
}

impl CompactionStrategy for TieredCompact {
    fn name(&self) -> &'static str {
        "TieredCompact"
    }

    fn compact(
        &mut self,
        messages: &[Message],
        target_tokens: i64,
        counter: &TokenCounter,
    ) -> Result<Vec<Message>, CompactError> {
        if messages.is_empty() {
            return Err(CompactError::EmptyMessages);
        }

        let mut result = messages.to_vec();

        let eligible_end = find_eligible_end(&result, self.keep_recent);

        let mut current_tokens = counter(&result);

        if current_tokens <= target_tokens {
            self.last_phase = 0;
            return Ok(result);
        }

        result = self.phase1_compact(&result, eligible_end);
        current_tokens = counter(&result);
        if current_tokens <= target_tokens {
            self.last_phase = 1;
            return Ok(result);
        }

        result = self.phase2_compact(&result, eligible_end);
        current_tokens = counter(&result);
        if current_tokens <= target_tokens {
            self.last_phase = 2;
            return Ok(result);
        }

        result = self.phase3_compact(&result, eligible_end);
        self.last_phase = 3;
        Ok(result)
    }
}

/// Index of the last message eligible for compaction (Go `findEligibleEnd`).
///
/// Without step indexes this protects the last `keep_recent` messages; with
/// step indexes it protects the highest `keep_recent` step numbers and
/// returns the last index whose step is not protected. Returns `-1` when
/// every message is protected (mirrors the Go sentinel).
fn find_eligible_end(messages: &[Message], keep_recent: i64) -> i64 {
    if keep_recent <= 0 {
        return messages.len() as i64 - 1;
    }

    let mut max_step: i64 = -1;
    for message in messages {
        if let Some(step_index) = message.meta.step_index {
            if step_index > max_step {
                max_step = step_index;
            }
        }
    }

    if max_step < 0 {
        let protected_count = keep_recent.min(messages.len() as i64);
        return messages.len() as i64 - 1 - protected_count;
    }

    let mut protected_steps = std::collections::HashSet::new();
    let mut current_step = max_step;
    for _ in 0..keep_recent {
        protected_steps.insert(current_step);
        current_step -= 1;
        if current_step < 0 {
            break;
        }
    }

    for (index, message) in messages.iter().enumerate().rev() {
        let step_index = message.meta.step_index.unwrap_or(0);
        if !protected_steps.contains(&step_index) {
            return index as i64;
        }
    }

    -1
}

/// Whether the message type is one of the nudge types (Go `isNudgeType`).
fn is_nudge_type(message_type: &MessageType) -> bool {
    matches!(
        message_type,
        MessageType::StepNudge | MessageType::RetryNudge | MessageType::PrerequisiteNudge
    )
}

impl TieredCompact {
    /// Step indexes protected from compaction (Go `protectedSteps`): the last
    /// `keep_recent` distinct step indexes, or nothing when `keep_recent <= 0`.
    fn protected_steps(&self, messages: &[Message]) -> std::collections::HashSet<i64> {
        let mut protected = std::collections::HashSet::new();
        if self.keep_recent <= 0 {
            return protected;
        }

        let mut steps: Vec<i64> = Vec::new();
        let mut step_set = std::collections::HashSet::new();
        for message in messages {
            if let Some(step_index) = message.meta.step_index {
                if step_set.insert(step_index) {
                    steps.push(step_index);
                }
            }
        }

        if steps.is_empty() {
            return protected;
        }

        if self.keep_recent >= steps.len() as i64 {
            protected.extend(steps);
            return protected;
        }

        let start_index = steps.len() - self.keep_recent as usize;
        protected.extend(steps[start_index..].iter().copied());
        protected
    }

    fn is_protected(
        &self,
        message: &Message,
        protected_steps: &std::collections::HashSet<i64>,
    ) -> bool {
        match message.meta.step_index {
            None => false,
            Some(step_index) => protected_steps.contains(&step_index),
        }
    }

    /// Phase 1: drop nudge messages, truncate long tool results (Go
    /// `phase1Compact`). The first two messages are always kept.
    fn phase1_compact(&self, messages: &[Message], _eligible_end: i64) -> Vec<Message> {
        let mut result = Vec::with_capacity(messages.len());
        let protected = self.protected_steps(messages);

        for (index, message) in messages.iter().enumerate() {
            if index == 0 || index == 1 {
                result.push(message.clone());
                continue;
            }

            if self.is_protected(message, &protected) {
                result.push(message.clone());
                continue;
            }

            if is_nudge_type(&message.meta.message_type) {
                continue;
            }

            if message.meta.message_type == MessageType::ToolResult
                && message.content.len() > self.truncate_chars
            {
                let mut truncated = message.clone();
                truncated.content = truncate_bytes(&message.content, self.truncate_chars);
                result.push(truncated);
                continue;
            }

            result.push(message.clone());
        }

        result
    }

    /// Phase 2: additionally drop tool results (Go `phase2Compact`).
    fn phase2_compact(&self, messages: &[Message], _eligible_end: i64) -> Vec<Message> {
        let mut result = Vec::with_capacity(messages.len());
        let protected = self.protected_steps(messages);

        for (index, message) in messages.iter().enumerate() {
            if index == 0 || index == 1 {
                result.push(message.clone());
                continue;
            }

            if self.is_protected(message, &protected) {
                result.push(message.clone());
                continue;
            }

            if message.meta.message_type == MessageType::ToolResult {
                continue;
            }

            result.push(message.clone());
        }

        result
    }

    /// Phase 3: additionally drop reasoning and text responses (Go
    /// `phase3Compact`).
    fn phase3_compact(&self, messages: &[Message], _eligible_end: i64) -> Vec<Message> {
        let mut result = Vec::with_capacity(messages.len());
        let protected = self.protected_steps(messages);

        for (index, message) in messages.iter().enumerate() {
            if index == 0 || index == 1 {
                result.push(message.clone());
                continue;
            }

            if self.is_protected(message, &protected) {
                result.push(message.clone());
                continue;
            }

            if message.meta.message_type == MessageType::Reasoning
                || message.meta.message_type == MessageType::TextResponse
            {
                continue;
            }

            result.push(message.clone());
        }

        result
    }
}

/// Byte truncation like Go's `content[:n]` slice, but landing on the largest
/// UTF-8 character boundary at or before `n` bytes (Rust strings must stay
/// valid UTF-8, so a mid-character split would panic).
fn truncate_bytes(content: &str, max_bytes: usize) -> String {
    if content.len() <= max_bytes {
        return content.to_owned();
    }
    let mut end = max_bytes;
    while end > 0 && !content.is_char_boundary(end) {
        end -= 1;
    }
    content[..end].to_owned()
}

/// Convenience counter over [`estimate_messages_tokens`], mirroring the Go
/// tests' counter closure.
pub fn estimate_messages_counter() -> TokenCounter {
    Arc::new(|messages: &[Message]| estimate_messages_tokens(messages))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::context_window::{default_counter, estimate_messages_counter};
    use crate::llm::request::{MessageMeta, Role};
    use crate::llm::response::ToolCall;

    fn counter() -> TokenCounter {
        estimate_messages_counter()
    }

    fn meta(message_type: MessageType, step_index: Option<i64>) -> MessageMeta {
        MessageMeta {
            message_type,
            step_index,
            ..Default::default()
        }
    }

    #[test]
    fn tiered_compact_name() {
        assert_eq!(TieredCompact::new().name(), "TieredCompact");
    }

    #[test]
    fn phase1_drops_nudges() {
        let mut compact = TieredCompact::new();
        compact.keep_recent = 0;

        let messages = vec![
            Message {
                role: Role::System,
                content: "system".into(),
                meta: meta(MessageType::SystemPrompt, None),
                ..Default::default()
            },
            Message {
                role: Role::User,
                content: "user".into(),
                meta: meta(MessageType::UserInput, None),
                ..Default::default()
            },
            Message {
                role: Role::Assistant,
                content: "".into(),
                meta: meta(MessageType::StepNudge, Some(0)),
                ..Default::default()
            },
            Message {
                role: Role::Assistant,
                content: "text".into(),
                meta: meta(MessageType::TextResponse, Some(0)),
                ..Default::default()
            },
        ];

        let result = compact.compact(&messages, 5, &counter()).unwrap();
        assert!(
            !result
                .iter()
                .any(|message| message.meta.message_type == MessageType::StepNudge)
        );
    }

    #[test]
    fn phase1_truncates_tool_results() {
        let mut compact = TieredCompact::new();
        compact.keep_recent = 0;

        let long = "x".repeat(500);
        let messages = vec![
            Message {
                role: Role::System,
                content: "system".into(),
                meta: meta(MessageType::SystemPrompt, Some(0)),
                ..Default::default()
            },
            Message {
                role: Role::User,
                content: "user".into(),
                meta: meta(MessageType::UserInput, Some(0)),
                ..Default::default()
            },
            Message {
                role: Role::Tool,
                content: long.clone(),
                meta: meta(MessageType::ToolResult, Some(0)),
                ..Default::default()
            },
            Message {
                role: Role::Tool,
                content: long,
                meta: meta(MessageType::ToolResult, Some(1)),
                ..Default::default()
            },
        ];

        let result = compact.compact(&messages, 150, &counter()).unwrap();
        assert_eq!(result.len(), 4);
        for message in &result {
            if message.meta.message_type == MessageType::ToolResult {
                assert!(
                    message.content.len() <= 200,
                    "expected tool_result truncated to 200 chars, got {}",
                    message.content.len()
                );
            }
        }
    }

    #[test]
    fn phase2_drops_tool_results() {
        let mut compact = TieredCompact::new();
        compact.keep_recent = 0;

        let messages = vec![
            Message {
                role: Role::System,
                content: "system".into(),
                meta: meta(MessageType::SystemPrompt, None),
                ..Default::default()
            },
            Message {
                role: Role::User,
                content: "user".into(),
                meta: meta(MessageType::UserInput, None),
                ..Default::default()
            },
            Message {
                role: Role::Tool,
                content: "tool result".into(),
                meta: meta(MessageType::ToolResult, Some(0)),
                ..Default::default()
            },
            Message {
                role: Role::Assistant,
                content: "text".into(),
                meta: meta(MessageType::TextResponse, Some(0)),
                ..Default::default()
            },
        ];

        let result = compact.compact(&messages, 5, &counter()).unwrap();
        assert!(
            !result
                .iter()
                .any(|message| message.meta.message_type == MessageType::ToolResult)
        );
    }

    #[test]
    fn phase3_drops_reasoning_and_text() {
        let mut compact = TieredCompact::new();
        compact.keep_recent = 0;

        let messages = vec![
            Message {
                role: Role::System,
                content: "system".into(),
                meta: meta(MessageType::SystemPrompt, None),
                ..Default::default()
            },
            Message {
                role: Role::User,
                content: "user".into(),
                meta: meta(MessageType::UserInput, None),
                ..Default::default()
            },
            Message {
                role: Role::Tool,
                content: "tool result".into(),
                meta: meta(MessageType::ToolResult, Some(0)),
                ..Default::default()
            },
            Message {
                role: Role::Assistant,
                content: "reasoning".into(),
                meta: meta(MessageType::Reasoning, Some(0)),
                ..Default::default()
            },
            Message {
                role: Role::Assistant,
                content: "text response".into(),
                meta: meta(MessageType::TextResponse, Some(0)),
                ..Default::default()
            },
            Message {
                role: Role::Assistant,
                content: "".into(),
                tool_calls: vec![ToolCall {
                    name: "test".into(),
                    ..Default::default()
                }],
                meta: meta(MessageType::ToolCall, Some(0)),
                ..Default::default()
            },
        ];

        let result = compact.compact(&messages, 2, &counter()).unwrap();
        for message in &result {
            assert!(
                message.meta.message_type != MessageType::Reasoning
                    && message.meta.message_type != MessageType::TextResponse,
                "expected {} to be dropped in phase 3",
                message.meta.message_type.as_str()
            );
        }
    }

    #[test]
    fn protects_system_and_user() {
        let mut compact = TieredCompact::new();
        compact.keep_recent = 0;

        let messages = vec![
            Message {
                role: Role::System,
                content: "system prompt".into(),
                meta: meta(MessageType::SystemPrompt, None),
                ..Default::default()
            },
            Message {
                role: Role::User,
                content: "user input".into(),
                meta: meta(MessageType::UserInput, None),
                ..Default::default()
            },
            Message {
                role: Role::Assistant,
                content: "".into(),
                meta: meta(MessageType::StepNudge, Some(0)),
                ..Default::default()
            },
        ];

        let result = compact.compact(&messages, 1, &counter()).unwrap();
        assert_eq!(result.len(), 2, "system+user protected");
        assert_eq!(result[0].meta.message_type, MessageType::SystemPrompt);
        assert_eq!(result[1].meta.message_type, MessageType::UserInput);
    }

    #[test]
    fn keep_recent_protects_last_steps() {
        let mut compact = TieredCompact::new();
        compact.keep_recent = 2;

        let mut messages = vec![
            Message {
                role: Role::System,
                content: "system".into(),
                meta: meta(MessageType::SystemPrompt, None),
                ..Default::default()
            },
            Message {
                role: Role::User,
                content: "user".into(),
                meta: meta(MessageType::UserInput, None),
                ..Default::default()
            },
        ];
        for step in 0..5i64 {
            messages.push(Message {
                role: Role::Tool,
                content: format!("tool{step}"),
                meta: meta(MessageType::ToolResult, Some(step)),
                ..Default::default()
            });
        }

        let result = compact.compact(&messages, 1, &counter()).unwrap();
        let has_step3 = result
            .iter()
            .any(|message| message.meta.step_index == Some(3));
        let has_step4 = result
            .iter()
            .any(|message| message.meta.step_index == Some(4));
        assert!(has_step3 && has_step4, "steps 3 and 4 preserved");
    }

    #[test]
    fn empty_messages_is_error() {
        let mut compact = TieredCompact::new();
        let error = compact.compact(&[], 100, &counter()).unwrap_err();
        assert_eq!(error, CompactError::EmptyMessages);
        assert_eq!(error.to_string(), "no messages to compact");
    }

    #[test]
    fn find_eligible_end_no_step_index() {
        let messages: Vec<Message> = ["system", "user", "msg1", "msg2", "msg3"]
            .iter()
            .enumerate()
            .map(|(index, content)| Message {
                role: if index == 0 {
                    Role::System
                } else if index == 1 {
                    Role::User
                } else {
                    Role::Assistant
                },
                content: (*content).into(),
                ..Default::default()
            })
            .collect();

        assert_eq!(find_eligible_end(&messages, 2), 2);
    }

    #[test]
    fn find_eligible_end_with_step_index() {
        let steps = [0i64, 0, 1, 2, 3, 4];
        let messages: Vec<Message> = steps
            .iter()
            .enumerate()
            .map(|(index, step)| Message {
                role: if index == 0 {
                    Role::System
                } else if index == 1 {
                    Role::User
                } else {
                    Role::Assistant
                },
                content: format!("msg{index}"),
                meta: MessageMeta {
                    step_index: Some(*step),
                    ..Default::default()
                },
                ..Default::default()
            })
            .collect();

        assert_eq!(find_eligible_end(&messages, 2), 3);
    }

    #[test]
    fn is_nudge_type_matches_go() {
        let cases = [
            (MessageType::StepNudge, true),
            (MessageType::RetryNudge, true),
            (MessageType::PrerequisiteNudge, true),
            (MessageType::ToolResult, false),
            (MessageType::Reasoning, false),
            (MessageType::TextResponse, false),
            (MessageType::SystemPrompt, false),
            (MessageType::UserInput, false),
        ];
        for (message_type, want) in cases {
            assert_eq!(is_nudge_type(&message_type), want, "{message_type:?}");
        }
    }

    #[test]
    fn default_counter_matches_go() {
        let messages = vec![
            Message {
                role: Role::User,
                content: "Hello world test content".into(),
                ..Default::default()
            },
            Message {
                role: Role::Assistant,
                content: "Response with some text".into(),
                ..Default::default()
            },
        ];

        let expected = ("Hello world test content".len() / 4
            + "user".len()
            + 4
            + "Response with some text".len() / 4
            + "assistant".len()
            + 4) as i64;
        assert_eq!(default_counter(&messages), expected);
    }
}
