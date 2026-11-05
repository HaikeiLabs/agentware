# Workflow work: proposed review split

The implementation is prepared as three review units, not a single PR:

1. **Shared progress contract:** `fixtures/workflow`, `docs/workflow-progress.md`,
   and the progress trackers/tests in Python, Go, and TypeScript. Independent
   from the optional Forge dependency; existing behavior is unchanged by default.
2. **Python Forge and framework adapters:** `python/pyproject.toml`, the
   `integrations` package, the Pydantic AI example and integration tests,
   `python/README.md`, and `docs/forge-workflow-integration.md`. Depends on the
   Python progress tracker from unit 1. Forge 0.10.1 is optional and requires
   Python 3.12+.
3. **Rust quality support:** `rust/agentware-kei/src/{progress,guardrails,context}.rs`,
   the Rust exports and tests, and `docs/rust-workflow-guardrails.md`. Replays
   the fixture from unit 1. Its tool formats and context strategy have a smaller
   scope than the full upstream Forge runtime.

The existing Hermes and Kitaru adapters wrap opaque CLI or flow executions;
they do not expose their inner model/tool loop. The framework-neutral Python
session is available at a tool-call boundary, but those adapters must not claim
inner-call coverage until a suitable hook is implemented and tested.
