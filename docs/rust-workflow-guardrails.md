# Rust workflow quality support

The Rust crate exposes a per-workflow `progress::ProgressTracker` implementing
the shared `fixtures/workflow/progress.v1.json` contract. Its `guardrails`
module also provides an initial parsed-call facade that replays the existing
shared v1 guardrail fixtures, plus text rescue for JSON, rehearsal, Qwen XML,
and Mistral formats. The parser treats text as data and does not evaluate code.
The `context::compact_tool_results` helper truncates old tool results without
dropping messages or changing tool-call IDs; it does not guarantee a hard
token limit.

These are response-quality tools, not authorization. The caller owns the
workflow loop, independent policy and audit checks, total budgets, and handling
of downstream failures. The implementation does not claim full Forge parser
edge-case parity, framework adapters, or tiered context-manager parity.
Applications must supply verified observations rather than treating model
claims as progress.
