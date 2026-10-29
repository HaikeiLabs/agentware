# Seeing governed tool calls in Kei

This page explains which audit records Agentware keeps locally, which record
Kei receives, and how to follow one governed tool call from your harness to
the Kei console.

## What is recorded where

| Where | What | Fields | Leaves the process? |
| --- | --- | --- | --- |
| Agentware `Auditor` | One record per tool call, allowed or denied | Invoking subject, delegation chain, framework, tool name, args digest, decision, policy ID, tokens, latency, success, error | **No.** The default `Auditor` is `InMemoryAuditor` in every language. Records live in process memory and are lost when the process exits. `AuditHook` callbacks see the same records; what they do with them is up to you. |
| kei-proxy spool | One **decision** record per authorize request | `span_id`, invoking subject, agent, framework, tool name, decision, reason code, policy, org, workspace, args digest | Yes, when a collector ships the spool to Kei. |
| Kei audit | The decision record the collector shipped | As above. Org and workspace come from the runtime installation's credential, not from the record | Shown in the console under **Audit Logs** (`/audit`). |

Two consequences:

- **Kei only sees the authorization decision.** Execution outcome fields
  (success, latency, tokens, error) stay in the Agentware `Auditor`. Kei has no
  route that accepts them.
- **Nothing reaches Kei unless the client has a `KeiProxyEvaluator`.**
  `AuditedToolClient` without a policy evaluator never calls kei-proxy. Go and
  Python allow the call with the reason `no policy configured`; TypeScript
  denies it. Either way, Kei never hears about it.

## Required setup

1. **Wire `KeiProxyEvaluator`** on the `AuditedToolClient` (or `Middleware`),
   with the client returned by the authorization-client selector:
   `runtimesocket.SelectAuthorizationClient` (Go),
   `select_authorization_client` (Python), `selectAuthorizationClient`
   (TypeScript).
2. **Pick the authorize path.** The selector decides once, at construction:

   | Path | Selected when | Writes the decision to the spool | Ships the spool |
   | --- | --- | --- | --- |
   | `kei-proxy serve` socket (**recommended**) | `KEI_RUNTIME_SOCKET_PATH` (or an explicit socket path) is set | Always | Yes: `serve` runs the collector in process |
   | One-shot `kei-proxy authorize` subprocess (deprecated fallback) | No socket path is configured | **Only if `KEI_PROXY_AUDIT` is set** in the harness environment | **No.** You must run a collector yourself |

   On the subprocess path the child inherits an allowlisted environment that
   includes `KEI_PROXY_AUDIT` and `KEI_PROXY_AUDIT_MAX_BYTES`. If the harness
   process does not set `KEI_PROXY_AUDIT`, decisions are enforced but never
   written.
3. **Run a collector** that can authenticate to Kei. `kei-proxy serve` and
   `kei-proxy runtime heartbeat` each start one. On the subprocess path without
   either, run `kei-proxy collector`. Run only one collector per spool. The
   collector needs `KEI_RUNTIME_TOKEN` and `KEI_RUNTIME_CONTROL_PLANE_URL`, and
   the runtime installation must be bootstrapped, active and verified. Without
   that, flushes fail and records stay in the spool.

The report-only `kei-proxy hook` covers the Claude Code, Codex, OpenCode and
Cursor harnesses only; it does not record SDK (Agentware) calls.

## Recipe: see one governed call

### 0. Wire the SDK

Go is shown; Python and TypeScript use the selector names above.

```go
import (
    "github.com/haikeilabs/agentware/go/kei/evaluator"
    "github.com/haikeilabs/agentware/go/kei/runtimesocket"
    "github.com/haikeilabs/agentware/go/middleware"
)

authz, err := runtimesocket.SelectAuthorizationClient("my-agent", runtimesocket.SelectOptions{})
if err != nil {
    return err // fail closed
}
client := middleware.NewAuditedToolClient("my-agent", exec).
    WithPolicy(evaluator.NewKeiProxyEvaluator(authz))
```

Set in the harness environment (from your secret manager, never in source):

- `KEI_RUNTIME_SOCKET_PATH` — the `kei-proxy serve` socket. Recommended.
- `KEI_RUNTIME_TOKEN` — the runtime installation credential, where your flow
  needs it.
- Only on the subprocess fallback: `KEI_PROXY_AUDIT=<spool file>`.

### 1. Run the daemon

```sh
kei-proxy serve
```

It needs `KEI_RUNTIME_TOKEN` and `KEI_RUNTIME_CONTROL_PLANE_URL`, and starts
the collector. See [runtime setup](getting-started/runtime-setup.md).

### 2. Make one call and find it in the local spool

The spool is `KEI_PROXY_AUDIT` if set, otherwise
`<state dir>/audit/harness-tool.jsonl`. The default state dir is
`~/Library/Application Support/kei-proxy` on macOS, and `/var/lib/kei-proxy`
(root) or `$XDG_STATE_HOME/kei-proxy` / `~/.local/state/kei-proxy` (user) on
Linux.

```sh
SPOOL="${KEI_PROXY_AUDIT:-$HOME/Library/Application Support/kei-proxy/audit/harness-tool.jsonl}"
ls -la "$(dirname "$SPOOL")"   # active file, sealed .NNNNNNNNNN segments, .state, .meta
tail -n 5 "$SPOOL" | jq '{span_id, tool_name, decision, reason_code, invoking_subject, agent_id, workspace_id}'
cat "$SPOOL.state" "$SPOOL.meta" 2>/dev/null   # collector cursor; evicted-record counts
```

Note the `span_id`. There is no `kei-proxy audit` subcommand; read the files
directly. No line for your call means the evaluator is not wired, or the
subprocess path is running without `KEI_PROXY_AUDIT`.

### 3. Ship it

- With `serve` running, wait about a minute (the collector polls every minute)
  and check its logs for collector errors.
- Without a daemon (subprocess path only):

  ```sh
  kei-proxy collector --jsonl "$SPOOL"          # once; add --poll to keep running
  ```

  Never run it next to a `serve` that owns the same spool.

Shipped segments are deleted and `.state` advances. A 401 means the token is
missing or invalid, or the installation is not active and verified. A batch
the control plane rejects is retried and can stall shipping for the records
behind it; see the kei-proxy operator runbook (forthcoming) for recovery. The
spool is capped (`KEI_PROXY_AUDIT_MAX_BYTES`, default 100 MiB); past the cap
the oldest segments are evicted whether shipped or not.

### 4. Query the catalog

As an org or workspace admin, through the console session:

```text
GET /api/v1/audit:query?org_id=<org>&workspace_id=<installation's workspace>&from=<RFC3339>&limit=50
```

Non-admins see only their own subjects. Operators with service access can look
a record up by span with `GET /api/v1/audit/records?org_id=<org>&span_id=<span_id>`.
If the record is here but not in the console, the cause is the workspace or a
filter (step 5).

### 5. Open the console

1. Open **Audit Logs** (`/audit`) and select the **runtime installation's
   workspace**. The record's workspace comes from the installation's
   credential, not from anything the SDK sends.
2. Set the Harness, Agent and Decision filters to **All**. These filters run in
   the browser after the fetch. An SDK decision row may not be classified as
   **SDK (Agentware)** and can show an unknown harness, so filtering on that
   harness can hide it.
3. Search for the tool name, or use the dashboard's recent-activity card.

Agents → Sessions lists Claude Code, Codex, OpenCode and Cursor sessions only.
An SDK harness with no sessions there is expected.

## Related

- [Harness contract](harness-contract.md)
- [Runtime setup](getting-started/runtime-setup.md)
- [Tenant-side proxy reference](tenant-proxy-reference.md)
- The kei-proxy README, "Local audit spool", for spool format and rotation.
