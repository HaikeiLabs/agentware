# Changelog

Notable changes to agentware (Go, Python, TypeScript and Rust). Releases use
one shared version across npm, PyPI, Go and crates.io.

## Unreleased

### Added

- Platform-delivered connector config for the operation runners. The socket client reads `GET /v1/agent-config` (kei-proxy 0.12.0 or later) in every language: Python `agent_config()`, TypeScript `agentConfig()`, Go `AgentConfig(ctx)`, Rust `agent_config()`; metadata only, credential and secret-reference fields removed. `connector_config(client, bindings)` (Python), `connectorConfig(client, bindings)` (TypeScript) and `operations.ConnectorConfigs(ctx, client, tools)` (Go) build the runner's config from it, keyed by the bound tool: the connection name and base URL come from the platform, the kind and header from the contracts `Connection` of that name, and the harness names no connector ID. When kei-proxy cannot answer (404 from an older daemon, 503 `agent_config_unavailable`, unreachable, malformed) they fail closed with a typed `connectors_unavailable` error. New shared socket parity cases in `testing/contracts/runtime-socket/client-cases.json`.
- Credential release without a connector for a registered connector_binding tool: the socket clients omit the `connector` object when the connector ID and capability are both empty and the tool and its `tool_args` are set (kei-proxy 0.12.0 derives the connector from the tool binding). The runners release this way for a platform-built entry (Python/TypeScript: an empty `connector_id`; Go: `ConnectorConfig.DeriveConnector`). An explicit connector entry keeps working unchanged, and the runners also look an entry up by tool name before the contract connector prefix.
- `fixtures/kei/KEI_PROXY_SHA256` carries the verified kei-proxy 0.12.0 Linux x86_64 checksum; `KEI_PROXY_VERSION` moves to v0.12.0 with the 0.19.0 release.

### Fixed

- Tool results serialize the same way in every formatter and every port. A string result is sent verbatim; a dict/object, list, number or boolean is compact JSON in key insertion order; no data is the empty string; errors keep each family's own convention. Before this, the Python generic, OpenAI, DeepSeek, GLM and Anthropic formatters sent a dict or list result as a Python repr (`{'temp': 22}`) and a boolean as `True`, and the Qwen formatter JSON-quoted a string result (`"Sunny, 22°C"`, which double-encoded a Go or Rust `Output` that already held JSON) and sorted dict keys in Python and TypeScript. Qwen now sends the result text as is between the `tool_response` tags, as the official Qwen chat template does. New `format_result` cases in every shared toolformat fixture pin the rule in all four ports.

## [0.18.0] - 2026-11-25

### Added

- An optional call id on the tool-result formatter in every language: Python `format_tool_result(name, result, call_id=None)`, Go `FormatToolResult(name, result, callID ...string)`, TypeScript `formatToolResult(name, result, callId?)`, and Rust `Formatter::format_tool_result_with_call_id(name, result, Option<&str>)` (a default method, so existing Rust implementations keep compiling). Only the Anthropic formatter uses it; the other families accept and ignore it. The Go and Rust inference executors now pass the id of the call they answer. Go code that implements `toolformat.ToolFormatter` itself must add the variadic parameter.
- OperationRunner in Python (`haikeilabs.agentware.operations`), Go (`kei/operations`) and TypeScript (`operations`): generated connector operations executed through kei-proxy. For one call the runner renders the operation's resource template from the generated op descriptor, asks the kei-proxy daemon for one decision on the tool call — the bare operation id (`github.pulls.list`) with the tool's arguments and context, so the daemon derives and decides the registered operation plan itself — and, on an explicit allow, releases the session credential for the bare operation id on the rendered resource before calling the generated operation with the released token. The runner sends the installed tool binding's `binding_version` on authorize (kei-proxy denies `binding_stale` on a mismatch, so the harness re-syncs the tool manifest), works against op grants that match with a trailing `.*` wildcard, and is fail-closed: anything that is not an explicit allow denies before any provider request, and errors carry reason codes only — never a credential or a provider payload.
- LangChain and Pydantic AI adapters (Python): `langchain_tool` builds a `langchain_core` BaseTool over the runner (args schema derived from the generated request message, every invocation authorized and audited in the caller's context, denies surface as `ToolException` with the reason code) and `register_pydantic_ai_tool` registers a plain tool on a pydantic_ai agent. New `langchain` / `pydantic-ai` extras; framework imports are lazy so each adapter stays usable without the other.
- The SDKs now depend on the published connector-contracts packages instead of vendoring generated code: `@haikeilabs/connector-contracts` (TypeScript), `haikeilabs-connector-contracts` (Python) and `github.com/HaikeiLabs/kei-connector-contracts/gen/go` (Go) supply the op descriptors, connection types and tool-manifest options the runners consume.
- Snowflake key-pair JWT helper for harness-side Snowflake SQL API auth, in Go (`connectors/snowflake`: `JWT`, `PublicKeyFingerprint`, `NormalizeAccountIdentifier`), Python (`haikeilabs.agentware.connectors.snowflake`, needs the new `snowflake` extra for `cryptography`) and TypeScript (`snowflakeJwt`, `snowflakePublicKeyFingerprint`, `normalizeSnowflakeAccountIdentifier`). The harness passes the private key PEM that the runtime releases; the helper mints an RS256 token with `exp` capped at one hour. It makes no network calls and refuses encrypted PEMs. The shared fixture `fixtures/snowflake/cases.v1.json` is replayed by all three languages.
- Shared reasoning-tag constants in Go (`reasoning.ThinkOpen` … `reasoning.TagPairs()`), Python (`haikeilabs.agentware.reasoning`: `THINK_OPEN` … `REASONING_TAG_PAIRS`), TypeScript (`THINK_OPEN` … `REASONING_TAG_PAIRS`) and Rust (`reasoning::THINK_OPEN` … `reasoning::REASONING_TAG_PAIRS`): open/close pairs for think, thinking and bracket THINK, each built from parts so no source file types a reasoning tag literally.
- The `mm:think` reasoning-tag pair (MiniMax M3 emits it inline in content) joins the shared constants in all four ports: Go `reasoning.MMThinkOpen`/`MMThinkClose`, Python `MMTHINK_OPEN`/`MMTHINK_CLOSE`, TypeScript `MMTHINK_OPEN`/`MMTHINK_CLOSE`, Rust `reasoning::MMTHINK_OPEN`/`reasoning::MMTHINK_CLOSE`. `TagPairs()`/`REASONING_TAG_PAIRS` now carry four pairs in the fixed order think, mm:think, thinking, bracket THINK, and the guardrails response validator strips the new pair as well (it builds from the same constants). New shared fixture `fixtures/reasoning/minimax-cases.json`, replayed by all four ports; its examples carry an optional per-example `options` field (today the only key is `implicit_open`).
- Opt-in close-tag-only reasoning (`implicit_open`; Go `Options{ImplicitOpen}`, Python `ReasoningOptions.implicit_open`, TypeScript `implicitOpen`, Rust `implicit_open`; default false): MiniMax M2 emits the closing tag without the opening one, and by default a stray close tag stays a fail-closed error. With the option on, everything before a leading think or mm:think close tag counts as reasoning; only those two closes may open implicitly, and only once per output — every other close tag (e.g. thinking) and a second stray close remains unbalanced and fails closed.

### Fixed

- Qwen tool formatting now uses Hermes JSON tool definitions and calls in all four languages. Tool results use a JSON value in a tool response block without the tool name. Legacy nested tool-tag parsing is deprecated and will be removed two minor releases after the release that introduces this change.
- Anthropic tool formatting now uses the Anthropic shapes in all four languages. Tool definitions are `{name, description, input_schema}` (they were OpenAI-style `{type: "function", function: {..., parameters}}`). `parse_tool_calls` reads `tool_use` blocks (one block or a content array) and keeps each block's `id` as the call id; malformed blocks are never returned as calls, and the `<function_calls><invoke>` form is still parsed when the response holds no `tool_use` block. Tool results are `tool_result` blocks with `tool_use_id`, `content` and, on failure, `is_error: true` (they were `<function_results>` XML with an `Error:` prefix). Without a call id, the result carries `tool_name` instead of `tool_use_id`. Validation checks `tool_use` blocks for an id, a name, an object input and duplicate ids.

- On a validation retry, the inference loop in Python, Go and TypeScript now appends the failed assistant turn before the retry nudge, so the next request ends with a user message. Current Claude models reject a request that ends with an assistant turn (they do not support assistant-message prefill), and a model otherwise reads the correction before the answer it corrects. The failed turn is kept only when it has presentable content; its tool calls are dropped (unanswered tool calls are rejected by the native APIs) and the nudge names the failed call instead. An empty or whitespace-only failed turn is skipped entirely.

- Pass TypeScript tool definitions to synchronous inference backends on every attempt.
- Tool input schemas are read one way in Python: the new public helper `haikeilabs.agentware.tools.schema_of(tool)` returns a tool's JSON schema whether it is declared as a dict attribute (`parameters` or `input_schema`, including a property) or a zero-argument method. `DefaultPromptGenerator.generate_tool_schemas`, `ToolRegistry.schemas` and the executor's tool mapping use it, so a method-style schema no longer crashes prompt generation with "Object of type method is not JSON serializable", an attribute-style schema is no longer rejected by `ToolRegistry.schemas` or silently replaced with an empty object in native tool calls, and the tool formatters now share the same lookup instead of a private copy. When a tool declares both `parameters` and `input_schema`, `parameters` wins, matching the formatters.
- The operation runner (Go `kei/operations`, Python `haikeilabs.agentware.operations`) now authorizes a connector_binding tool the way kei-proxy (proxy-v0.11.0 and later) decides it. Authorize sends the tool name and the tool call's `tool_args`/`tool_context`, and no `connector:<op>` action. The credential release capability is the bare operation id (`github.pulls.list`) and carries the same tool arguments. Before this fix, every planned call was denied `preflight_required` at authorize and `operation_plan_invalid` at release. Additive API: Python `OperationRunner.run(..., tool_args=, tool_context=)`; Go `Run(..., WithToolCall(tool, args, context))` and `ConnectorConfig.InstalledConnectorID`, the installed connector's bundle id for the release. Go now also sends the tool name on authorize and release. `fixtures/kei/fake-control-plane.py` gains an opt-in `operation-plan` bundle and the identity-mode credential route for end-to-end tests against a real `kei-proxy serve`.
- The reasoning adapter, in every language, now recognizes the plain think tag pair that Qwen3, QwQ and GLM emit when served without a reasoning parser; it previously handled only the thinking and bracket THINK pairs. The guardrails response validator now strips all three pairs too (it missed the thinking pair), across newlines (multi-line blocks were left in place), and the Rust validator's think pattern no longer has a stray newline that kept it from matching. Both paths use the shared constants. New shared fixtures: plain think cases in `fixtures/reasoning/{qwen,glm}-cases.json` and `fixtures/reasoning/validator-rescue.v1.json`.

## [0.17.0] - 2026-11-09

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
