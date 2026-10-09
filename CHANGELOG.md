# Changelog

Notable changes to pedro-agentware (Go, Python, and TypeScript). Releases are
coordinated separately; entries collect under **Unreleased** until then.

## [0.8.0] - 2026-10-09

### Added

- **TypeScript:** opt-in `KeiProxySocketAuthorizeClient` for the `kei-proxy
  serve` daemon socket (#172). `openSession({ source, externalId })` opens a
  named `kei.session/v2` session (identity is always explicit, never inferred
  from `userId`); `authorize(session, request)` calls `POST /v1/authorize`
  with `X-Kei-Session`. Sessions are cached per identity and reopened once on
  `session_not_found`. Missing identity, socket errors, timeouts, `401`, and
  malformed responses fail closed. The runtime token is read from
  `KEI_RUNTIME_TOKEN` and sent only in the `Authorization` header. The
  one-shot `KeiProxyAuthorizeClient` is unchanged.
- Explicit typed routes in tool registries; connector manifest routes require
  agent identity, and connector routes take precedence over local handlers
  (#168).
- Kei tool manifest v2 and linter (HAI-279, #165).
- Model format parity for OpenAI, Anthropic, Qwen, DeepSeek and GLM in
  `toolformat` (#166).

### Changed

- Python evals are the canonical EV-C1 table-test runner (#170); agent-evals
  pull request gate added to CI (#171).

## [0.6.0] - 2026-09-29

### Changed

- **Breaking:** Removed all legacy `KEI_HARNESS_TOKEN` / legacy bootstrap-token
  handling across all SDKs (HAI-236). The environment names `KEI_HARNESS_TOKEN`,
  `LEGACY_BOOTSTRAP_SECRET_NAME`, and related legacy env vars are no longer read
  or rejected. Use `KEI_RUNTIME_TOKEN` exclusively.

### Fixed

- RuntimeLink heartbeat watchdog: the TypeScript watchdog now respects the
  configured interval, timeout, and grace window, eliminating the
  `runtime_unresponsive` → `runtime_crashloop` crash loop against a 60s
  `kei-proxy` heartbeat interval. All SDKs pass `--interval`, `--timeout`, and
  `--parent-stdin` to `kei-proxy runtime heartbeat` child processes.
  Requires `kei-proxy` ≥ 0.1.12.

## [0.5.0] - 2026-09-28

### Changed

- **Breaking (TypeScript):** `KeiProxyEvaluator.evaluate` now returns
  `Promise<Decision>`, and `KeiProxyAuthorizeClient.authorize` spawns
  `kei-proxy` asynchronously. The synchronous `spawnSync` path is **removed**,
  not deprecated: it blocked the Node event loop for up to `timeoutMs` on
  every Kei-gated call. `KeiProxyEvaluator` therefore no longer implements the
  synchronous `PolicyEvaluator`; await `evaluate` before running the tool.
  `KeiProxyAuthorizationClient.authorize` may return a value or a promise.

### Security

- kei-proxy authorize clients (TypeScript, Python, Go) no longer pass `PATH`
  or `HOME` to the child. The executable is resolved to an absolute path in the
  parent; a bare name is looked up in the absolute entries of the parent
  `PATH`. `AUTHORIZE_CHILD_ENV_ALLOWLIST` / `AuthorizeChildEnvAllowlist` drop
  both names.
- The kei-proxy child runs in its own process group (TypeScript `detached`,
  Python `start_new_session`, Go `Setpgid`), and a timeout SIGKILLs the whole
  group so no grandchild outlives the call.
- Optional binary pin: TypeScript `expectedSha256`, Python `expected_sha256=`,
  Go `CLIClient.ExpectedSHA256`. Before every spawn the path must equal its
  realpath, is opened with `O_NOFOLLOW`, must be a regular file, and is
  re-hashed and compared in constant time; any drift denies with the new
  reason class `pin_mismatch` without spawning.
- `fixtures/kei/authorize-cases.v1.json`: `pin_mismatch` reason class,
  `executable_pin` contract, three pinned-executable cases, `group_killed`
  on the timeout case, and `PATH`/`HOME` moved from `child_env.allowlist` to
  `child_env.stripped`.

### Added

- **KeiProxyEvaluator** — ported to Python (`pedro_agentware.kei`), TypeScript
  (`@haikeilabs/agentware/kei`), and Go (`go/kei/`), with a shared
  `authorize-cases` fixture table (`testing/contracts/kei-proxy/authorize/`)
  that every language tests against (HAI-155). The evaluator fails closed on
  every path that is not an explicit `permit`/`allow`.
  - Python: `KeiProxyEvaluator` with subprocess `kei-proxy authorize` client;
    table-driven tests against the shared fixture set.
  - TypeScript: `KeiProxyEvaluator` with dedicated CLI client module; add
    delegation fields (`delegate()`, `DelegationDepth`, `ParentSpan`) to
    `CallerContext` and `enrollment` to `Decision`.
  - Go: `KeiProxyEvaluator`, `Enrollment` added to `middleware.Decision`;
    completes the Go parity table.
  - All three: enrollment payload on DENY so the harness can show remediation
    even when a call is blocked.
  - Shared fixtures include `testing/contracts/kei-proxy/authorize/cases.json`
    and a fake proxy response harness.

- RuntimeLink: `RuntimeIdentity` exposes the installation's assigned agents
  from the runtime `identity` event (HAI-172). New fields: TypeScript
  `defaultAgentId?: string` and `agents: AssignedAgent[]`; Python
  `default_agent_id: str | None` and `agents: tuple[AssignedAgent, ...]`; Go
  `DefaultAgentID string` and `Agents []AssignedAgent`. A harness reads its
  agent from `link.identity()` and needs only `KEI_RUNTIME_TOKEN` — no
  agent-ID env var.
  - Tolerant of older runtimes: missing `agent_id` / `agents` parse as no
    default and an empty list.
  - Malformed `agent_id`, a non-list `agents`, or malformed entries are dropped
    and counted in `consecutive_fails` like other dropped child lines; the
    identity itself is still accepted.
  - When `agent_id` is absent, the first `is_default` entry supplies the
    default.
  - Parsers report the drop count: TypeScript `ChildEvent.droppedAgents`, Go
    `ChildEvent.DroppedAgents`, Python `parse_child_line_counted()`.
  - Shared contract fixture `testing/contracts/runtime-link/wire/child-events.json`
    gains agent cases; existing identity expectations now include
    `agent_id: ""` and `agents: []`.

## [0.4.0] - 2026-09-26

## [0.7.0] - 2026-09-30

### Added

- **Kei tool manifest export (HAI-271):** New `KeiScope` and `GovernedTool`
  types and `ExportKeiToolManifest` / `export_kei_tool_manifest` /
  `exportKeiToolManifest` in Go (`go/tools/tool.go`, `go/tools/registry.go`),
  Python (`pedro_agentware.tools/tool.py`, `pedro_agentware.tools/registry.py`),
  and TypeScript (`src/tools/tool.ts`, `src/tools/registry.ts`). Produces a
  structured manifest mapping tool definitions to their required scopes and
  connectors for Kei registration. Includes shared fixture
  `fixtures/kei/tool-manifest.v1.json` and design doc
  `docs/kei-tool-manifest.md`.

- **`connect` field on `Decision` (HAI-266):** All SDKs now carry an opaque
  `connect` block from `kei-proxy` on `deny` and `enrollment_required`
  decisions, mirroring `enrollment`. The payload (url, provider,
  connector_id, expires_at, reason) is a one-time claim link — surface it to
  the user, never log it. `KeiProxyEvaluator` passes the field through in all
  three languages.

### Changed

- **Breaking:** Removed `approval_id` from the delegation envelope (ADR-027).
  The Kei call-approval protocol no longer exists — approvals grant access and
  are not per-call signals. The field is dropped from delegation-envelope
  fixtures (`fixtures/kei/delegation-envelope-v1.json`) and delegation-boundary
  tests in Go, Python, and TypeScript. Proxy boundary docs updated to describe
  the proxy as an authorization/execution boundary without the per-call
  approval concept.

### Documentation

- New `docs/fail-closed-design.md` covering the `KeiProxyEvaluator` decision
  table, `KEI_PROXY_DISABLED` (fail-closed — never an allow switch), the
  governed vs non-governed tool call distinction, and test injection patterns
  for all three language ports.

### Chores

- `typescript/`: untrack `node_modules/` from git, switch CI to `npm ci`.
