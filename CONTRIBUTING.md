# Contributing to pedro-agentware

pedro-agentware is implemented three times — **Go (`go/`, the reference
implementation), Python (`python/`, package `pedro_agentware`), and
TypeScript (`typescript/`)** — with deliberately mirrored package structure and
mirrored test suites. Changes to shared logic (middleware, guardrails,
toolformat, llmcontext, tools) must be mirrored in all three languages and
their tests.

This guide covers the full path for adding and testing a model **formatter**
(`toolformat`), the most common contribution surface.

## Adding a model formatter

A formatter renders tool definitions for a model family, parses the model's
text response into `ParsedToolCall`s, formats tool results back into the
model's expected shape, and validates raw responses. The five supported
families — **OpenAI, Anthropic, Qwen, DeepSeek, GLM** — each have a formatter
in all three languages. Per-family format details, reasoning fields, and
thinking tags are documented in `docs/model-format-reference.md`; that doc is
the source of truth for model behavior.

### 1. Implement the `ToolFormatter` interface

Five methods, identical in shape across languages:

**Go** (`go/toolformat/formatter.go`):

```go
type ToolFormatter interface {
    FormatToolDefinitions(tools []tools.Tool) string
    ParseToolCalls(response string) ([]ParsedToolCall, error)
    FormatToolResult(name string, result *tools.Result) string
    ModelFamily() string
    ValidateFormat(response string) error
}
```

**Python** (`python/src/pedro_agentware/toolformat/formatter.py`):

```python
class ToolFormatter(Protocol):
    def format_tool_definitions(self, tools: list[Tool]) -> str: ...
    def parse_tool_calls(self, response: str) -> list[ParsedToolCall]: ...
    def format_tool_result(self, name: str, result: Result) -> str: ...
    def model_family(self) -> str: ...
    def validate_format(self, response: str) -> str | None: ...
```

**TypeScript** (`typescript/src/toolformat/formatter.ts`):

```ts
export interface ToolFormatter {
  formatToolDefinitions(tools: AnyTool[]): string;
  parseToolCalls(response: string): ParsedToolCall[];
  formatToolResult(name: string, result: Result): string;
  modelFamily(): string;
  validateFormat(response: string): string | null;
}
```

`ParsedToolCall` carries `id`, `name`, `args`, and `raw` in all three
languages. If the family's wire format is the OpenAI-compatible JSON array,
embed/extend `GenericFormatter` (Go `generic.go`, Python and TypeScript
`GenericFormatter`) the way `OpenAIFOrmatter`, `DeepSeekFormatter`, and
`GLMFormatter` do, rather than re-implementing JSON parsing.

### 2. Register the formatter in the selector

Selectors match on **case-insensitive substring** of the model name and fall
through to `GenericFormatter` for anything unrecognized:

- Go: `GetFormatter` in `go/toolformat/selector.go` (a `switch` over
  `strings.Contains` on the lowercased name).
- Python: `get_formatter` in `python/src/pedro_agentware/toolformat/formatter.py`.
- TypeScript: `getFormatter` in `typescript/src/toolformat/formatter.ts`.

Existing mappings (verified in `docs/model-format-reference.md` and the
selector tests): `qwen`/`qwq` → Qwen, `claude`/`anthropic` → Anthropic,
`deepseek` → DeepSeek, `glm`/`chatglm` → GLM, `gpt`/`o1`/`o3` → OpenAI.
Add the new family's name tokens to the selector **in all three languages**
and extend the selector test in each suite.

Note: Go additionally ships `LlamaFormatter`, `MistralFormatter`,
`MiniMaxFormatter`, and `NemotronFormatter` (Go-only, selected only by the Go
selector). Python and TypeScript selectors do not map those names; they fall
through to `GenericFormatter`.

### 3. Reasoning and thinking tags

Reasoning handling lives in the reasoning adapter, mirrored in
`go/reasoning/adapter.go`, `python/src/pedro_agentware/reasoning/adapter.py`,
and `typescript/src/reasoning/adapter.ts` (contract:
`docs/reasoning-adapter.md`). Verified behavior for the five supported
families (`docs/model-format-reference.md`):

| Family | Reasoning field | Inline thinking tags |
|--------|-----------------|----------------------|
| OpenAI | `reasoning_content` | none |
| Anthropic | `thinking` (content block) | none |
| Qwen | `reasoning_content` | `<thinking>...</thinking>` |
| DeepSeek | `reasoning_content` | `[THINK]...[/THINK]` |
| GLM | `reasoning_content` | none |

The adapter's known field set covers `reasoning_content`, `thinking`,
`thinking_content`, and `reasoning`. If a new family uses a different field
name, register it via `RegisterModelField` / `register_model_field` rather
than editing the known set. Do not document or assume reasoning behavior for
a family that is not in `docs/model-format-reference.md`.

### 4. Add tests in all three languages

Mirror the existing suites; each covers all five methods plus the selector:

| Language | Test file | Focused run (from repo root) |
|----------|-----------|------------------------------|
| Go | `go/toolformat/formatter_test.go` | `cd go && go test ./toolformat/...` |
| Python | `python/tests/toolformat_test.py` | `cd python && pytest tests/toolformat_test.py` |
| TypeScript | `typescript/tests/toolformat.test.ts` | `cd typescript && npx jest tests/toolformat.test.ts` |

Follow the conventions of the file you extend (Go table-driven subtests,
Python `Test<Class>` classes with mock tools, Jest `describe`/`it` blocks).
Cover at minimum, per formatter: empty input, a valid single call, multiple
calls, no-calls text, unparseable arguments, result success and error paths,
`ModelFamily`/`model_family`/`modelFamily`, and `ValidateFormat` valid/invalid
cases. Add the new model-name tokens to the selector test in each suite.

### 5. Shared fixtures

Seeded shared fixtures live at the repo root, one file per family plus a
shared tool set:

- `fixtures/toolformat/tools.json` — the two shared tool definitions
  (`get_weather`, `search_web`) used across families.
- `fixtures/toolformat/{family}-cases.json` — per-family cases with the keys
  `family`, `description`, `format_definitions`, `parse_calls`,
  `format_result`, `validate_format`.
- `fixtures/reasoning/{family}-cases.json` + `fixtures/reasoning/tools.json` —
  the same pattern for the reasoning adapter.

The current per-language toolformat suites use inline cases rather than
loading these files, so a new family needs **both**: a
`fixtures/toolformat/{family}-cases.json` seeded from an existing family's
shape (and the matching `fixtures/reasoning/` file if the family has
reasoning behavior) and the inline test coverage in all three suites.

### 6. Update the reference doc

Add the family to `docs/model-format-reference.md`: the Quick Reference row,
a per-family detail section (model names, definitions/parse/result formats,
reasoning field, inline tags), and the Implementation Matrix rows. Only state
model behavior that is verified in code or the model's documented format.

## Validation commands

Focused checks per language (also available as Makefile targets):

```bash
# Go
cd go && go build ./... && go test ./toolformat/... && go vet ./...
golangci-lint run            # from go/; Makefile: make go-lint

# Python (src-layout; install dev extras first)
cd python && pip install -e ".[dev]"
pytest tests/toolformat_test.py
ruff check . && mypy src/    # Makefile: make python-lint python-typecheck

# TypeScript (Node >= 18, ESM)
cd typescript && npm run build && npx jest tests/toolformat.test.ts
npm run lint                 # eslint

# Docs (markdown lint; rules in .markdownlint.json)
npx markdownlint-cli2 "docs/**/*.md" README.md "!docs/build-history/**"
```

Run the full suite for the languages you touched before opening a PR:
`make go-test`, `make python-test`, and `cd typescript && npm test`.

## Parity checklist

Before opening a PR, confirm for every language you touched:

- [ ] Formatter implements all five interface methods.
- [ ] Selector maps the new model-name tokens (case-insensitive).
- [ ] Test suite extended: all five methods + selector, success and failure
      paths.
- [ ] `fixtures/toolformat/{family}-cases.json` seeded (and
      `fixtures/reasoning/` where applicable).
- [ ] `docs/model-format-reference.md` updated.
- [ ] Build, focused tests, lint, and typecheck pass in that language.
