# Eval-suite v1 conformance fixtures

`agentware.eval-suite.v1` conformance fixtures: each bundle pins one slice of
the loader (`load_suite`) and grader (`score_table_case` via
`EvalRunner.run_table_case`) behaviour — a pass or a scored failure with the
failing check's message fragment, or a suite the loader must reject. There is
no model and no network anywhere in this family: the model turns are recorded.

## Consumers

- Python: `python/tests/evals_conformance_test.py` (replays every fixture
  through the real loader and grader with a scripted client; must pass).
- A port of the grader in another language (the TypeScript Playground port is
  the first planned one) replays the same bundles.

## The family is not in the conformance gate (yet)

`scripts/conformance/gate.py` compares coverage across languages, and this
family has one language today. It is deliberately **not** registered in
`FAMILIES`: registering it before a second runner exists would fail the gate
for every other language. When the second language's runner lands, in the
same change:

1. add its fixture test (and record what it ran, like the other families),
2. add `"evals"` to the `-k` filter of the Python leg in
   `.github/workflows/conformance.yml` (the filter currently selects
   `toolformat or reasoning or toolspec or manifest`),
3. add the family to `FAMILIES` in `scripts/conformance/gate.py`.

## Bundle format

Valid fixtures are named `*.fixture.json`; invalid ones `*.invalid.json`.
Both are self-contained: a consumer needs no other file.

### Valid bundle (`*.fixture.json`)

```json
{
  "suite": { ...full "agentware.eval-suite.v1" suite... },
  "turns": {
    "<case id>": [ {"reply": "..."} | {"tool_calls": [{"name": "...", "arguments": {}}]} ]
  },
  "expected": [
    {"id": "<case id>", "passed": true}
  ],
  "runner": {"max_turns": 3}
}
```

- `suite` — the full suite JSON, exactly as a user would export it. The
  consumer writes it to a file and loads it with its real loader.
- `turns` — the recorded model turns, keyed by case id. A turn is either
  `{"reply": "<final text>"}` or `{"tool_calls": [{"name", "arguments"}]}`;
  `arguments` may be a JSON object (serialize it for an OpenAI-style client)
  or a pre-serialized string. Turns are served in order, one list entry per
  model request. Unlike the `fixtures/evals/expectations` scripts, **the
  whole script does not have to be consumed**: a correct grader stops at the
  case's first decisive event, so trailing recorded turns are probes. A
  scripted client that is asked for more turns than recorded answers with a
  plain `{"reply": "done"}` — a grader that over-runs changes the outcome and
  fails the fixture.
- `expected` — one entry per suite case, covering the cases exactly. Fields:
  - `id` — the case id.
  - `passed` — the expected outcome of `run_table_case` (`errored` must stay
    false: a transport error is never a fixture outcome).
  - `error_contains` — required when `passed` is false: a stable fragment of
    the failure reason, i.e. the failing check's message. Use exactly what
    the reference implementation emits (watch for quoting: e.g. the grader
    says `invalid first call to search_wiki` with no quotes around the name).
  - `turns` — optional; the expected number of model requests. Pins the stop
    rules (e.g. a first-tool case must stop at `1` even though a further turn
    was recorded).
- `runner` — optional runner config; today only `max_turns`. When absent the
  consumer uses its default (`10` for the Python runner), so bundles that
  depend on the limit must set it here.

### Invalid bundle (`*.invalid.json`)

```json
{
  "suite": { ...suite the loader must reject... },
  "error_contains": "<fragment of the loader error>"
}
```

Loading `suite` must raise the loader's validation error with
`error_contains` in its message. Fragments are stable, human-readable parts
of the error (pydantic field paths like `cases.0.expect.bogus`, or the
validation message itself like `duplicate case ids`), never the file path or
a whole dump.

## Case inventory

| Fixture | Pins |
|---|---|
| `first-tool` | first call must be `expect.tool` (`args`-checked); wrong first tool fails; no call fails; stop at the first tool message |
| `tool-null-and-absent` | `"tool": null` (must not call) vs `tool` absent (no assertion) |
| `args` | exact `args`; `required_arg_keys`; `forbidden_arg_keys`; schema-`required` missing; undeclared argument; arguments not JSON / not an object |
| `content` | `contains_all`, `contains_any`, `regex` (case-insensitive), `not_contains`; content checks run until a final reply |
| `forbidden-tools` | forbidden tool called in the first turn, a later turn, or when `"tool": null` (forbidden check wins) |
| `deny` | deny feeds the DENY back and gets exactly one more turn; continues before the first deny; a claim of success fails `not_contains`; a denied `expect.tool` call fails a non-deny case |
| `tool-loop-stop` | loop to `max_turns` with no final reply fails; stop-after-first-tool-message pinned by `turns` |

The invalid fixtures cover: unknown keys at suite / case / `expect` /
`content` level, the case-id pattern (uppercase, too long), wrong `schema`,
a suite name that is not dotted, duplicate case ids, duplicate tool names,
`expect.tool` not a suite tool, empty `cases`, `repeats` below 1,
`expect.deny` without `context.allowed_tools`, `expect.deny` with
`expect.tool` in `allowed_tools`, `expect.tool` also in `forbidden_tools`,
arg assertions without `expect.tool`, an invalid regex, and both or neither
of `system_prompt` / `system_prompt_file`.

## Out of scope

Suites using `system_prompt_file` are not representable in this bundle
format (the consumer writes the embedded suite to a standalone file). That
loader path is covered by the loader's own unit tests
(`python/tests/evals_suite_test.py`).
