# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

pedro-agentware is MCP-compatible middleware for LLM tool calling: it sits between an agent (LLM orchestrator) and tool execution, enforcing policy on every tool call (allow/deny/filter, rate limits, caller-context conditions) with auditing. The same library is implemented three times — **Go (`go/`, the reference implementation), Python (`python/`), and TypeScript (`typescript/`)** — with deliberately mirrored package structure and mirrored test suites. When changing shared logic (middleware, guardrails, toolformat, llmcontext, tools), check whether the counterparts in the other two languages and their tests need the same change.

The delegation contract: `CallerContext.InvokingSubject` is the human who initiated the request and is carried unchanged across every delegation hop (`CallerContext.Delegate` / `delegate()`); `ParentSpan` and `DelegationDepth` record where in the chain the call sits. `Trusted` defaults to **false** (fail-closed) in every language; a missing caller context is never promoted to trusted.

## Before Planning: Wiki First

Search the shared Herdr wiki before planning — `wiki search "tenant data
distributed proxy"` (and per-topic searches with `--top-k 10`). Read the
canonical tenant-side proxy architecture in `docs/tenant-proxy-reference.md`
and preserve its **metadata-only control-plane boundary**: `tool_bindings` and
`secret_refs` are non-secret metadata only; the distributed proxy is the
connector/provider runtime and policy enforcement point; ABAC decides metadata
policy but never executes writes or serves/store credentials; provider
payloads/results, customer content, credentials, embeddings, and indexes never
enter Kei; local execution stays possible without a direct ABAC connector
dependency.

## Shared Herdr Wiki

Architecture and coordination captures live in the shared Herdr wiki
(plugin: <https://github.com/Soypete/herdr-wiki-plugin>). All operations go
through the `wiki` command.

- Setup: `git clone https://github.com/Soypete/herdr-wiki-plugin ~/code/herdr-wiki-plugin`
  and add it to PATH: `export PATH="$HOME/code/herdr-wiki-plugin/bin:$PATH"`.
  If `wiki` is not on PATH, install it first and use it directly as `wiki`,
  never as `python3 .../bin/wiki`.
- Commands: `wiki search <query> [--top-k N] [--json]`, `wiki stats`,
  `wiki capture --title T --type T --content C [--link predicate:target ...]`,
  and `wiki organize` (**human only** — folds the inbox into the graph).
- Rules: `--type` and link predicates come from the closed vocabulary
  (`claim`/`contradiction`/`decision`/`entity`/`source`; predicates
  `derived_from`/`contradicts`/`supports`/`about`/`relates_to`). Captures land
  in an inbox; **workers never edit wiki pages directly** and always search the
  wiki before planning. Never read or edit wiki inbox JSON files directly.

## Commands

**Go** (run from `go/`; module `github.com/soypete/pedro-agentware/go`):

```bash
go test ./...                       # all tests
go test -v -run TestName ./middleware/   # single test
go vet ./... && golangci-lint run   # lint (CI runs golangci-lint)
go build ./cmd/pedro-proxy          # build the proxy binary
```

**Python** (run from `python/`; src-layout package `pedro_agentware`, Python ≥3.10):

```bash
pip install -e ".[dev]"
pytest                              # test files are *_test.py (not test_*.py); asyncio_mode=auto
pytest tests/caller_context_test.py -k test_name   # single test
ruff check src/ && mypy src/        # lint + strict typing (CI runs both)
```

**TypeScript** (run from `typescript/`; ESM, Node ≥18):

```bash
npm test
npm run build                       # tsc
npm run lint                        # eslint
```

**Evals** (from repo root; needs an OpenAI-compatible endpoint, override with `EVAL_BASE_URL`/`--base-url`):

```bash
make evals            # all evals; also: evals-file-search, evals-general, evals-clean
```

**Docker**: `docker-compose.yml` uses profiles — `llm` (pedrogpt on :8080), `mock` (kitaru-mock on :8081), `agent` (websearch-agent). E.g. `docker compose --profile mock up`.

CI workflows in `.github/workflows/` are path-filtered per language; `release.yml` does a weekly automated version bump.

## Architecture

Each language implementation contains the same packages (Go names shown; Python/TS mirror them):

- **`middleware/`** — the core: `Middleware` wraps a `ToolExecutor`; a `PolicyEvaluator` returns a `Decision` (allow / deny / filter-with-redacted-args) from the tool name, args, and `CallerContext` (user, session, role, trusted, delegation fields — carried in `context.Context` in Go); an `Auditor` records every decision. Rules match tools by glob, support condition operators (`eq`, `contains`, `matches`, `exists`, negations), and per-tool rate limits (`ratelimit.go`). Configured via chained options: `NewMiddleware(exec).WithPolicy(...).WithAuditor(...)`. `AuditedToolClient` (`tool_client.go` / `tool_client.py`) is the small dependency-free surface: a function in, a `Result` out, audited either way.
- **`middleware/guardrails/`** — agent-loop guardrails: error tracker, nudge, response validator, step enforcer.
- **`toolformat/`** — formats/parses tool calls for open models that lack native tool-call APIs: generic, llama, minimax, mistral, nemotron, qwen, plus a selector.
- **`llm/` + `llmcontext/`** — token counting, context-window accounting, and conversation compaction.
- **`llm/proxy/` + `cmd/pedro-proxy/`** (Go only) — `pedro-proxy`, an OpenAI-compatible HTTP proxy in front of a backend (llama-server, Ollama) that adds retries, context-window management, and compaction.
- **`tools/`, `executor/`, `jobs/`, `prompts/`** — tool registry/results, dispatch, background jobs, system-prompt generation with tool sections.
- **Adapters** — wrap agent backends behind a unified interface: Go `go/adapters/{adk,hermes}`; Python `python/adapters/{hermes,kitaru,pydantic}` (each with its own `pyproject.toml`, separate from the main package). See `python/adapters/README.md`.
- **`memory/`** (Go) — "wiki memory": an LLM-maintained, ontology-constrained markdown wiki scoped per user. `memory.Vault` resolves per-user directories (`<root>/<user>/wiki/` + `raw/`) with path-boundary containment; `memory/page` parses frontmatter + typed wikilinks and emits the A-box as N-Triples; `memory/ontology` loads the read-only T-box and validates pages (class/property existence, domain/range, SKOS cycles) returning structured `Violation` diagnostics. RDF handling uses `github.com/soypete/ontology-go` (the maintainer's library — use it for any RDF/TTL/SKOS work here, not a third-party lib). The T-box lives in the `ontologies/` git submodule (run `git submodule update --init` after cloning) and is read-only: missing terms go in `SCHEMA_GAPS.md`, never invented. The page contract is documented in `SCHEMA.md`.
- **`kei/`** (Python) — the KEI integration surface for third-party harnesses: `HarnessContract`, auth providers, proxy config, and `KeiProxyEvaluator`, a `PolicyEvaluator` that fails closed on every path that is not an explicit `permit`/`allow`. The contract third-party builders implement against is `docs/harness-contract.md`.
- **Tenant-side distributed proxy** — agentware provides local tool middleware, delegation, semantic tool bindings, and proxy integration. The distributed proxy is the connector/provider runtime and policy enforcement point; Kei is a metadata-only control plane. ABAC is called for metadata policy decisions only; provider payloads/results, customer content, credentials, embeddings, and indexes never enter Kei. See `docs/tenant-proxy-reference.md`.

## Docs and design references

- `business/` — PRD, engineering design, SDK plan, milestones. Read `ENGINEERING_DESIGN.md` and `SDK_PLAN.md` before architectural changes.
- `docs/tenant-proxy-reference.md` — canonical tenant-side proxy architecture and metadata-only control-plane boundary. Read before changing middleware, tool bindings, or the `kei/` surface.
- `docs/harness-contract.md` — the contract third-party agent builders implement against.
- `SCHEMA.md` — the wiki-memory page contract (frontmatter, typed wikilinks, ingest workflow). `SCHEMA_GAPS.md` — **active**: ontology terms the read-only T-box lacks; add entries here rather than inventing terms locally.
- `docs/build-history/` — archived working files from the wiki-memory build loop. `DECISIONS.md` there explains why the component is shaped as it is; worth reading before changing `go/memory`. Historical, not maintained.
- `docs/{go,python,typescript}/README.md` — usage examples per language.
- `AGENTS.md` — build/test commands and Go style conventions (naming, imports, error wrapping with `%w`, sentinel errors, structured logging).
- Staleness warning: older docs reference the pre-rename packages `middleware-py` / `middleware_py`. The actual Python package is `pedro_agentware`; actual Go types live in `go/middleware` (e.g. `middleware.Decision`, `tools.Result`). Trust the code over the docs.
