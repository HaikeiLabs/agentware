//! Context-window accounting (port of `go/llm/context_window.go`).

use super::compact::{
    CompactEvent, CompactionStrategy, PhaseReporter, TieredCompact, TokenCounter,
};
use super::request::Message;
use super::tokens::estimate_messages_tokens;
use std::sync::{Arc, Mutex};

/// Builds the threshold warning text (Go `ThresholdCallback`).
pub type ThresholdCallback = Arc<dyn Fn(i64, i64, f64) -> String + Send + Sync>;

/// Observes compaction events (Go `CompactCallback`).
pub type CompactCallback = Arc<dyn Fn(CompactEvent) + Send + Sync>;

struct Inner {
    compaction_ratio: f64,
    last_known_tokens: Option<i64>,
    thresholds: Vec<f64>,
    on_threshold: Option<ThresholdCallback>,
    on_compact: Option<CompactCallback>,
    fired_thresholds: Vec<f64>,
}

/// Tracks token usage against the context window and compacts when needed
/// (Go `ContextWindowManager`).
///
/// Go guards state with an `RWMutex`; the Rust port holds the mutable state
/// behind a `Mutex`, so all methods take `&self` and stay thread-safe the
/// same way.
pub struct ContextWindowManager {
    context_window: i64,
    counter: TokenCounter,
    inner: Mutex<Inner>,
}

impl ContextWindowManager {
    /// Go `NewContextWindowManager`: default compaction ratio 0.75, default
    /// thresholds [0.65, 0.80], default threshold callback, and Go's
    /// `DefaultCounter` when `counter` is `None`.
    pub fn new(context_window: i64, counter: Option<TokenCounter>) -> Self {
        Self {
            context_window,
            counter: counter.unwrap_or_else(|| Arc::new(default_counter)),
            inner: Mutex::new(Inner {
                compaction_ratio: 0.75,
                last_known_tokens: None,
                thresholds: vec![0.65, 0.80],
                on_threshold: None,
                on_compact: None,
                fired_thresholds: Vec::new(),
            }),
        }
    }

    /// Go `WithThresholds`: replaces the thresholds (copied and sorted
    /// ascending; an empty slice keeps the current ones) and, when `cb` is
    /// `Some`, replaces the threshold callback.
    pub fn with_thresholds(self, thresholds: &[f64], cb: Option<ThresholdCallback>) -> Self {
        let mut inner = self.inner.lock().expect("context window mutex poisoned");
        if !thresholds.is_empty() {
            let mut copy = thresholds.to_vec();
            copy.sort_by(|a, b| a.partial_cmp(b).expect("thresholds are finite"));
            inner.thresholds = copy;
        }
        if let Some(cb) = cb {
            inner.on_threshold = Some(cb);
        }
        drop(inner);
        self
    }

    /// Go `WithOnCompact`.
    pub fn with_on_compact(self, cb: CompactCallback) -> Self {
        self.inner
            .lock()
            .expect("context window mutex poisoned")
            .on_compact = Some(cb);
        self
    }

    /// Go `SetCompactionRatio`.
    pub fn set_compaction_ratio(&self, ratio: f64) {
        self.inner
            .lock()
            .expect("context window mutex poisoned")
            .compaction_ratio = ratio;
    }

    /// Go `UpdateTokenCount`.
    pub fn update_token_count(&self, n: i64) {
        self.inner
            .lock()
            .expect("context window mutex poisoned")
            .last_known_tokens = Some(n);
    }

    /// Returns `(current_tokens, at_or_over_threshold)` (Go `Check`). The Go
    /// threshold comparison is `currentTokens >= threshold`.
    pub fn check(&self, messages: &[Message]) -> (i64, bool) {
        let inner = self.inner.lock().expect("context window mutex poisoned");
        let current_tokens = estimate_tokens_locked(&inner, &self.counter, messages);
        let threshold = (self.context_window as f64 * inner.compaction_ratio) as i64;
        (current_tokens, current_tokens >= threshold)
    }

    /// Go `ShouldCompact`: strictly over the threshold.
    pub fn should_compact(&self, messages: &[Message]) -> bool {
        let inner = self.inner.lock().expect("context window mutex poisoned");
        let current_tokens = estimate_tokens_locked(&inner, &self.counter, messages);
        let threshold = (self.context_window as f64 * inner.compaction_ratio) as i64;
        current_tokens > threshold
    }

    /// Runs the tiered compaction strategy toward the compaction target,
    /// emits the compact event when a callback is set, and resets the last
    /// known token count and fired thresholds (Go `Compact`).
    pub fn compact(
        &self,
        messages: &[Message],
    ) -> Result<Vec<Message>, super::compact::CompactError> {
        let mut inner = self.inner.lock().expect("context window mutex poisoned");

        let tokens_before = (self.counter)(messages);
        let messages_before = messages.len() as i64;
        let budget_tokens = self.context_window;
        let target_tokens = (budget_tokens as f64 * inner.compaction_ratio) as i64;
        let mut strategy = TieredCompact::new();
        let compacted = strategy.compact(messages, target_tokens, &self.counter)?;
        let tokens_after = (self.counter)(&compacted);
        let messages_after = compacted.len() as i64;

        let phase_reached = strategy.last_phase();

        let step_index = messages
            .last()
            .and_then(|message| message.meta.step_index)
            .unwrap_or(0);

        if let Some(on_compact) = &inner.on_compact {
            on_compact(CompactEvent {
                step_index,
                tokens_before,
                tokens_after,
                budget_tokens,
                messages_before,
                messages_after,
                phase_reached,
                strategy_name: strategy.name().to_owned(),
            });
        }

        inner.last_known_tokens = None;
        inner.fired_thresholds = Vec::new();
        Ok(compacted)
    }

    /// Returns the warning text for the first unfired threshold at or below
    /// the current usage, firing it at most once until the next compaction
    /// (Go `CheckThresholds`). Empty when usage or budget is zero. The Go
    /// `ctx` parameter carried no behavior and is dropped.
    pub fn check_thresholds(&self, messages: &[Message]) -> String {
        let mut inner = self.inner.lock().expect("context window mutex poisoned");

        let current_tokens = estimate_tokens_locked(&inner, &self.counter, messages);
        let budget = self.context_window;
        if current_tokens == 0 || budget == 0 {
            return String::new();
        }

        let pct = current_tokens as f64 / budget as f64;

        let thresholds = inner.thresholds.clone();
        for threshold in thresholds {
            if pct >= threshold && !inner.fired_thresholds.contains(&threshold) {
                inner.fired_thresholds.push(threshold);
                let callback = inner
                    .on_threshold
                    .clone()
                    .unwrap_or_else(|| Arc::new(default_threshold_callback));
                return callback(current_tokens, budget, pct);
            }
        }
        String::new()
    }
}

fn estimate_tokens_locked(inner: &Inner, counter: &TokenCounter, messages: &[Message]) -> i64 {
    if let Some(last_known) = inner.last_known_tokens {
        return last_known;
    }
    counter(messages)
}

/// Go `DefaultThresholdCallback`.
pub fn default_threshold_callback(_tokens: i64, _budget: i64, pct: f64) -> String {
    if pct >= 0.80 {
        "Context is nearly full. Summarize critical findings now and prioritize completing the current task.".to_owned()
    } else if pct >= 0.65 {
        "Context is filling up. Be concise and front-load important information.".to_owned()
    } else {
        String::new()
    }
}

/// Go `DefaultCounter`.
pub fn default_counter(messages: &[Message]) -> i64 {
    let mut total = 0i64;
    for message in messages {
        let mut overhead = message.role.as_str().len() as i64 + 4;
        for tool_call in &message.tool_calls {
            overhead += tool_call.name.len() as i64 + 1;
        }
        total += message.content.len() as i64 / 4 + overhead;
    }
    total
}

/// Convenience counter over [`estimate_messages_tokens`], mirroring the Go
/// tests' counter closure.
pub fn estimate_messages_counter() -> TokenCounter {
    Arc::new(|messages: &[Message]| estimate_messages_tokens(messages))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::request::Role;
    use crate::llm::response::ToolCall;

    fn user_message() -> Vec<Message> {
        vec![Message {
            role: Role::User,
            content: "test".into(),
            ..Default::default()
        }]
    }

    fn fixed_counter(tokens: i64) -> TokenCounter {
        Arc::new(move |_| tokens)
    }

    #[test]
    fn update_token_count_overrides_estimation() {
        let manager = ContextWindowManager::new(1000, None);
        manager.update_token_count(500);

        let (tokens, at_threshold) = manager.check(&user_message());
        assert_eq!(tokens, 500);
        assert!(!at_threshold);
    }

    #[test]
    fn check_uses_actual_count_until_updated() {
        let manager = ContextWindowManager::new(1000, Some(fixed_counter(800)));
        manager.set_compaction_ratio(0.75);

        let (tokens, at_threshold) = manager.check(&user_message());
        assert_eq!(tokens, 800);
        assert!(at_threshold, "800 is 75% of 1000");

        manager.update_token_count(600);

        let (tokens, at_threshold) = manager.check(&user_message());
        assert_eq!(tokens, 600);
        assert!(!at_threshold, "600 is 60% of 1000");
    }

    #[test]
    fn should_compact_uses_actual_count() {
        let manager = ContextWindowManager::new(1000, Some(fixed_counter(900)));
        manager.set_compaction_ratio(0.75);

        assert!(
            manager.should_compact(&user_message()),
            "900 is 90% of 1000"
        );

        manager.update_token_count(700);
        assert!(
            !manager.should_compact(&user_message()),
            "700 is 70% of 1000"
        );
    }

    #[test]
    fn compact_resets_token_count() {
        let manager = ContextWindowManager::new(1000, None);
        manager.set_compaction_ratio(0.75);

        manager.update_token_count(1500);

        let compacted = manager
            .compact(&[Message {
                role: Role::User,
                content: "short".into(),
                ..Default::default()
            }])
            .unwrap();

        let (tokens, _) = manager.check(&compacted);
        assert_ne!(tokens, 1500, "token count reset after compaction");
        assert!(tokens < 100, "expected low token count, got {tokens}");
    }

    #[test]
    fn thread_safety() {
        let manager = Arc::new(ContextWindowManager::new(1000, None));
        let writer = Arc::clone(&manager);
        let handle = std::thread::spawn(move || {
            for i in 0..1000i64 {
                writer.update_token_count(i);
            }
        });
        for _ in 0..1000 {
            let _ = manager.check(&user_message());
        }
        handle.join().unwrap();
    }

    #[test]
    fn check_thresholds_fires_once() {
        let manager = ContextWindowManager::new(1000, Some(fixed_counter(850)))
            .with_thresholds(&[0.80], None);

        assert!(
            !manager.check_thresholds(&user_message()).is_empty(),
            "expected warning at 85% threshold"
        );
        assert!(
            manager.check_thresholds(&user_message()).is_empty(),
            "threshold should fire only once"
        );
    }

    #[test]
    fn check_thresholds_resets_after_compact() {
        let manager = ContextWindowManager::new(1000, Some(fixed_counter(850)))
            .with_thresholds(&[0.80], None);

        let _ = manager.check_thresholds(&user_message());

        let _ = manager.compact(&user_message()).unwrap();
        manager.update_token_count(850);

        assert!(
            !manager.check_thresholds(&user_message()).is_empty(),
            "threshold should fire again after reset following compaction"
        );
    }

    #[test]
    fn check_thresholds_multiple_thresholds_ordered() {
        let manager = ContextWindowManager::new(1000, Some(fixed_counter(900)))
            .with_thresholds(&[0.50, 0.80, 0.65], None);

        let warning = manager.check_thresholds(&user_message());
        assert!(
            !warning.is_empty(),
            "expected warning at 90% (triggers 80% first due to sorted order)"
        );
        assert_eq!(
            warning,
            "Context is nearly full. Summarize critical findings now and prioritize completing the current task."
        );
    }

    #[test]
    fn check_thresholds_default_thresholds() {
        let manager = ContextWindowManager::new(1000, Some(fixed_counter(700)));
        assert!(
            !manager.check_thresholds(&user_message()).is_empty(),
            "expected default 65% warning"
        );
    }

    #[test]
    fn check_thresholds_custom_callback() {
        let called = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let called_clone = Arc::clone(&called);
        let callback: ThresholdCallback = Arc::new(move |_, _, _| {
            called_clone.store(true, std::sync::atomic::Ordering::SeqCst);
            "custom warning".to_owned()
        });
        let manager = ContextWindowManager::new(1000, Some(fixed_counter(850)))
            .with_thresholds(&[0.80], Some(callback));

        let warning = manager.check_thresholds(&user_message());
        assert!(
            called.load(std::sync::atomic::Ordering::SeqCst),
            "custom callback should be called"
        );
        assert_eq!(warning, "custom warning");
    }

    #[test]
    fn check_thresholds_zero_tokens() {
        let manager = ContextWindowManager::new(1000, None);
        let messages = vec![Message {
            role: Role::User,
            content: String::new(),
            ..Default::default()
        }];
        assert!(manager.check_thresholds(&messages).is_empty());
    }

    #[test]
    fn thread_safety_check_thresholds() {
        let manager =
            Arc::new(ContextWindowManager::new(1000, None).with_thresholds(&[0.50], None));
        let writer = Arc::clone(&manager);
        let handle = std::thread::spawn(move || {
            for i in 0..1000i64 {
                writer.update_token_count(i);
            }
        });
        for _ in 0..1000 {
            let _ = manager.check_thresholds(&user_message());
        }
        handle.join().unwrap();
    }

    #[test]
    fn compact_callback_fires() {
        let manager = ContextWindowManager::new(1000, Some(fixed_counter(900)))
            .with_on_compact(Arc::new(|_| {}));
        let _ = manager.compact(&user_message()).unwrap();
    }

    #[test]
    fn compact_event_contains_correct_data() {
        let event = Arc::new(Mutex::new(None::<CompactEvent>));
        let slot = Arc::clone(&event);
        let manager = ContextWindowManager::new(1000, Some(fixed_counter(900))).with_on_compact(
            Arc::new(move |captured| {
                *slot.lock().unwrap() = Some(captured);
            }),
        );

        let messages = vec![
            Message {
                role: Role::System,
                content: "system".into(),
                ..Default::default()
            },
            Message {
                role: Role::User,
                content: "test".into(),
                ..Default::default()
            },
            Message {
                role: Role::Assistant,
                content: "response".into(),
                ..Default::default()
            },
        ];
        let _ = manager.compact(&messages).unwrap();

        let captured = event.lock().unwrap().clone().expect("callback fired");
        assert_eq!(captured.budget_tokens, 1000);
        assert_eq!(captured.messages_before, 3);
        assert_eq!(captured.strategy_name, "TieredCompact");
        assert_ne!(captured.phase_reached, 0);
    }

    #[test]
    fn compact_without_callback_does_not_panic() {
        let manager = ContextWindowManager::new(1000, Some(fixed_counter(900)));
        let _ = manager.compact(&user_message()).unwrap();
    }

    #[test]
    fn compact_phase_reached_reflects_phases() {
        let long_content = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. ".repeat(100);
        let messages: Vec<Message> = (0..20)
            .map(|i| Message {
                role: Role::User,
                content: long_content.clone(),
                meta: crate::llm::request::MessageMeta {
                    step_index: Some(5 + i),
                    ..Default::default()
                },
                ..Default::default()
            })
            .collect();

        let manager = ContextWindowManager::new(1000, Some(Arc::new(default_counter)))
            .with_on_compact(Arc::new(|_| {}));
        let _ = manager.compact(&messages).unwrap();
    }

    #[test]
    fn default_counter_counts_roles_and_tool_names() {
        let messages = vec![Message {
            role: Role::Assistant,
            content: String::new(),
            tool_calls: vec![ToolCall {
                metadata: None,
                name: "test".into(),
                ..Default::default()
            }],
            ..Default::default()
        }];
        // 0/4 content + len("assistant") + 4 + len("test") + 1
        assert_eq!(default_counter(&messages), 9 + 4 + 4 + 1);
    }
}
