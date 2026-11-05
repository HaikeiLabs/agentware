//! Workflow progress from application-verified observations.
//!
//! This is response-quality state for one workflow. It does not authorize tool
//! calls. Callers must separately enforce policy and bound total execution.

use std::collections::HashSet;

/// A snapshot of pending requirements and the no-progress budget.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProgressSnapshot {
    /// Requirements not currently observed, in declaration order.
    pub pending: Vec<String>,
    /// Whether all required observations are present.
    pub ready: bool,
    /// Consecutive failures since the last new observation.
    pub no_progress: usize,
    /// Whether the configured no-progress budget has been exceeded.
    pub exhausted: bool,
}

/// Per-run tracker. Applications validate facts before recording them.
#[derive(Debug, Clone)]
pub struct ProgressTracker {
    required: Vec<String>,
    satisfied: HashSet<String>,
    max_no_progress: usize,
    failures: usize,
}

impl ProgressTracker {
    /// Create a tracker with distinct, nonempty requirement keys.
    pub fn new(required: Vec<String>, max_no_progress: usize) -> Result<Self, &'static str> {
        let unique: HashSet<_> = required.iter().collect();
        if required.is_empty()
            || required.iter().any(|s| s.trim().is_empty())
            || unique.len() != required.len()
        {
            return Err("requirements must be distinct and nonempty");
        }
        Ok(Self {
            required,
            satisfied: HashSet::new(),
            max_no_progress,
            failures: 0,
        })
    }

    /// Record or invalidate a verified fact. Only a new fact resets failures.
    pub fn observe(&mut self, key: &str, present: bool) -> Result<ProgressSnapshot, &'static str> {
        if !self.required.iter().any(|value| value == key) {
            return Err("unknown requirement");
        }
        if present {
            if self.satisfied.insert(key.to_owned()) {
                self.failures = 0;
            }
        } else {
            self.satisfied.remove(key);
        }
        Ok(self.snapshot())
    }

    /// Count a no-progress failure; enforce a separate total budget as well.
    pub fn failure(&mut self) -> ProgressSnapshot {
        self.failures += 1;
        self.snapshot()
    }

    /// Return a copy of the current state.
    pub fn snapshot(&self) -> ProgressSnapshot {
        let pending: Vec<String> = self
            .required
            .iter()
            .filter(|key| !self.satisfied.contains(*key))
            .cloned()
            .collect();
        ProgressSnapshot {
            ready: pending.is_empty(),
            pending,
            no_progress: self.failures,
            exhausted: self.failures > self.max_no_progress,
        }
    }
}
