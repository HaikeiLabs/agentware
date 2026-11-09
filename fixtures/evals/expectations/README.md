# Eval runner expectations

`cases.json` pins the eval runner's `expect`, `expectedArgs`, `repeats` and
`minPassRate` behaviour. Every language replays these same cases through its
`EvalRunner` with a scripted model, with no network:

- Go: `go/evals/expectations_test.go`
- Python: `python/tests/evals_expectations_test.py`
- TypeScript: `typescript/tests/evals-expectations.test.ts`
- Rust: `rust/agentware-kei/tests/evals_expectations.rs`

Each entry has:

- `case` — one case in the shared case-file contract (see
  `docs/evals/README.md`). The top-level `tools` are added to every case as
  its `tools`.
- `script` — the scripted model turns, one list per repeat. A turn is
  `{"content": "..."}` or `{"tool_calls": [{"name", "arguments"}]}`;
  `arguments` is a JSON object (serialize it for an OpenAI-style client).
  The turns are consumed in order, and every replay asserts the whole script
  was used.
- `expected` — `success`, `passes`, `repeats`, the total `turns` across
  repeats, the called tool names in order (`toolCalls`), and either the exact
  `error` (`""` for a pass) or a stable `errorContains` fragment.
