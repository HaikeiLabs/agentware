# Agent Middleware - Developer Guide

## Overview

This is **pedro-agentware** — MCP-compatible middleware for LLM tool calling. It sits between an agent (LLM orchestrator) and tool execution, enforcing policy on every tool call (allow / deny / filter, rate limits, caller-context conditions) and emitting an audit record per call, whether the call proceeds or not.

The same library is implemented three times — **Go (`go/`, the reference implementation), Python (`python/`), and TypeScript (`typescript/`)** — with deliberately mirrored package structure and mirrored test suites. When changing shared logic (middleware, guardrails, toolformat, llmcontext, tools), check whether the counterparts in the other languages and their tests need the same change.

The delegation contract: `CallerContext.InvokingSubject` is the human who initiated the request and is carried unchanged across every delegation hop (`CallerContext.Delegate` / `delegate()`); `ParentSpan` and `DelegationDepth` record where in the chain the call sits. `Trusted` defaults to **false** (fail-closed) in every language, and a missing caller context is never promoted to trusted.

## Before Planning: Wiki First

Every worker **must** search the shared Herdr wiki before planning changes:

```bash
wiki search "tenant data distributed proxy"
wiki search "<topic>" --top-k 10
```

Read the canonical tenant-side proxy architecture in `docs/tenant-proxy-reference.md`
(the in-repo copy of the wiki's *"Documentation audit: tenant data must stay
behind distributed proxy"* capture) and preserve its **metadata-only
control-plane boundary**:

- `tool_bindings` and `secret_refs` are **non-secret metadata only**; the
  bootstrap secret is rejected from any manifest.
- The distributed proxy is the **connector/provider runtime and policy
  enforcement point**.
- **ABAC is called for metadata policy decisions only** — it decides; it does
  not execute writes or serve/store credentials.
- Provider payloads/results, customer content, credentials, embeddings, and
  indexes **never enter Kei**.
- Local agent execution stays possible without a direct ABAC connector
  dependency; governed external operations use the proxy boundary.

Never write docs that say ABAC serves credentials or that agentware sends
customer data through the control plane. See [Shared Herdr Wiki](#shared-herdr-wiki).

## Shared Herdr Wiki

Architecture and coordination captures live in the shared Herdr wiki
(plugin: <https://github.com/Soypete/herdr-wiki-plugin>). All operations go
through the `wiki` command, which prints results to stdout.

### Clone / setup

```bash
git clone https://github.com/Soypete/herdr-wiki-plugin
# plugin is already installed and `wiki` is on PATH for most workers
```

If `wiki` is not on PATH, install it first:

```bash
git clone https://github.com/Soypete/herdr-wiki-plugin ~/code/herdr-wiki-plugin
export PATH="$HOME/code/herdr-wiki-plugin/bin:$PATH"
```

### Commands

```bash
wiki search <query> [--top-k N] [--json]        # search prior notes/claims
wiki stats                                      # knowledge-base stats
wiki capture --title T --type T --content C \
  --link predicate:target ...                   # capture a durable finding
wiki organize                                   # HUMAN ONLY — folds inbox into the graph
```

### Rules

- `--type` and link predicates come from the closed vocabulary (`claim`,
  `contradiction`, `decision`, `entity`, `source`; predicates `derived_from`,
  `contradicts`, `supports`, `about`, `relates_to`). Do not guess types.
- Captures land in an inbox; a human runs `wiki organize`. **Workers never
  edit wiki pages directly.**
- Never read or edit wiki inbox JSON files directly; use only `wiki search` /
  `wiki capture`.
- Search the wiki before planning; when you learn something durable, capture it.

## Build, Lint, and Test Commands

### Go (module at `go/`)

```bash
cd go
go build ./...
go test ./...                     # all tests
go test -run TestName ./middleware/   # a single test
go vet ./...
gofmt -w .                         # format
golangci-lint run                  # lint (CI runs golangci-lint)
```

### Python (src-layout package `pedro_agentware` at `python/`)

```bash
cd python
pip install -e ".[dev]"
pytest                            # test files are *_test.py (not test_*.py); asyncio_mode=auto
pytest -k TestName
ruff check .                       # lint
ruff format .                      # format
mypy src/                          # strict typing (CI runs both)
```

### Testing Guidelines

- Write unit tests for all exported functions
- Use table-driven tests for functions with multiple test cases
- Mock external dependencies (Policy, Auditor, ToolExecutor)
- Test both success and failure paths
- Name Go test files `*_test.go`; Python test files `*_test.py`
- Test fail-closed behavior explicitly: missing caller context, unknown decision, unreachable policy endpoint all deny

---

## Architecture

Each language implementation contains the same packages (Go names shown; Python/TS mirror them):

- **`middleware/`** — the core: `Middleware` wraps a `ToolExecutor`; a `PolicyEvaluator` returns a `Decision` (allow / deny / filter-with-redacted-args) from the tool name, args, and `CallerContext` (user, session, role, trusted, delegation fields — carried in `context.Context` in Go); an `Auditor` records every decision. Rules match tools by glob, support condition operators (`eq`, `contains`, `matches`, `exists`, negations), and per-tool rate limits (`ratelimit.go`). Configured via chained options: `NewMiddleware(exec).WithPolicy(...).WithAuditor(...)`. `AuditedToolClient` (`tool_client.go` / `tool_client.py`) is the small dependency-free surface: a function in, a `Result` out, audited either way.
- **`middleware/guardrails/`** — agent-loop guardrails: error tracker, nudge, response validator, step enforcer.
- **`toolformat/`** — formats/parses tool calls for open models that lack native tool-call APIs: generic, llama, minimax, mistral, nemotron, qwen, plus a selector.
- **`llm/` + `llmcontext/`** — token counting, context-window accounting, and conversation compaction.
- **`llm/proxy/` + `cmd/pedro-proxy/`** (Go only) — `pedro-proxy`, an OpenAI-compatible HTTP proxy in front of a backend (llama-server, Ollama) that adds retries, context-window management, and compaction.
- **`tools/`, `executor/`, `jobs/`, `prompts/`** — tool registry/results, dispatch, background jobs, system-prompt generation with tool sections.
- **Adapters** — wrap agent backends behind a unified interface: Go `go/adapters/{adk,hermes}`; Python `python/adapters/{hermes,kitaru,pydantic}` (each with its own `pyproject.toml`). See `python/adapters/README.md`.
- **`memory/`** (Go) — "wiki memory": an LLM-maintained, ontology-constrained markdown wiki scoped per user. `memory.Vault` resolves per-user directories (`<root>/<user>/wiki/` + `raw/`) with path-boundary containment; `memory/page` parses frontmatter + typed wikilinks and emits the A-box as N-Triples; `memory/ontology` loads the read-only T-box and validates pages, returning structured `Violation` diagnostics. RDF handling uses `github.com/soypete/ontology-go`. The T-box lives in the `ontologies/` git submodule (`git submodule update --init` after cloning) and is read-only: missing terms go in `SCHEMA_GAPS.md`, never invented. The page contract is in `SCHEMA.md`.
- **`kei/`** (Python) — the KEI integration surface for third-party harnesses: `HarnessContract`, auth providers, proxy config, and `KeiProxyEvaluator`, a `PolicyEvaluator` that fails closed on every path that is not an explicit affirmative (`permit`/`allow`). See `docs/harness-contract.md`.
- **Tenant-side distributed proxy** — agentware provides local tool middleware, delegation, semantic tool bindings, and proxy integration. The distributed proxy is the **connector/provider runtime and policy enforcement point**; Kei is a **metadata-only control plane** (registration, auth *references*, tool bindings, scopes, audit metadata). ABAC is called for **metadata policy decisions only** — it never executes writes or serves/store credentials. Provider payloads/results, customer content, credentials, embeddings, and indexes never enter Kei. See `docs/tenant-proxy-reference.md`.

## Code Style Guidelines

### General Principles

- Write idiomatic Go — follow standard library conventions
- Keep functions small and focused (single responsibility)
- Prefer composition over inheritance
- Use interfaces for abstraction; define them early

### Naming Conventions

- **Packages**: lowercase, short, e.g., `middleware`, `policy`, `audit`
- **Interfaces**: `ToolExecutor`, `Policy`, `Auditor` — noun-based, noter-like
- **Functions**: `ValidateToolCall`, `EnforcePolicy` — VerbFirst, camelCase
- **Variables**: `toolName`, `policyConfig` — camelCase, descriptive
- **Constants**: `MaxToolCallsPerMinute`, `DefaultTimeout` — PascalCase for exported, camelCase for unexported
- **Types**: `ToolDefinition`, `ToolResult` — PascalCase

### Imports

- Standard library first, then third-party, then internal
- Group imports: stdlib | external | internal
- Use explicit imports (no dot imports)
- Add newline between import groups

### Error Handling

- Return descriptive errors — include context
- Use sentinel errors for expected conditions: `ErrToolNotFound`, `ErrPolicyDenied`
- Wrap errors with `fmt.Errorf("context: %w", err)`
- Never silently ignore errors with `_`
- Handle errors at the appropriate level

### Context Usage

- Pass `context.Context` as first argument to functions that may timeout or be canceled
- Use `context.TODO()` when context is not yet available
- Set timeouts for external calls
- Check `ctx.Err()` in long-running operations

### Logging

- Use structured logging (zap or zerolog)
- Include request IDs for traceability
- Log at appropriate levels: Debug for details, Info for normal flow, Error for failures
- Don't log sensitive data (keys, tokens, passwords)

### Documentation

- Document all exported symbols with doc comments
- Include usage examples for complex APIs

---

## Key Patterns to Follow

1. **ToolBridge Pattern**: Wrap executor with middleware interface
2. **Validation First**: Validate tool calls before execution
3. **Filter Tool Definitions**: Remove tools before the LLM sees them
4. **Audit Everything**: Log all decisions with full context, including the delegation chain
5. **Fail Closed**: Missing context, unknown decisions, and unreachable policy endpoints deny — never allow
6. **Rate Limiting**: Track per-tool, per-user usage
7. **Config Hierarchy**: Project -> User -> Defaults
8. **Metadata-Only Control Plane**: Kei holds metadata (bindings, auth refs, scopes, audit) — never provider payloads, customer content, credentials, embeddings, or indexes

## Docs and Design References

- `business/` — PRD, engineering design, SDK plan, milestones. Read `ENGINEERING_DESIGN.md` and `SDK_PLAN.md` before architectural changes.
- `docs/tenant-proxy-reference.md` — **canonical** tenant-side proxy architecture and metadata-only control-plane boundary. Read before changing middleware, tool bindings, or the `kei/` surface.
- `docs/harness-contract.md` — the contract third-party agent builders implement against.
- `SCHEMA.md` — the wiki-memory page contract. `SCHEMA_GAPS.md` — **active**: ontology terms the read-only T-box lacks; add entries here rather than inventing terms locally.
- `docs/build-history/` — archived working files from the wiki-memory build loop. `DECISIONS.md` there explains why the component is shaped as it is. Historical, not maintained.
- `docs/{go,python,typescript}/` — usage examples per language.
- Staleness warning: older docs reference the pre-rename packages `middleware-py` / `middleware_py`. The actual Python package is `pedro_agentware`; actual Go types live in `go/middleware` (e.g. `middleware.Decision`, `tools.Result`). Trust the code over the docs.

Follow semantic versioning for releases.
