# Fixture conformance exclusions

Every language runs every case in the shared fixtures. The `Conformance`
workflow (`.github/workflows/conformance.yml`) enforces this: each language
records the cases it ran, and `scripts/conformance/gate.py` fails the check
when a language skipped a case that is not listed below. See
[`docs/testing.md`](../docs/testing.md).

## The rule

- A language may skip a fixture case only if a row here names that language
  and a pattern matching the case.
- Every row says why, and names the PR or task that removes it. A row is
  temporary: the PR that adds the runner deletes the row in the same commit.
- A row that no longer excludes anything (the language now runs every case
  it matches) fails the gate, so stale rows cannot linger.
- Do not add a row to get a red check green. Fix the runner, or fix the
  fixture, or get the owner to agree to the exclusion in the PR.

A case is identified as `<fixture>#<section>/<case name>`, where `<fixture>`
is the path under `fixtures/`, `<section>` is the fixture's top-level array
(`parse_calls`, `examples`, `valid`, ...) and `<case name>` is the entry's
`name`. Patterns use shell globbing (`*`, `?`, `[...]`); `*` also matches `/`
and `#`.

## Exclusions

| Language | Cases | Reason | Removed by |
|---|---|---|---|
| go | `toolformat/glm-cases.json#format_definitions/*` | Go replays only `parse_calls` and `validate_format` from the GLM fixture | unassigned (T4 follow-up) |
| go | `toolformat/glm-cases.json#format_result/*` | as above | unassigned (T4 follow-up) |
| go | `toolformat/[!g]*-cases.json#*` | No Go runner for the anthropic, deepseek, openai and qwen toolformat fixtures | unassigned (T4 follow-up) |
| go | `reasoning/*-cases.json#*` | No Go runner for the reasoning fixtures | unassigned (T4 follow-up) |
| python | `toolformat/*-cases.json#*` | No Python runner for the toolformat fixtures (`tests/toolformat_test.py` uses inline cases) | unassigned (T4 follow-up) |
| python | `reasoning/*-cases.json#*` | No Python runner for the reasoning fixtures (`tests/reasoning_test.py` uses inline cases) | unassigned (T4 follow-up) |
| typescript | `reasoning/deepseek-cases.json#examples/text mode with THINK tags` | The shared input combines a leading-space THINK-tag block with trailing content; the TypeScript adapter returns different leading whitespace | aw-ts-fx |
| typescript | `reasoning/qwen-cases.json#examples/reasoning_content with thinking tags` | With both a native reasoning field and inline thinking tags, the TypeScript adapter strips tags from clean content while this fixture expects them retained | aw-ts-fx |
| typescript | `reasoning/qwen-cases.json#examples/text response with thinking tags` | The TypeScript adapter strips inline thinking tags from clean content while this fixture expects them retained | aw-ts-fx |
| rust | `toolformat/*-cases.json#*` | Rust has no toolformat module yet | T3a, `feat/parity-8-rust-toolformat` (aw-rs-tf), PR not open yet |
| rust | `reasoning/*-cases.json#*` | Rust has no reasoning adapter yet | T3b Rust reasoning, not started |
| rust | `toolspec/cases.v1.json#*` | No Rust runner for the ToolSpec contract cases | unassigned (T4 follow-up) |
