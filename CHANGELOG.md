# Changelog

Notable changes to agentware (Go, Python, TypeScript and Rust). Releases use
one shared version across npm, PyPI, Go and crates.io.

## Unreleased

### Added

- Anthropic native Messages tool-use and tool-result formatting with signed and redacted thinking preservation in TypeScript, Python, Go and Rust.
- Gemini 3 thought signatures: OpenAI-compatible backends round-trip per-tool-call provider extras (extra_content) in TS, Python, Go and Rust.
- Eval runner expectations in TypeScript, Python, Go and Rust: `expect` (`"call"` | `"no_call"`), partial `expectedArgs`, `repeats` and `minPassRate`, with `passes`/`repeats` on each result, a model seam for offline runs, and the shared fixture `fixtures/evals/expectations` replayed by every language. Rust gains an `evals` module. Existing cases and reports are unchanged.

## [0.16.0] - 2026-11-08

### Added

- **Rust:** the runtime socket client reaches parity with Go, Python and
  TypeScript: `list_models`, `model_config` and `list_model_profiles` (with
  `agent_id`/`profile_id` selectors and secret-field stripping),
  `chat_completion`, `stream_chat_completion` and `release_credential`. The
  crate is now `haikeilabs-agentware` (import `haikeilabs_agentware`) at the
  shared version, checked by `make version-check`.
- **Contracts:** shared runtime-socket cases for model config, model
  profiles and credential release, replayed by Go, TypeScript and Rust.
- **Python:** a replay of every shared runtime-socket fixture (constants,
  client cases and tool-sync cases), so all four SDKs now run the same
  contract.

- **Rust:** toolformat for qwen, llama, minimax, mistral and nemotron, so every
  tool-call family now has Rust, Go, Python and TypeScript implementations and
  `fixtures/EXCLUSIONS.md` has no Rust rows (#282).
- **Rust:** the `kei-proxy authorize` CLI fallback (`evaluator::CliClient`),
  the socket-or-CLI selector (`evaluator::select_authorization_client`) and
  the shared authorize-cases replay
  (`rust/agentware-kei/tests/authorize_cases.rs`), reaching Go, Python and
  TypeScript evaluator parity.

### Fixed

- **Python:** the parsers now fail closed in four cases where they previously
  accepted bad input, matching Go: the generic formatter rejects an empty tool
  name and arguments that are not an object, the anthropic formatter rejects
  `<invoke tool="">`, and reasoning `strip()` raises `MalformedJSONError`
  rather than `UnicodeDecodeError` for undecodable bytes (#288).
- **Rust:** `tool_args_digest` now sorts object keys at every depth like Go,
  so digests match across SDKs.
- **Rust:** the evaluator follows Go's decision mapping: enrollment and
  connect are carried only as JSON objects, only on deny and
  enrollment_required.

### Tests

- **Python:** Hypothesis property tests for the tool-call, manifest-v3 and
  reasoning parsers, with a nightly high-count job (#288).

## [0.15.0] - 2026-10-30

### Added

- **Python:** exact connector-target authorization for Unix-socket requests and named-session credential release.
- **Python:** load and validate `kei.tool-manifest/v3` into ToolSpecs (#270).
- **Python, TypeScript:** reasoning adapters accept OpenAI Responses and Anthropic content parts (#275, #276).

## [0.14.0] - 2026-10-29

### Added

- **Go, TypeScript:** load and validate `kei.tool-manifest/v3` files into
  ToolSpecs (`LoadManifestV3`/`ParseManifestV3`,
  `loadManifestV3`/`parseManifestV3`), with toolsync entry points that register
  typed specs or a manifest file (#263, #264).
- Shared `kei.tool-manifest/v3` loader fixtures (#260).
- Verified GLM-5.3-Flash tool-call and reasoning fixtures (#259).

### Fixed

- **Go:** `GenericFormatter` and the nemotron formatter decode string-encoded
  tool-call arguments, as GLM-5.3 emits them (#266).

### Documentation

- Add `docs/audit-visibility.md`: how governed tool calls become visible in
  Kei (#261).

### CI

- Add the tier-1 fixture conformance matrix and the `fixtures/EXCLUSIONS.md`
  equal-coverage gate (#267).
- Move the eval gate from deepseek-v4-flash to glm-5.3-flash (HAI-896) (#257).
- Bump vulnerable npm dependencies, including deepmerge (#256, #258).

### Not yet in this release

- **Python:** the `kei.tool-manifest/v3` loader is in progress.
- **Rust:** the toolformat and manifest loader are in progress.

## [0.13.0] - 2026-10-26

### Breaking

- **Go, Python, TypeScript:** remove the `ManifestV1`/`ManifestV2`/`ManifestV4`
  selections (Go) and `version=1/2/4` export paths in `export_kei_tool_manifest`
  and `exportKeiToolManifest`; `ExportKeiToolManifest`, `export_kei_tool_manifest`,
  and `exportKeiToolManifest` now export v3 by default. Remove v2 linter support.
  Use v3 (HAI-727, HAI-875).

## [0.12.0] - 2026-10-22

### Added

- **Go, Python, TypeScript:** release a session credential through the
  kei-proxy Unix socket after an allow decision.
- **Python:** rescue Qwen XML tool calls in the response validator.
- **Go:** minimal harness example demonstrates a governed deny and verifies
  the denied handler does not run.

### Documentation

- Add a cold-start dry-run runbook and fill runtime setup gaps in the
  getting-started guides.
- Explain deny hints and policy bundle refresh after tool registration.
- Link the README's governed harness guides to the Agentware docs site.
- Correct ToolSpec examples to use a valid tool manifest source.

## [0.11.0] - 2026-10-19

### Breaking

- **Go, Python:** Removed the local middleware policy engines, including
  Python `Policy`, `Rule`, and `SimplePolicyEvaluator` and the Go middleware
  policy engine (#220). Only the `PolicyEvaluator` interface remains; callers
  provide their own evaluator.

### Added

- **Go, Python, TypeScript:** ToolSpec, where one object feeds the per-model
  toolformat formatters and runtime registration.
- **Go, Python, TypeScript:** registrars that post a
  `kei.tool-manifest/v3` manifest to `/v1/tools:register`.
- Shared ToolSpec fixture and fake-socket route for `/v1/tools:register`.

## [0.10.0] - 2026-10-17

### Breaking

- **Go:** module path changed to
  `github.com/haikeilabs/agentware/go`, so
  replace `github.com/soypete/pedro-agentware/go` in imports and `go.mod`.
- **Go, Python, TypeScript:** removed caller-provided `agent_id` / `AgentID` /
  `agentId` from Kei authorize request types and client APIs. The runtime token
  supplies authorization identity.

### Added

- **TypeScript:** asynchronous `AuditedToolClient` and
  `CallerContext.delegate()`.
- **Go, Python, TypeScript:** Unix socket clients for `kei-proxy serve` and
  minimal harness examples for each language.
- Surface deny `reason_code` and `hint` in authorization decisions.
- Guardrails facades in Go, Python, and TypeScript.
- Getting-started guides and a runtime setup runbook.
- Shared release versioning for npm, PyPI, and Go, driven by the
  `agentware-v<VERSION>` release tag.

### Documentation

- README install instructions use the published npm, PyPI, and Go packages and
  list the minimum kei-proxy and kei-cli versions for each SDK release.

## [0.9.0] - 2026-10-09

### Added

- **Go, Python, TypeScript:** v4 tool manifest plan exporters, including a
  shared fixture and language-specific registry support (#169).

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
  connectors for Kei registration. Includes the shared manifest fixture and design doc
  `docs/kei-tool-manifest.md`.

- **`connect` field on `Decision` (HAI-266):** All SDKs now carry an opaque
  `connect` block from `kei-proxy` on `deny` and `enrollment_required`
  decisions, mirroring `enrollment`. The payload (url, provider,
  connector_id, expires_at, reason) is a one-time claim link — surface it to
  the user, never log it. `KeiProxyEvaluator` passes the field through in all
  three languages.

### Changed

- **Breaking:** Removed `approval_id` from the delegation envelope. The
  per-call approval field is no longer part of the delegation contract.
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
