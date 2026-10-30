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
| rust | `toolformat/*-cases.json#*` | Rust has no toolformat module yet | T3a, `feat/parity-8-rust-toolformat` (aw-rs-tf), PR not open yet |
| rust | `toolspec/cases.v1.json#*` | No Rust runner for the ToolSpec contract cases | unassigned (T4 follow-up) |
