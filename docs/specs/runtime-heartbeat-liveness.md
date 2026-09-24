# Spec: Shared runtime heartbeat and catalog liveness

**Status**: Proposed implementation specification. Nothing here is built yet.
**Date**: 2026-09-23
**Tracking**: HAI-141 (Agentware runtime-lifecycle SDK extraction). Attach every PR in §9 to it. Do not open overlapping tickets.
**Authority**: the wiki decision *"Heartbeat and liveness taxonomy: harness-to-runtime versus runtime-to-catalog"* (inbox `20260923T153736615413`). It **supersedes** the earlier generic note *"Heartbeat is runtime-to-catalog connectivity; liveness has runtime and catalog layers"* (inbox `20260923T153324729164`). Where the two disagree, this spec follows the newer one.
**Boundary**: `docs/tenant-proxy-reference.md` (metadata-only control plane). This spec does not relax it anywhere.

---

## 1. Taxonomy (normative)

| Term | Path | Proves | Owner |
| --- | --- | --- | --- |
| **Heartbeat** | harness → Agentware SDK `RuntimeLink` → tenant-side `kei-connector-runtime` (binary `kei-proxy`) | The configured harness is connected to a working runtime for its installation, right now. | **Agentware** owns the identity envelope, interval, timeout, retry/backoff, cancellation, redaction, and metric hooks. The harness only configures and starts it. |
| **Liveness** | `kei-connector-runtime` → `kei-policy-catalog` (`POST /api/v1/runtime/heartbeat`), as the catalog observes it | The catalog has recently heard from this runtime installation. | **kei-policy-catalog** owns freshness, `connected`/`stale`/`offline` state, fleet metrics, alerts, and audit-aggregator health. |
| **Readiness / diagnostics** | a harness-local `/health`, `/ready`, `whoami` | Only that the local process is up. | The harness. This is **never** a heartbeat or a liveness signal. |

The only permitted path is:

```
harness ──config──▶ Agentware SDK RuntimeLink ──(pinned child, stdio JSONL)──▶ kei-proxy runtime heartbeat
                                                                                      │  POST /api/v1/runtime/heartbeat (v2, metadata only)
                                                                                      ▼
                                                                            kei-policy-catalog ──▶ liveness state, metrics, alerts
kei-proxy authorize ──KEI_PROXY_AUDIT JSONL (digest-only)──▶ kei-proxy collector ──▶ POST /api/v1/runtime/audit/flush ──▶ audit aggregator
```

No harness implements heartbeat semantics of its own: no HTTP client to the catalog, no shell supervisor loop, no control-plane-shaped endpoint.

## 2. Current state (as of 2026-09-23)

Revisions inspected: Agentware `f5820d8`, kei-connector-runtime `origin/main e5d2181`, kei-policy-catalog `origin/main a96b3cb`, Assistant `origin/main`, PDE-Harness (local, contains uncommitted work), and the cross-harness audit captured in the wiki (inbox `20260923T153438143714`). For Kei-Chat-Harness this spec relies on that audit at `452da91`.

### 2.1 What exists

- **kei-connector-runtime** `runtime.go`: `runtime bootstrap` calls `WhoAmI`, sends one `Heartbeat`, and prints safe identity JSON. `runtime heartbeat` does the same, then loops `POST /api/v1/runtime/heartbeat {"runtime_version"}` on a fixed `--interval` (default 1m). Problems:
  - it **exits on the first failed POST**, with no retry or classification;
  - it never checks whether its parent is still alive, so an orphaned child keeps reporting a dead harness as live;
  - it writes nothing per beat, so a supervisor can't tell a healthy child from a stuck one.
- **kei-policy-catalog** `runtime_installations.go`: the heartbeat handler updates `runtime_version` and `last_heartbeat_at` for `pending|active` installations, looked up by token hash. It returns 204, or 401 when no rows matched. There is **no** derived freshness state, no interval awareness, no metrics, no transition audit, no 429/`Retry-After`, and no harness attribution.
- **Assistant**: `deployment/docker-entrypoint.sh` runs a shell supervisor around `kei-proxy runtime heartbeat` and the collector (PR #90/#91). It uses exponential backoff from `KEI_HEARTBEAT_RESTART_MIN` to `KEI_HEARTBEAT_RESTART_MAX`, resets after `KEI_HEARTBEAT_STABLE_SECONDS`, and logs the first `KEI_HEARTBEAT_LOG_COUNT` beats. It is tested by `test/docker_entrypoint_heartbeat.test.ts`. This is the **behavioral reference** to preserve.
- **Agentware**: Python has `kei/` (auth, manifest, `KeiProxyEvaluator`, `PolicyBundleLifecycle` with jittered backoff). Go and TypeScript have **no** `kei` surface. Nothing in any language mentions heartbeat or liveness.

### 2.2 Gaps and duplicated seams

| # | Gap / duplication | Where | Consequence |
| --- | --- | --- | --- |
| G1 | Three heartbeat paths | Assistant shell supervisor; Chat `RuntimeHeartbeatClient` (PR #147: direct httpx to the catalog, merged, unwired); `kei-proxy runtime heartbeat` | Competing semantics. #147 bypasses the runtime, which violates §1. |
| G2 | No heartbeat at all | PDE; Chat helm (only the audit-collector sidecar) | Liveness comes only from bootstrap and implicit per-`authorize` beats, so the catalog shows an idle bot as offline. |
| G3 | Runtime heartbeat exits on the first failure | connector-runtime | A catalog blip becomes a child crash, so harness↔runtime and runtime↔catalog failures can't be told apart. |
| G4 | Orphaned heartbeat child | connector-runtime (no parent-death detection) | Liveness can say "connected" while the harness is dead. |
| G5 | Catalog has a timestamp but no state | policy-catalog | No `stale`/`offline`, no alerts, no fleet view. |
| G6 | Heartbeat body is anonymous | `{"runtime_version"}` only | The catalog can't tell which harness kind or SDK version is attached. |
| G7 | Four `kei-proxy` subprocess wrappers | Assistant `KeiProxySubprocessTransport`, PDE `JsonLinesKeiProxy` (`--json-lines` isn't a command), Chat `KeiProxyClient`, Agentware `LocalProxyProcess` (`--port`/`--api-url` aren't commands) | Two are non-functional. Three pass the full `os.environ` (Discord/Twilio/Gemini secrets) to the child. |
| G8 | Three copies of the bootstrap call | Assistant entrypoint, Chat `main.py`, and Chat `teams_main.py`; PDE bootstrap is manual | Fatal vs. non-fatal behavior differs (open decision D0). |
| G9 | Harness-local control-plane-shaped routes | Chat `headless_main.py` serves `/api/v1/runtime/{whoami,heartbeat}` (PR #146) | Easy to mistake for the catalog. Violates §1 row 3. |
| G10 | `CallerContext` lineage parity | Go has no `SpanID/AgentID/AgentVersion/Framework/WorkspaceID`; TS has no delegation fields at all | TS/Go harnesses can't populate `KEI_PROXY_*` lineage. |
| G11 | Manifest guard misses the live credential name | `python/.../kei/config.py` `BOOTSTRAP_SECRET_NAME = "KEI_HARNESS_TOKEN"` | **RESOLVED**: `BOOTSTRAP_SECRET_NAME` changed to `"KEI_RUNTIME_TOKEN"`; `validate_manifest` now rejects both `KEI_RUNTIME_TOKEN` and the legacy `KEI_HARNESS_TOKEN`. |

## 3. SDK interface (normative)

The Go implementation is the reference. Python and TypeScript mirror its names and behavior, and share one contract suite (§8.2).

| Language | Package |
| --- | --- |
| Go | `go/kei/runtimelink` |
| Python | `pedro_agentware.kei.runtime_link` |
| TypeScript | `typescript/src/kei/runtimeLink.ts`, exported from the root barrel |

### 3.1 Types

```go
// RuntimeLink supervises the harness→runtime heartbeat. Start is non-blocking.
type RuntimeLink interface {
    Start(ctx context.Context) error        // error ONLY for invalid config; never for network state
    Stop(ctx context.Context) error         // idempotent; bounded by Config.StopTimeout
    Status() LinkStatus                     // lock-free snapshot, safe from /ready handlers
    Identity() (RuntimeIdentity, bool)      // false until the runtime reports identity
    Events() <-chan LinkEvent               // bounded (64); drops oldest, counts drops
}

func New(cfg Config) (RuntimeLink, error)   // validates; returns ErrConfig* sentinels (wrapped with %w)

type Config struct {
    Enabled         bool             // default: true iff a runtime token is resolvable
    Binary          BinaryRef        // {Path, SHA256}; re-verified before EVERY spawn
    ControlPlaneURL string           // passed to child as KEI_RUNTIME_CONTROL_PLANE_URL
    Token           SecretSource     // KEI_RUNTIME_TOKEN only; never read from a manifest
    Interval        time.Duration    // default 60s; clamp [15s, 300s]
    BeatTimeout     time.Duration    // default 10s; must be < Interval/2
    Grace           time.Duration    // default 15s
    Restart         Backoff          // Min 1s, Max 300s, StableReset 300s, full jitter
    StopTimeout     time.Duration    // default 10s
    Harness         HarnessEnvelope  // declared, non-authoritative (§5.1)
    Metrics         MetricsSink      // optional; non-blocking; panics/exceptions swallowed and counted
    Audit           LifecycleAuditSink // optional; receives redacted lifecycle records (§5.3)
    Logger          Logger           // structured; never receives token, argv, or child stderr text
}

type HarnessEnvelope struct {
    Kind          string // closed set: assistant | pde | chat-discord | chat-slack | chat-teams | cli
    Version       string // harness build version, ≤64 chars, [A-Za-z0-9._+-]
    DeploymentEnv string // e.g. prod | staging | dev, ≤32 chars
}

type LinkState string // disabled | starting | connected | degraded | reconnecting | terminal | stopped

type LinkStatus struct {
    State            LinkState
    Reason           FailureClass // §6; empty when connected
    LastBeatAt       time.Time     // last beat event received from the child (any outcome)
    LastCatalogOKAt  time.Time     // last beat the runtime reported as outcome=ok
    ConsecutiveFails int
    RunID            string        // current child run id
    Restarts         int
}
```

Python and TS use the language's idioms: `async start()/stop()`, `AbortSignal` in TS, `asyncio.CancelledError` propagation in Python. The field names and defaults stay the same.

### 3.2 Configuration schema (environment)

The SDK reads these through `ConfigFromEnv()` / `config_from_env()` / `configFromEnv()`. Harnesses may also construct `Config` directly. The canonical names are the Assistant's existing ones.

| Variable | Default | Notes |
| --- | --- | --- |
| `KEI_RUNTIME_ENABLED` | unset → enabled iff `KEI_RUNTIME_TOKEN` is set | `false` → state `disabled` (local-only mode is valid and is not an error). |
| `KEI_RUNTIME_TOKEN` | — | The only credential. It reaches the child through env only. |
| `KEI_RUNTIME_CONTROL_PLANE_URL` | — | Required when enabled. |
| `KEI_PROXY_PATH`, `KEI_PROXY_SHA` | — | The binary and its SHA-256. The SHA is required in `prod`. |
| `KEI_HEARTBEAT_INTERVAL` | `60` (s) | |
| `KEI_HEARTBEAT_TIMEOUT` | `10` (s) | New name. |
| `KEI_HEARTBEAT_RESTART_MIN` / `_MAX` | `1` / `300` (s) | |
| `KEI_HEARTBEAT_STABLE_SECONDS` | `300` | |
| `KEI_HEARTBEAT_LOG_COUNT` | `3` | The first N beats of each run are logged at info level. |
| `KEI_HARNESS_KIND`, `KEI_HARNESS_VERSION`, `KEI_DEPLOYMENT_ENV` | — | Envelope. An unknown `KIND` is `ErrConfigHarnessKind`. |

Refused (hard `ErrConfig`, fail closed): `KEI_HARNESS_TOKEN` set **without** `KEI_RUNTIME_TOKEN` (ADR-020 / HAI-119), and a `BeatTimeout` that is ≥ `Interval/2`.

### 3.3 Harness integration surface

Each harness writes about this much and nothing more:

```python
link = runtime_link.new(runtime_link.config_from_env(harness=HarnessEnvelope(kind="chat-discord", version=__version__)))
await link.start()
...                                   # readiness reads link.status() only for diagnostics (§6.3)
await link.stop()                     # on shutdown signal
```

## 4. Wire contracts

### 4.1 SDK → runtime (child process)

```
argv: <KEI_PROXY_PATH> runtime heartbeat --interval <s>s --timeout <s>s --output jsonl --parent-stdin
env : exactly the allowlist {KEI_RUNTIME_TOKEN, KEI_RUNTIME_CONTROL_PLANE_URL, KEI_HARNESS_KIND,
      KEI_HARNESS_VERSION, KEI_DEPLOYMENT_ENV, KEI_AGENTWARE_SDK_LANG, KEI_AGENTWARE_SDK_VERSION,
      KEI_HEARTBEAT_RUN_ID, PATH, HOME, TZ}   — never os.environ
stdin: a pipe held open by the SDK; EOF means the parent is gone, so the child must exit 0 within 1s (--parent-stdin)
stdout: JSONL events (below), each line ≤ 4 KiB; longer lines are dropped and counted
stderr: drained and discarded, counted by bytes only; never logged, reflected, or parsed
process: own process group; Stop = close stdin → SIGTERM group → wait StopTimeout/2 → SIGKILL group
```

Child stdout events have `"v":1`. Unknown `event` values are ignored; unknown `v` counts as `contract_mismatch`.

```json
{"v":1,"event":"identity","run_id":"…","installation_id":"…","org_id":"…","workspace_id":"…","platform":"discord","status":"active","binding_status":"verified","runtime_version":"1.9.0"}
{"v":1,"event":"beat","run_id":"…","seq":42,"at":"2026-09-23T17:00:00Z","outcome":"ok","http_status":204,"latency_ms":83,"next_in_ms":60000}
{"v":1,"event":"terminal","run_id":"…","reason":"unauthorized"}
```

`outcome` ∈ `ok | catalog_unreachable | catalog_timeout | catalog_error | catalog_backpressure | catalog_rejected | unauthorized`.

Exit codes:

| Code | Meaning | SDK response |
| --- | --- | --- |
| `0` | Graceful (stdin EOF or SIGTERM) | Restart only if the SDK didn't initiate it. |
| `1` | Transient or internal | Restart with backoff. |
| `2` | Usage / flag error | `terminal:contract_mismatch` |
| `3` | Unauthorized, revoked, or inactive installation | `terminal:installation_unauthorized` |
| `4` | Invalid runtime config | `terminal:config_invalid` |

**Capability negotiation.** Before the first spawn the SDK runs `kei-proxy runtime heartbeat --help` with a 2s timeout and checks for `--output`.

- If it is present, the SDK uses v2 as described above.
- If it is absent (legacy runtime), the SDK runs `runtime heartbeat` without the new flags and treats "child alive, and the identity line received" as the only signal. It reports state `connected` with `Reason=legacy_runtime_unverified`, which matches the Assistant shell supervisor exactly. Legacy mode is removed one minor release after connector-runtime v2 reaches every harness.

### 4.2 Runtime → catalog: `POST /api/v1/runtime/heartbeat` (v2)

```json
{
  "runtime_version": "1.9.0",
  "schema_version": 2,
  "run_id": "uuid",
  "seq": 42,
  "interval_seconds": 60,
  "runtime_started_at": "2026-09-23T16:00:00Z",
  "harness": {"kind": "chat-discord", "version": "2026.09.23", "deployment_env": "prod",
              "sdk_lang": "python", "sdk_version": "0.4.0", "link": "attached"},
  "counters": {"beats_failed_since_ok": 0, "audit_pending_records": 3,
               "audit_last_flush_ok_at": "2026-09-23T16:59:10Z"}
}
```

- **Identity comes only from the bearer token hash.** The body has no installation, org, workspace, or subject fields. The catalog rejects unknown top-level keys with 400 so nothing can creep in later.
- Every string is length-bounded and charset-validated. `harness.kind` is a closed enum. The whole body must be ≤ 2 KiB.
- **Backward compatible:** a body with only `runtime_version` stays valid (v1). The catalog marks it `harness.kind = unknown`.
- Responses:

  | Status | Meaning |
  | --- | --- |
  | 204 | Recorded. |
  | 400 | Contract. |
  | 401 | Token unknown, or installation not `pending\|active`. |
  | 429 | Backpressure, with `Retry-After` in seconds. |
  | 5xx | Catalog error. |

  Response bodies carry no detail beyond a fixed error string.

### 4.3 Catalog read surface

`GET /api/v1/internal/runtime-installations[/{id}]` gains the following. All values are derived; nothing is stored as authoritative client input.

```json
{"liveness": {"state": "connected|stale|offline|never_seen|suspended",
              "last_heartbeat_at": "…", "interval_seconds": 60, "age_seconds": 41,
              "harness_kind": "chat-discord", "sdk_version": "0.4.0", "runtime_version": "1.9.0",
              "run_id": "…", "catalog_observation_ok": true}}
```

## 5. Identity and audit lineage

### 5.1 Identity envelope (three tiers; never promote a lower tier)

| Tier | Fields | Source | Use |
| --- | --- | --- | --- |
| **Authoritative** | `installation_id`, `org_id`, `workspace_id`, `platform`, `status`, `binding_status` | The runtime's `identity` event, which comes from catalog `whoami` via the token | Scoping, audit, catalog rows. A missing `workspace_id` → `terminal:config_invalid` for runtimes that require one. |
| **Declared** | `harness.kind`, `harness.version`, `deployment_env`, `sdk_lang`, `sdk_version`, `runtime_version` | Harness config and build | Labels and attribution only. Never used for authorization. |
| **Correlation** | `sdk_instance_id` (UUID per SDK start), `run_id` (UUID per child spawn), `seq` | Generated by the SDK and the runtime | Joins SDK metrics ↔ runtime events ↔ catalog rows. |

`HarnessManifest.installation_id` and any `KEI_ORG_ID`-style config are declared, not authoritative. They must match the identity event, or the SDK logs an `identity_mismatch` diagnostic. The identity event wins.

### 5.2 Tool-call lineage (unchanged contract; parity required)

The canonical audit context from `c2304ab` maps to the child env for `authorize`:

| `CallerContext` field | Env var |
| --- | --- |
| `span_id` | `KEI_PROXY_SPAN_ID` |
| `invoking_subject` (pseudonymous) | `KEI_PROXY_INVOKING_SUBJECT` |
| `parent_span` | `KEI_PROXY_PARENT_SPAN` |
| `delegation_depth` | `KEI_PROXY_DELEGATION_DEPTH` |
| `agent_id` | `KEI_PROXY_AGENT_ID` |
| `agent_version` | `KEI_PROXY_AGENT_VERSION` |
| `framework` | `KEI_PROXY_FRAMEWORK` |
| `tool_args_digest` | `--args-digest` |
| `workspace_id` | Must equal the authoritative `workspace_id`; otherwise deny |

The runtime stamps `installation_id` and `run_id` onto each JSONL audit record. Go and TS `CallerContext` gain the missing fields (G10), with identical `delegate()` semantics.

### 5.3 Lifecycle audit records (redacted by construction)

The SDK emits `runtime.link.{started,connected,degraded,reconnecting,terminal,stopped}`. The catalog emits `runtime.liveness.transition`. Both use a closed schema: envelope fields, `state`, `reason`, `run_id`, `seq`, timestamps, and counts. **Those types have no field that could carry** a token, argv, env, child stderr, provider payload or result, customer content, or plaintext channel user id.

Subjects are pseudonymized before they reach any SDK record, using a tenant-side keyed digest. Lifecycle records go to the harness's `LifecycleAuditSink` (tenant-side) and, through the runtime's audit JSONL → collector → `POST /api/v1/runtime/audit/flush`, to the catalog's audit aggregator as metadata only.

## 6. Behavior

### 6.1 Timing, retry, backoff, cancellation, backpressure

**SDK (harness↔runtime)**

- **Child restart backoff:** `min(Max, Min·2^n)` with full jitter. `n` resets after `StableReset` of continuous `connected|degraded`. This matches Assistant PR #90.
- **Beat watchdog:** no `beat` event for `Interval + BeatTimeout + Grace` → `runtime_unresponsive`. The SDK then kills the process group and restarts.
- **Exit 3 or 4:** state `terminal`. The SDK retries at `Max` (300s) so it picks up a rotated token, and never hot-loops.
- **Cancellation:** `Stop` or `ctx` cancel closes stdin, then SIGTERMs the group, then SIGKILLs it after `StopTimeout/2`. It never leaves an orphan, and it is idempotent. Context cancellation is the only way to stop a Python or TS link; no daemon threads outlive `stop()`.
- **Backpressure inside the SDK:** stdout is read continuously on a dedicated reader. Events go to a 64-slot ring that drops the oldest entry. `MetricsSink` and `LifecycleAuditSink` run off the reader path with a bounded queue (256) that drops and counts. A slow or throwing sink can never delay the watchdog or block the harness event loop.

**Runtime (runtime↔catalog)**

- **Tick:** `interval ± 10%` jitter so the fleet doesn't pulse. Each POST has a `--timeout`.
- **Transient failures** (`catalog_unreachable|catalog_timeout|catalog_error`): at most one in-tick retry after `min(5s, interval/4)`, then an event with that outcome, then **keep running**. This fixes G3.
- **429:** sleep `min(Retry-After, 5×interval)`, then emit `catalog_backpressure`.
- **401:** emit `terminal`, then exit 3.
- **400:** emit `terminal`, then exit 2. (The SDK does not restart on contract errors because they won't fix themselves.)
- **stdin EOF:** exit 0 within 1s. This fixes G4.

**Catalog**

- A heartbeat is an O(1) indexed update. It is rate-limited per token hash to 1 accepted beat per 10s; excess beats get 429 `Retry-After: 10`.
- Under global overload the catalog sheds heartbeats with 429 before it sheds `authorize` or `audit/flush`.

### 6.2 Catalog freshness (derived at read time and by a 30s sweeper)

Let `I = clamp(interval_seconds, 15, 300)` (60 for v1 bodies) and `age = now − last_heartbeat_at`. States are checked in this order; the first match wins.

| State | Condition |
| --- | --- |
| `suspended` | `status ∈ {disabled, revoked}` |
| `never_seen` | `last_heartbeat_at IS NULL` |
| `connected` | `age ≤ 2I + 15s` |
| `stale` | `age ≤ 5I` |
| `offline` | otherwise |

With the defaults that is connected ≤ 135s, stale ≤ 300s, and offline > 300s.

**Catalog-outage guard:** if the catalog's own ingest was unavailable, freshness is stretched instead of trusted. The sweeper doesn't emit `stale→offline` transitions or per-installation alerts while `catalog_observation_ok=false`. That flag is false when either:

- process uptime < `5I_max`, or
- the fleet-wide accepted-heartbeat rate has dropped more than 50% from its trailing 1h baseline.

This keeps a catalog incident from being reported as a fleet of dead tenants.

### 6.3 Failure classification

`FailureClass` is a closed enum shared by SDK status, events, metrics labels, and alerts.

| Class | Layer | Detected by | Typical cause | Actionable owner |
| --- | --- | --- | --- | --- |
| `config_invalid` | harness↔runtime | SDK `New`, exit 4 | Missing URL/token, bad kind, legacy-only token | Harness deployer |
| `runtime_unavailable` | harness↔runtime | SDK pre-spawn | Binary missing, SHA drift, capability probe failed | Harness deployer / packaging |
| `runtime_crashloop` | harness↔runtime | SDK: >5 restarts in 15m | Runtime bug, resource limits | Harness owner, then connector-runtime |
| `runtime_unresponsive` | harness↔runtime | SDK watchdog | Hung child, blocked pipe | Harness owner, then connector-runtime |
| `contract_mismatch` | either | Exit 2, unknown `v`, catalog 400 | Version skew | Whoever deployed last |
| `catalog_unreachable` / `catalog_timeout` | runtime↔catalog | Runtime beat outcome | Tenant egress, DNS, TLS, gateway | Tenant network, *unless* fleet-wide |
| `catalog_error` | runtime↔catalog | 5xx | Catalog bug or DB outage | **Catalog on-call** |
| `catalog_backpressure` | runtime↔catalog | 429 | Catalog shedding | Catalog on-call if sustained |
| `installation_unauthorized` | runtime↔catalog | 401, exit 3 | Revoked or rotated token, disabled installation | Tenant admin |
| `installation_stale` / `installation_offline` | catalog observation | Sweeper | Any of the above, or the harness is down | Routed by the co-reported SDK/runtime class when present |
| `audit_backlog` | audit path | `audit_pending_records` rising and last flush older than 15m | Collector down, aggregator down | Harness owner, or catalog if fleet-wide |

**Readiness rule:** SDK state never gates a harness `/ready` or liveness probe. Otherwise a catalog outage would restart the whole fleet. Tool calls still fail closed on their own through bundle and bootstrap gating.

`link.status()` may appear in a diagnostics endpoint under a harness-local path such as `/diag/runtime-link`, and **never** under `/api/v1/runtime/*` (G9).

Whether a *bootstrap* failure blocks the listener is decision **D0** (§9). It is still open. Whatever D0 chooses, heartbeat state does not.

## 7. Metrics and alerts

### 7.1 Harness↔runtime connectivity (emitted tenant-side by the SDK through `MetricsSink`)

Labels: `harness_kind`, `sdk_lang`, `deployment_env`, and where noted `state|reason|outcome`. `installation_id` goes on exemplars/logs only, never as a label.

| Metric | Type |
| --- | --- |
| `agentware_runtime_link_state{state}` | gauge (0/1) |
| `agentware_runtime_link_restarts_total{reason}` | counter |
| `agentware_runtime_beat_last_seconds` (age of last beat event) | gauge |
| `agentware_runtime_beats_total{outcome}` (runtime-reported catalog outcome) | counter |
| `agentware_runtime_link_terminal{reason}` | gauge |
| `agentware_runtime_link_events_dropped_total`, `agentware_runtime_link_sink_errors_total` | counter |
| `agentware_audit_collector_restarts_total`, `agentware_audit_pending_records` | counter / gauge |

Tenant alerts go to the harness owner:

- **RuntimeLinkDown**: `state ∉ {connected, degraded}` for 5m.
- **RuntimeLinkCrashloop**: restarts > 5 in 15m.
- **RuntimeLinkUnauthorized**: `terminal{reason="installation_unauthorized"}` → immediate.
- **CatalogUnreachableFromTenant**: `beats_total{outcome=~"catalog_unreachable|catalog_timeout"}` makes up > 50% of beats over 15m.

### 7.2 Runtime↔catalog liveness (emitted by kei-policy-catalog)

| Metric | Type |
| --- | --- |
| `kei_catalog_runtime_heartbeats_total{result="accepted\|unauthorized\|invalid\|throttled\|error", schema}` | counter |
| `kei_catalog_runtime_installations{liveness_state, platform, harness_kind}` | gauge, from the sweeper |
| `kei_catalog_runtime_heartbeat_age_seconds` | histogram |
| `kei_catalog_runtime_liveness_transitions_total{from, to}` | counter |
| `kei_catalog_observation_ok` | gauge |
| `kei_catalog_audit_flush_total{result}`, `kei_catalog_audit_aggregator_last_success_timestamp` | counter / gauge |

Catalog alerts:

- **FleetLivenessDrop** (page catalog on-call): more than 20% of `active` installations left `connected` within 5m. This **suppresses** InstallationOffline.
- **HeartbeatIngestErrors** (page catalog): `result="error"` > 5% for 10m.
- **AuditAggregatorStale** (page catalog): no successful aggregation for 15m.
- **InstallationOffline** (ticket, not page; routed to the tenant's owner): an `active` installation is `offline` for 10m and `catalog_observation_ok=1`.
- **InstallationNeverSeen** (ticket): `pending` for 24h with no heartbeat.

The split is what makes alerts actionable. Tenant-side `CatalogUnreachableFromTenant` plus a catalog that is healthy (no FleetLivenessDrop) points to a tenant network problem. FleetLivenessDrop plus HeartbeatIngestErrors points to the catalog.

## 8. Tests

### 8.1 Unit tests (per repo, per language)

- **Agentware (Go/Py/TS):**
  - config validation and clamps; legacy-token refusal
  - env allowlist (an exact-set assertion, plus canary secrets `DISCORD_TOKEN`/`TWILIO_AUTH_TOKEN` that must be absent from the child env)
  - argv construction
  - event parser truth table: valid, unknown event, unknown `v`, > 4 KiB, non-JSON, partial line
  - state machine over every (state, event) pair
  - exit-code mapping
  - backoff math: bounds, jitter range, stable reset
  - watchdog timing with a fake clock
  - `Stop` is idempotent and kills the process group
  - a throwing or slow sink doesn't delay the watchdog
  - redaction: lifecycle records serialize only allowlisted keys
  - manifest guard rejects `KEI_RUNTIME_TOKEN` and `KEI_HARNESS_TOKEN` (G11)
  - `CallerContext` parity fields and `delegate()`
- **connector-runtime:**
  - `--output jsonl` golden lines
  - no exit on 5xx or timeout
  - exit 3 on 401, exit 2 on 400
  - `Retry-After` is honored and capped
  - stdin EOF → exit 0 in under 1s
  - tick jitter bounds
  - the v2 body matches the golden fixture, and a redaction test shows no env or secret values appear in the body or on stdout
- **policy-catalog:**
  - freshness function table at every boundary (2I+15s, 5I, clamps, v1 default)
  - v1 and v2 bodies accepted; unknown keys and oversized bodies get 400
  - 401 for revoked or disabled installations
  - per-token 429
  - sweeper transitions, and suppression while `catalog_observation_ok=false`
  - migration Up/Down (use the next free version checked against `origin/main`; see the migration-031 collision note)

### 8.2 Contract tests (shared fixtures)

- `testing/contracts/runtime-link/`, added in Agentware:
  - `fake-kei-proxy.sh`, a POSIX script whose behavior is driven by `FAKE_SCENARIO`
  - `scenarios/*.json` holding inputs and the **expected state/event sequence**
  - `wire/*.json` holding golden child events and v2 heartbeat bodies
- All three Agentware languages run every scenario and must produce identical sequences:
  - happy path
  - catalog 5xx burst → `degraded` → recovery
  - child crash ×N → bounded backoff → stable reset
  - hang → watchdog kill
  - 401 → terminal
  - 400 → terminal
  - legacy runtime (no `--output`)
  - oversize and garbage stdout
  - parent death → child exits
  - stop during backoff
- connector-runtime and policy-catalog vendor `wire/*.json` with a pinned SHA-256 and assert that they produce and accept those exact shapes. A change to any fixture requires PRs in all three repos.
- These scenarios port Assistant `test/docker_entrypoint_heartbeat.test.ts`. That is the parity gate before H1 removes the shell supervisor.

### 8.3 Local integration tests (no live control plane, no providers, no real credentials)

Add a `docker compose --profile runtime-link` stack with:

- the pinned real `kei-proxy` (v2);
- the real `kei-policy-catalog` with Postgres, seeded with a test installation and a throwaway token;
- one SDK smoke process per language.

Scenarios:

1. `connected` in the catalog within `2I` (run with `I=15s`).
2. Stop the catalog: SDK goes `degraded` and the harness stays ready. After the catalog restarts, the outage guard holds back any `offline` transitions and per-installation alerts, and installations return to `connected` within `2I`.
3. SIGKILL the SDK process: the child exits on stdin EOF, and the catalog goes `stale` then `offline`. Nothing keeps reporting after the process is gone.
4. Revoke the installation: `terminal:installation_unauthorized` and catalog `suspended`.
5. Throttle the catalog: `catalog_backpressure`, and the interval stretches.
6. Captured traffic, logs, and audit rows are grepped for the token and `KEI_TEST_LEAK_CANARY` values, and must contain neither.

### 8.4 Post-merge production canary (per harness, after each harness deploy)

- A dedicated **canary installation** per harness in a non-customer Haikei workspace. It runs heartbeat only; no tool calls reach providers.
- Checks, read-only against the catalog installation API and metrics:
  - `liveness.state=connected` within `2I+15s` of rollout;
  - `harness_kind` and `sdk_version` are as expected;
  - `kei_catalog_runtime_heartbeats_total{result!="accepted"}` does not increase for that installation;
  - no restarts beyond the one caused by the deploy;
  - a log query for the token prefix and leak canary returns zero hits.
- Restart the canary pod once. It must show `stale` at most and never `offline`, and return to `connected`.
- Alert rules are validated with `promtool test rules` in CI before the canary.
- **Rollback trigger:** the canary is not `connected` within `5I`, or any secret-scan hit. The fix is to roll back the harness image. The SDK itself is a library, so there is no separate flag to flip.

## 9. Phased PR stack

Decisions come first. Each code PR is independently reviewable and links HAI-141.

| # | Repo / owner | Content | Depends on |
| --- | --- | --- | --- |
| **D0** | Product/infra owner | Bootstrap failure fatal vs. non-fatal. Proposed default: **non-fatal listener, tools deny until bootstrap + bundle** (Assistant semantics), with `require_bootstrap` as an opt-in. | — |
| **D1** | Chat owner | Retire the PR #147 `RuntimeHeartbeatClient` (don't wire it), and move `headless_main.py` `/api/v1/runtime/*` routes under `/diag/`. | — |
| **S0** | Agentware | This spec. | — |
| **A1** | Agentware | Manifest guard fix (G11). Deprecate `LocalProxyProcess`/`run_proxy` with a warning; remove them one minor later. `CallerContext` parity in Go and TS (G10). | S0 |
| **CR1** | kei-connector-runtime | Heartbeat v2: `--output jsonl`, `--parent-stdin`, exit codes, in-tick retry without exiting, `Retry-After`, jitter, envelope env passthrough, v2 body, `run_id`, audit `installation_id`/`run_id` stamping. Flags default off, so it is backward compatible. | S0 |
| **PC1** | kei-policy-catalog | Accept v2 (columns: `last_interval_seconds`, `harness_kind`, `harness_version`, `sdk_lang`, `sdk_version`, `last_run_id`, `last_seq`, `audit_pending_records`), per-token 429, strict body validation, v1 compatibility. | S0 (parallel with CR1) |
| **PC2** | kei-policy-catalog | Freshness derivation and API field, sweeper, `observation_ok` guard, transition audit events, metrics. | PC1 |
| **PC3** | kei-policy-catalog / infra | Alert rules and dashboards (§7) with `promtool` tests. | PC2 |
| **A2** | Agentware (Go, reference) | `go/kei/runtimelink` and the shared contract fixtures (§8.2). | A1, CR1 wire fixtures |
| **A3** | Agentware (Python) | `kei.runtime_link` mirror. | A2 |
| **A4** | Agentware (TypeScript) | `src/kei/runtimeLink.ts` mirror, plus root-barrel export and publish. | A2 |
| **A5** | Agentware | `docker compose --profile runtime-link` integration suite (§8.3). | A2–A4, CR1, PC2 |
| **H1** | Assistant (DVL-Group) | Adopt the A4 `RuntimeLink`. Keep the shell supervisor behind `KEI_HEARTBEAT_LEGACY_SUPERVISOR=true` for one release, then delete it. The parity gate is the §8.2 scenarios ported from `docker_entrypoint_heartbeat.test.ts`. | A4, D0 |
| **H2** | PDE-Harness | Adopt A4 and add bootstrap and heartbeat. Replace `KEI_PROXY_COMMAND`/`JsonLinesKeiProxy` with the SDK transport (tracked under HAI-141 transport work). **Coordinate with the in-flight uncommitted work first.** | A4, D0 |
| **H3** | Kei-Chat-Harness | Adopt A3 in `main.py` and `teams_main.py` through one shared call. Delete the #147 client. Add a helm sidecar-free config: the SDK runs in-process and supervises the child. | A3, D0, D1 |
| **R1** | Agentware + connector-runtime | Remove legacy mode (no `--output`) once every harness pins runtime ≥ CR1. | H1–H3 in prod |

The release order is CR1 ∥ PC1 → PC2 → A2 → A3/A4 → H1/H2/H3 → PC3 enables paging. Alerts stay ticket-only until all three harnesses report v2.

## 10. Migration and compatibility (legacy `kei-proxy` naming)

- **Binary and distribution name stays `kei-proxy`.** Per the wiki note on kei PR #480, renaming the distribution breaks CI and infra. The **repository** is `kei-connector-runtime`, and new SDK APIs use "runtime" (`RuntimeLink`, `runtime_link`). "kei-proxy" appears only as the default binary name and in the existing env var names.
- **Env var names:** `KEI_PROXY_PATH`, `KEI_PROXY_SHA`, and `KEI_PROXY_*` lineage vars keep their names; they are the runtime's contract. No renames in this stack. Any future rename needs a dual-read period in the SDK and the runtime, and its own decision capture.
- **Token:** only `KEI_RUNTIME_TOKEN`. `KEI_HARNESS_TOKEN` alone is refused. Both names are rejected in manifests (A1).
- **Wire:** catalog v1 bodies are accepted indefinitely. Runtime v2 flags are opt-in, so older SDKs and the Assistant shell keep working. The SDK's legacy mode covers older runtimes until R1.
- **Code:** Python `LocalProxyProcess`, `run_proxy`, and `stop_proxy` go deprecated in A1 and are removed in the next minor. PDE `JsonLinesKeiProxy` and Chat `KeiProxyClient` spawn code are removed in H2/H3. The Chat #147 client is removed in H3.
- **Harness deploy config:** the Assistant's existing `KEI_HEARTBEAT_*` variables are the canonical schema, so H1 needs no env changes. Chat helm `runtime.*` values map onto the same names in H3.

## 11. Acceptance criteria

### 11.1 Common to all three installed harnesses (Assistant, PDE, Chat)

1. The harness starts heartbeat **only** through Agentware `RuntimeLink`. `git grep` in the harness finds none of the following:
   - `/api/v1/runtime/heartbeat`
   - a heartbeat loop or supervisor
   - a spawn of `runtime heartbeat` outside the SDK
2. The catalog shows `liveness.state=connected` with the correct `harness_kind`, `sdk_version`, and `runtime_version` within `2I+15s` of start, in local integration and in the production canary.
3. Killing the harness process (SIGKILL) leaves no orphan `kei-proxy` child. The catalog reaches `stale` by `2I+15s` and `offline` by `5I`.
4. A catalog outage puts the SDK in `degraded`. The harness stays ready and does not crash-loop. It recovers to `connected` on its own. FleetLivenessDrop fires and InstallationOffline does not.
5. A revoked installation reaches `terminal:installation_unauthorized` within one interval, RuntimeLinkUnauthorized fires, and there is at most one retry per 300s.
6. The child env is exactly the §4.1 allowlist, verified by a test. Harness secrets never reach the child.
7. Logs, metrics, lifecycle audit, catalog rows, and captured traffic contain no token, provider payload or result, customer content, or plaintext subject. This is verified by the canary-string scan.
8. The §7.1 metrics are exported, and `/ready` doesn't depend on link state. Bootstrap behavior matches D0, and tool calls deny until bootstrap and a valid bundle are in place.

### 11.2 Per harness

- **Assistant:** the §8.2 parity scenarios pass. `deployment/docker-entrypoint.sh` no longer supervises heartbeat (after one release behind the legacy flag). The collector is supervised by the SDK or stays a documented sidecar; one choice, documented.
- **PDE:** bootstrap and heartbeat are in-process, not a manual README step. `JsonLinesKeiProxy` and `KEI_PROXY_COMMAND` are gone. The canary installation is connected.
- **Chat (Discord, plus Slack/Teams entry points):** one shared lifecycle call serves `main.py` and `teams_main.py`. The PR #147 client is deleted. `headless_main.py` serves no `/api/v1/runtime/*` routes. Helm has no heartbeat sidecar and no second heartbeat path.

## 12. Non-goals and invariants

- No credentials, provider payloads/results, customer content, embeddings, or indexes cross into the catalog through heartbeat, liveness, or lifecycle audit.
- Local execution without Kei remains valid (`disabled` state, no ABAC/Kei dependency).
- The SDK never resolves credentials, never falls back to local permit, and never promotes declared config to identity.
- ABAC/catalog decides metadata policy only. Heartbeat state never changes authorization outcomes.
