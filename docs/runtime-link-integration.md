# RuntimeLink Harness Integration Guide

This document defines the public API surface of `RuntimeLink` — the
harness→Agentware→kei-connector-runtime heartbeat supervisor. It is the
integration reference for harness authors (Assistant, PDE, Chat-Discord,
Chat-Slack, Chat-Teams, CLI). The same contract types exist in all three SDK
languages; examples here show Python, Go, and TypeScript side by side.

> **Boundary:** RuntimeLink only supervises the heartbeat path
> (harness→runtime→catalog). It never spawns an `authorize` subprocess, talks
> to the catalog directly, or reads credential values. See
> [`docs/tenant-proxy-reference.md`](tenant-proxy-reference.md) for the
> metadata-only control-plane boundary and
> [`docs/specs/runtime-heartbeat-liveness.md`](specs/runtime-heartbeat-liveness.md)
> for the normative spec.

---

## Integration Quickstart

The smallest harness integration creates a `RuntimeLinkConfig` from the
environment, starts the link, and stops it on shutdown:

```python
from pedro_agentware.kei.runtime_link import (
    RuntimeLink,
    RuntimeLinkConfig,
    HarnessEnvelope,
    config_from_env,
)

cfg = config_from_env(harness=HarnessEnvelope(kind="chat-discord", version="1.0.0"))
link: RuntimeLink = ...   # RuntimeLink protocol implementation
await link.start()
# ... harness runs; read link.status() for diagnostics only
await link.stop()         # on shutdown signal
```

```go
import "github.com/soypete/pedro-agentware/go/kei/runtimelink"

cfg, err := runtimelink.ConfigFromEnv(nil, runtimelink.HarnessEnvelope{
    Kind:    runtimelink.HarnessChatDiscord,
    Version: "1.0.0",
})
link, err := runtimelink.New(cfg)
if err != nil { /* invalid config */ }
ctx, cancel := context.WithCancel(context.Background())
defer cancel()
go link.Start(ctx)        // non-blocking; error only for invalid config
// ... harness runs
link.Stop(ctx)            // on shutdown
```

```typescript
import {
  runtimeLinkConfigFromEnv,
  normalizeRuntimeLinkConfig,
  type RuntimeLink,
  type HarnessKind,
} from "@haikeilabs/agentware";

const cfg = runtimeLinkConfigFromEnv(process.env, {
  kind: "chat-discord" as HarnessKind,
  version: "1.0.0",
});
const link: RuntimeLink = /* RuntimeLink implementation */;
await link.start();
await link.stop();        // on shutdown
```

---

## Recommended Integration Subset

Harness authors should use **only** these types. The remaining public types
exist for the SDK implementation and shared contract tests.

| Type / Function | Python | Go | TypeScript | Purpose |
| --- | --- | --- | --- | --- |
| **RuntimeLink** | `RuntimeLink` (Protocol) | `RuntimeLink` (interface) | `RuntimeLink` (interface) | Supervisor that owns the child process, watchdog, and event ring |
| **RuntimeLinkConfig** | `RuntimeLinkConfig` (dataclass) | `Config` (struct) | `RuntimeLinkConfig` (interface) | All config fields; duration units vary by language |
| **config_from_env** / **ConfigFromEnv** | `config_from_env(env, harness)` | `ConfigFromEnv(lookup, harness)` | `runtimeLinkConfigFromEnv(env, harness)` | Reads spec §3.2 env vars, returns normalized config |
| **normalize_config** / **NormalizeConfig** | `normalize_config(cfg)` | `NormalizeConfig(cfg)` | `normalizeRuntimeLinkConfig(cfg)` | Validates and clamps every field; raises on first violation |
| **HarnessKind** | `HarnessKind` (enum) | `HarnessKind` (string type) | `HarnessKind` (union type) | Closed set: `assistant`, `pde`, `chat-discord`, `chat-slack`, `chat-teams`, `cli` |
| **RuntimeIdentity** | `RuntimeIdentity` (dataclass) | `RuntimeIdentity` (struct) | `RuntimeIdentity` (interface) | Authoritative identity from catalog whoami (never from harness config) |
| **LinkStatus** | `LinkStatus` (dataclass) | `LinkStatus` (struct) | `LinkStatus` (interface) | Point-in-time diagnostic snapshot; never gates readiness |

---

## Configuration & Security Types

### BinaryRef

Names the runtime binary (`kei-proxy`) and its expected SHA-256 checksum. The
SHA is **required** when `deployment_env` is `"prod"`.

| Field | Python | Go | TypeScript |
| --- | --- | --- | --- |
| Binary path | `path: str` | `Path string` | `path: string` |
| SHA-256 hex | `sha256: str` | `SHA256 string` | `sha256: string` |

### SecretSource

Names the env var that holds the runtime token. The SDK **never reads the
value** — it passes the env var name to the child process, which reads it
directly from its own environment. Only `KEI_RUNTIME_TOKEN` is accepted;
`KEI_HARNESS_TOKEN` alone is refused with `legacy_token`.

| Field | Python | Go | TypeScript |
| --- | --- | --- | --- |
| Env var name | `env: str` | `Env string` | `env: string` |

### Backoff

Configures child restart backoff using full jitter: `floor(r × min(Max, Min ×
2^attempt))` where `r ∈ [0,1)`. The backoff resets after `stable_reset` seconds
of continuous `connected` or `degraded` state.

| Field | Python (seconds) | Go (time.Duration) | TypeScript (milliseconds) |
| --- | --- | --- | --- |
| Min delay | `min: float` | `Min time.Duration` | `minMs: number` |
| Max delay | `max: float` | `Max time.Duration` | `maxMs: number` |
| Stable reset | `stable_reset: float` | `StableReset time.Duration` | `stableResetMs: number` |

| Backoff helper | Python | TypeScript |
| --- | --- | --- |
| Compute delay | `Backoff.delay_ms(attempt, r)` | `backoffDelayMs(backoff, attempt, r)` |
| Sleep for delay (cancellable) | `Backoff.wait(attempt, r)` | `waitBackoff(backoff, attempt, options)` |

Go exposes backoff as an exported helper in the implementation slice.

### HarnessEnvelope

Declared, **non-authoritative** harness metadata. It labels heartbeats and
lifecycle events but is never used for authorization. The authoritative
identity comes from the runtime's `identity` event, which originates from
catalog whoami via the runtime token.

| Field | Python | Go | TypeScript | Constraint |
| --- | --- | --- | --- | --- |
| Kind | `kind: str` | `Kind HarnessKind` | `kind: string` | Must be a valid `HarnessKind` when link is enabled |
| Version | `version: str` | `Version string` | `version: string` | `[A-Za-z0-9._+-]{1,64}` |
| Deployment env | `deployment_env: str` | `DeploymentEnv string` | `deploymentEnv: string` | `[A-Za-z0-9._-]{1,32}` |

---

## Supervisor & Failure Types

### LinkState

Closed set of RuntimeLink supervisor states.

| State | Meaning |
| --- | --- |
| `disabled` | Link is not enabled (local-only mode; valid and not an error) |
| `starting` | Child process being spawned |
| `connected` | Child is running and heartbeats are arriving |
| `degraded` | Child is running but heartbeats are failing (catalog unreachable, errors) |
| `reconnecting` | Restarting after a crash or watchdog kill |
| `terminal` | Non-recoverable failure (unauthorized, contract mismatch) |
| `stopped` | `stop()` has completed |

### FailureClass

Closed failure taxonomy (spec §6.3) that separates harness↔runtime failures
from runtime↔catalog failures so alerts route to the correct owner.

| Class | Layer | Meaning |
| --- | --- | --- |
| `config_invalid` | harness↔runtime | Bad config (missing URL, bad kind, legacy-only token) |
| `runtime_unavailable` | harness↔runtime | Binary missing, SHA mismatch, capability probe failed |
| `runtime_crashloop` | harness↔runtime | >5 restarts in 15 minutes |
| `runtime_unresponsive` | harness↔runtime | Watchdog: no beat within `Interval + BeatTimeout + Grace` |
| `contract_mismatch` | either | Unknown protocol version or closed-set violation |
| `catalog_unreachable` | runtime↔catalog | Network / DNS / TLS failure to catalog |
| `catalog_timeout` | runtime↔catalog | Heartbeat POST timed out |
| `catalog_error` | runtime↔catalog | Catalog returned 5xx |
| `catalog_backpressure` | runtime↔catalog | Catalog returned 429 |
| `installation_unauthorized` | runtime↔catalog | 401: token revoked or installation disabled |
| `installation_stale` | catalog observation | No heartbeat within 2×interval + 15s |
| `installation_offline` | catalog observation | No heartbeat within 5×interval |
| `audit_backlog` | audit path | Pending records > threshold, last flush > 15m ago |
| `legacy_runtime_unverified` | harness↔runtime | Child lacks `--output` flag; legacy mode active |

---

## Lifecycle & Child-Process Protocol

### Lifecycle Events

The SDK emits six redacted lifecycle events on state transitions:

| Event Name | Trigger |
| --- | --- |
| `runtime.link.started` | `start()` called |
| `runtime.link.connected` | Identity event received from child |
| `runtime.link.degraded` | Beat failure reaches consecutive threshold |
| `runtime.link.reconnecting` | Restart backoff begins |
| `runtime.link.terminal` | Non-recoverable failure |
| `runtime.link.stopped` | `stop()` completed or context cancelled |

```python
async for event in link.events():
    logger.info("link: %s state=%s reason=%s", event.name, event.state, event.reason)
```

Each `LinkEvent` is **redacted by construction**: the type has no field that
could carry a token, argv, env, child stderr, provider payload or result,
reasoning, customer content, or a subject.

### Child stdout Wire Events (spec §4.1)

The child process writes JSONL events to stdout. The SDK parses them with
`parse_child_line()` and exposes them through the supervisor:

| Event | Fields | SDK type |
| --- | --- | --- |
| `identity` | `run_id`, `installation_id`, `org_id`, `workspace_id`, `platform`, `status`, `binding_status`, `runtime_version` | `RuntimeIdentity` (also returned by `link.identity()`) |
| `beat` | `run_id`, `seq`, `at`, `outcome`, `http_status`, `latency_ms`, `next_in_ms` | `BeatEvent` |
| `terminal` | `run_id`, `reason` | `TerminalEvent` |
| *(unknown event)* | — | `IgnoredEvent` / `{kind: "ignored"}` |

Only allowlisted fields are read from the wire; payloads, results, reasoning,
tokens, or any other key the runtime might emit are dropped.

### ChildProcessEnv

The child process receives **only** this allowlisted set of environment
variables — never the harness's full `os.environ`:

```text
KEI_RUNTIME_TOKEN, KEI_RUNTIME_CONTROL_PLANE_URL,
KEI_HARNESS_KIND, KEI_HARNESS_VERSION, KEI_DEPLOYMENT_ENV,
KEI_AGENTWARE_SDK_LANG, KEI_AGENTWARE_SDK_VERSION,
KEI_HEARTBEAT_RUN_ID, PATH, HOME, TZ
```

---

## Validation & Error Types

### RuntimeLinkConfigError (all languages)

Raised/thrown when a config field is invalid. Carries a stable machine-readable
`code` and the offending `field` name. The error **never** carries the
offending value, so a misplaced secret cannot leak through an error message.

| Code | Meaning |
| --- | --- |
| `invalid_value` | Field has an unparseable value |
| `out_of_range` | Field is outside its allowed bounds |
| `beat_timeout` | `beat_timeout` is ≤ 0 or ≥ `interval/2` |
| `harness_kind` | Kind is not in the closed set (or is empty when enabled) |
| `envelope` | Version or deployment_env violates its pattern |
| `legacy_token` | Only `KEI_HARNESS_TOKEN` is present (must be `KEI_RUNTIME_TOKEN`) |
| `token_missing` | `KEI_RUNTIME_ENABLED=true` but no token is set |
| `control_plane_url` | URL is missing, has credentials, or has an invalid scheme |
| `binary` | Binary path is empty or SHA is missing/invalid |

### ChildLineError (Python) / ChildLineError (TypeScript)

Thrown by `parse_child_line()` for lines the SDK must drop and count:

| Subtype | Python | TypeScript | Condition |
| --- | --- | --- | --- |
| Line too long | `LineTooLongError` | `kind: "line_too_long"` | Exceeds `MAX_CHILD_LINE_BYTES` (4096) |
| Malformed | `MalformedLineError` | `kind: "malformed"` | Not valid JSON, not an object, or bad field |
| Contract mismatch | `ContractMismatchError` | `kind: "contract_mismatch"` | Unknown `"v"` or outcome outside closed set |

### InvalidLinkEventError (Python, TypeScript)

Raised when constructing or validating a `LinkEvent` whose name, state, reason,
or other field is outside its closed set or bounds.

---

## Configuration Environment Variables

The SDK reads these through `config_from_env()` / `ConfigFromEnv()` /
`runtimeLinkConfigFromEnv()`. Harnesses may also construct `Config` directly.

| Variable | Default | Notes |
| --- | --- | --- |
| `KEI_RUNTIME_ENABLED` | unset → enabled iff token is set | `false` → state `disabled` (local-only, not an error) |
| `KEI_RUNTIME_TOKEN` | — | The only accepted credential var name |
| `KEI_RUNTIME_CONTROL_PLANE_URL` | — | Required when enabled |
| `KEI_PROXY_PATH` | `kei-proxy` | Runtime binary path |
| `KEI_PROXY_SHA` | — | Required in `prod` |
| `KEI_HEARTBEAT_INTERVAL` | `60` (s) | Clamped [15, 300] |
| `KEI_HEARTBEAT_TIMEOUT` | `10` (s) | Must be < interval/2 |
| `KEI_HEARTBEAT_RESTART_MIN` | `1` (s) | |
| `KEI_HEARTBEAT_RESTART_MAX` | `300` (s) | |
| `KEI_HEARTBEAT_STABLE_SECONDS` | `300` (s) | |
| `KEI_HEARTBEAT_LOG_COUNT` | `3` | First N beats per run logged at info |
| `KEI_HARNESS_KIND` | — | Overridden by programmatic `HarnessEnvelope` |
| `KEI_HARNESS_VERSION` | — | Overridden by programmatic `HarnessEnvelope` |
| `KEI_DEPLOYMENT_ENV` | — | Overridden by programmatic `HarnessEnvelope` |

---

## Usage Boundaries

### Permitted

- **Assistant, PDE, Chat-Discord, Chat-Slack, Chat-Teams, CLI adapters** —
  any harness whose `HarnessKind` is in the closed set may start a RuntimeLink.
- **Diagnostic-only status reads** — `link.status()` is safe for `/diag` or
  health-check endpoints, but must never gate harness readiness (spec §6.3).
- **Event iteration** — `link.events()` streams redacted lifecycle events for
  logging or metrics.

### Prohibited

- **No heartbeat semantics outside RuntimeLink.** Harnesses must not implement
  their own heartbeat loop, HTTP client to the catalog, or shell supervisor
  around `kei-proxy runtime heartbeat`. The SDK owns the entire lifecycle.
- **No catalog calls.** RuntimeLink never calls the catalog directly. All
  catalog communication goes through the child `kei-proxy` process.
- **No credential resolution.** `SecretSource` stores only the env-var name;
  the SDK never reads the token value. The child process receives the token
  through its own environment, not through argv or stdin.
- **No `os.environ` passthrough.** The child process receives exactly the
  allowlisted env vars. Harness secrets (Discord tokens, Twilio auth, Gemini
  keys) must never reach the child.
- **`LinkStatus` must not gate readiness.** A catalog outage puts the link in
  `degraded`; the harness must stay ready and serve requests. Tool calls still
  fail closed on their own through bundle and bootstrap gating (spec §6.3).
- **No payloads, results, reasoning, or customer data in events.** Lifecycle
  events are redacted by construction: they carry only envelope metadata,
  state, reason, and counters.

---

## Testing

Integration tests use the shared contract fixtures in
`testing/contracts/runtime-link/`. Run the contract scenarios per language:

```bash
# Python
cd python && pytest tests/kei/runtime_link_test.py -v

# Go
cd go && go test -v ./kei/runtimelink/...

# TypeScript
cd typescript && npx vitest run src/kei/runtimeLink.test.ts
```

To validate this documentation's examples compile and type-check:

```bash
# Python
cd python && python -c "
from pedro_agentware.kei.runtime_link import (
    RuntimeLink, RuntimeLinkConfig, HarnessKind, HarnessEnvelope,
    RuntimeIdentity, LinkStatus, BinaryRef, SecretSource, Backoff,
    LinkState, FailureClass, BeatEvent, TerminalEvent, ChildEvent,
    LinkEvent, EventHarness, LifecycleEventName, ChildLineError,
    RuntimeLinkConfigError, InvalidLinkEventError, config_from_env,
    normalize_config, parse_child_line,
)
print('All RuntimeLink types importable (Python)')
"

# TypeScript
cd typescript && npx tsc --noEmit --strict src/kei/runtimeLink.ts

# Go
cd go && go vet ./kei/runtimelink/...
```
