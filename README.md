# pedro-agentware

Policy enforcement and audit middleware for agent tool calls, in Go, Python, and
TypeScript.

Every tool an agent invokes passes through one interception point: policy decides
whether the call proceeds, and an audit record is emitted whether it does or not.
The record shape does not change when you swap agent frameworks or models — which
is the point.

## Why

Attribution dies at the first delegation hop. An agent spawns a subagent, the
subagent runs as a service account, and every log downstream says "the agent did
it." Nobody can answer who authorized an action, what data it touched, or what it
cost.

This library keeps the invoking human subject attached through every hop, records
the resources each call touched, and attributes token cost back up the delegation
chain.

## What it does

**Policy enforcement.** Declarative rules — allow, deny, filter — with rate
limits, turn caps, and conditions over caller attributes and tool arguments.
Fail-closed by default: a call with no caller context is denied, not trusted.

**Audit records.** One append-only record per tool call, carrying the invoking
subject, the delegation chain (`parent_span`, `delegation_depth`), the originating
framework, a SHA-256 digest of the arguments rather than the arguments themselves,
the resources touched, the policy decision and the rule that made it, token counts,
and latency. Metrics are rollups over these records; there is no second
instrumentation path.

**Framework adapters.** The same tool definition renders to a valid call format for
each target framework, and produces the same audit row shape under each. Adapters
live in `go/adapters/` — currently ADK and hermes.

**Wiki memory.** An LLM-maintained, ontology-constrained markdown wiki scoped per
user (`go/memory/`). Memory writes go through the same middleware chain as any
other tool call — declarative policy plus a semantic tier that validates page
frontmatter and typed links against a read-only ontology. Each vault resolves from
the caller context, and cross-user access is rejected by policy rule *and* by a
path-boundary check in the executor. Memory is not a side channel around the audit
trail; it is another audited tool call.

**Third-party harness contract.** The `kei/` module and `docs/harness-contract.md`
define what a harness must implement to be governed by agentware without depending
on any agent framework. Enforcement is shared library code: `KeiProxyEvaluator`
fails closed on every path that is not an explicit `permit`/`allow`.

**Tenant-side distributed proxy.** Agentware is the tenant-side execution layer:
local tool middleware, delegation, semantic tool bindings, and proxy integration.
Kei is a **metadata-only control plane** — it holds registration, auth
*references*, tool bindings (`tool_bindings`), non-secret `secret_refs`, scopes,
and audit metadata. The distributed proxy is the **connector/provider runtime and
policy enforcement point**; ABAC is called for **metadata policy decisions only**
and never executes writes or serves/store credentials. Provider payloads and
results, customer content, credentials, embeddings, and indexes **never enter
Kei**. Local agent execution works without a direct ABAC connector dependency;
governed external operations go through the proxy boundary. See
`docs/tenant-proxy-reference.md` for the authoritative architecture.

## Install

```bash
# Python (src-layout package pedro_agentware)
pip install -e ./python

# Go
go get github.com/soypete/pedro-agentware/go
```

TypeScript lives in `typescript/`.

## Quick start (Python)

```python
from pedro_agentware.middleware import AuditedToolClient, CallerContext

def echo(**kwargs):
    return {"echo": kwargs.get("value", "")}

client = AuditedToolClient(source="my-agent")

# At a human entry point the user IS the invoking subject; it survives every
# subsequent delegation hop.
caller = CallerContext(user_id="user-123", invoking_subject="user-123", source="my-agent")

result = await client.Execute("echo", {"value": "hi"}, "user-123", "session-1", None, echo, caller=caller)
# result == {"echo": "hi"}; an audit record was emitted for the call.
```

Delegation: when the agent spawns a subagent, hand the child its own context
without losing the human:

```python
child = caller.delegate(span="subagent-1")
# child.invoking_subject is still "user-123"; child.delegation_depth == 1
```

## Policy

Rules are YAML or constructed in code:

```yaml
rules:
  - name: "rate-limit-read"
    tools: ["read_file", "search"]
    action: "allow"
    max_rate:
      count: 5
      window: 60
    conditions:
      - field: "caller.role"
        operator: "eq"
        value: "user"

  - name: "deny-admin-tools"
    tools: ["delete_database", "drop_table"]
    action: "deny"
    conditions:
      - field: "caller.trusted"
        operator: "eq"
        value: "false"

  - name: "filter-sensitive"
    tools: ["get_user"]
    action: "filter"
    redact_fields: ["password", "ssn"]

default_deny: false
```

**Operators**: `eq`, `not_eq`, `contains`, `not_contains`, `matches`,
`not_matches`, `exists`, `not_exists`, `not`.

**Fields**: `caller.role`, `caller.user_id`, `caller.session_id`, `caller.source`,
`context.trusted`, `args.<name>`, `context.<key>`.

## Audit record

| Field | Notes |
| --- | --- |
| `invoking_subject` | The human who initiated the task. Survives every delegation hop. |
| `parent_span`, `delegation_depth` | Reconstructs the full call tree |
| `agent_id`, `agent_version` | |
| `framework` | Which adapter originated the call — proves cross-framework uniformity |
| `tool_name`, `tool_args_digest` | Digest, not raw args: avoids logging PII into the audit store |
| `resources_touched[]` | Resource identifiers — table, bucket, endpoint — not tool names |
| `decision`, `policy_id` | The outcome and the rule that decided it |
| `model`, `tokens_in`, `tokens_out`, `cached_tokens` | Feeds cost attribution |
| `latency_ms`, `error`, `retry_count`, `success` | Feeds reliability metrics |

`resources_touched` is what makes "show every agent invocation that read table X
on behalf of user Y" answerable. Tool-name logging is what everyone does;
resource-level lineage is what makes the question tractable.

## Layout

| Path | Contents |
| --- | --- |
| `go/middleware/` | Policy engine, audit records, interception |
| `go/adapters/` | Framework adapters (ADK, hermes) |
| `go/memory/` | Wiki memory — see `SCHEMA.md` for the page contract |
| `go/mcp/`, `go/llm/`, `go/executor/` | MCP server, model clients, execution |
| `python/` | Python port (`pedro_agentware`), including the `kei/` harness surface |
| `typescript/` | TypeScript port |
| `docs/{go,python,typescript}/` | Usage examples per language |
| `docs/engineering-design.md` | Architecture |
| `docs/tenant-proxy-reference.md` | Canonical tenant-side proxy architecture and metadata-only control-plane boundary |
| `docs/harness-contract.md` | Contract for third-party agent builders |
| `docs/runtime-link-integration.md` | RuntimeLink public API and usage boundaries for harness authors |
| `docs/build-history/` | Archived build-loop working files |

## Development

```bash
# Go
cd go && go build ./... && go test ./...

# Python
cd python && pip install -e ".[dev]" && pytest && ruff check . && mypy src/

# Docs (markdown lint; rules in .markdownlint.json; build-history is archived)
npx markdownlint-cli2 "docs/**/*.md" README.md "!docs/build-history/**"
```

The ontology T-box is a git submodule: run `git submodule update --init` after
cloning. It is read-only — missing terms go in `SCHEMA_GAPS.md`, never invented
locally.

## License

MIT
