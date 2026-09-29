# Changelog

Notable changes to pedro-agentware (Go, Python, and TypeScript). Releases are
coordinated separately; entries collect under **Unreleased** until then.

## Unreleased

### Fixed

- RuntimeLink now gives the TypeScript watchdog the configured interval,
  timeout, and grace window, and all SDKs pass configured heartbeat timing to
  v2 `kei-proxy` children. SDKs hold the parent-stdin pipe open until shutdown.

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

## Unreleased
