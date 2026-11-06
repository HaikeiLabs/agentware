# Agentware

The open-source SDK we build our own agents with, from [Haikei Labs](https://haikeilabs.com).

Build a custom AI agent or harness that calls tools, tracks decisions, and applies
guardrails. Agentware provides Go, Python, and TypeScript libraries for the
parts around your model: tool execution, middleware, audit, and agent-loop
helpers. Start with a local echo tool, then adapt it to your own application.

## Start building

Install the canonical [Haikei Skills](https://github.com/HaikeiLabs/skills)
to give your coding assistant Agentware guidance (requires Node.js 22.20+ and npx):

```sh
npx skills add HaikeiLabs/skills --skill haikei --skill agentware-sdk -a codex -y
```

For Claude Code, Cursor, OpenCode, or Pi, replace `codex` with `claude-code`,
`cursor`, `opencode`, or `pi`. Skills provide instructions; they do not install
the SDK, configure Kei, or grant permissions.

Install the SDK for your language, then try the local example below.

```sh
pip install haikeilabs-agentware     # Python 3.10+
npm install @haikeilabs/agentware    # TypeScript
go get github.com/haikeilabs/agentware/go
```

Save this as `echo.py` and run `python echo.py`:

```python
import asyncio
from haikeilabs.agentware.middleware import AuditedToolClient, CallerContext

def echo(**kwargs):
    return {"echo": kwargs["value"]}

async def main():
    client = AuditedToolClient(source="my-agent")
    caller = CallerContext(user_id="user-123", invoking_subject="user-123", source="my-agent")
    result = await client.Execute("echo", {"value": "hi"}, "user-123", "session-1", None, echo, caller=caller)
    print(result)

asyncio.run(main())
```

The output is `{'echo': 'hi'}` and the default in-memory auditor records the
call. **Without an evaluator, this client allows calls**; configure policy for
any operation that needs authorization. Kei is optional for this local example.
For a fake-proxy allow/deny example, see the [Python](docs/getting-started/python.md),
[TypeScript](docs/getting-started/typescript.md), and [Go](docs/getting-started/go.md)
guides. The [Rust workspace](rust/README.md) is a port in progress, not a
complete SDK.

Next, build an agent where a model calls a tool: the
[TypeScript first agent](examples/typescript/first-agent/README.md) summarizes
the top Hacker News stories with any OpenAI-compatible model, audits each tool
call, and denies a second tool with a local policy. No Kei needed.

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
Configure an evaluator and validate caller identity at the trusted entry point.
`KeiProxyEvaluator` fails closed when authorization cannot explicitly allow a call.
The Python `AuditedToolClient` allows calls when no evaluator is configured and
can construct a caller from supplied IDs, so it does not authenticate those IDs.

**Audit records.** Auditors record tool decisions and caller context; exact fields
and outcome visibility vary by port and integration. The Python in-memory auditor
stores the supplied arguments, so do not pass secrets or sensitive data without
appropriate sanitization. It is local and non-persistent by default. With
`KeiProxyEvaluator`, kei-proxy separately records authorization decisions;
see [docs/audit-visibility.md](docs/audit-visibility.md).

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
define what a harness must implement to get Kei policies through Agentware without depending
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
external operations that Kei allows or denies go through the proxy boundary. See
`docs/tenant-proxy-reference.md` for the authoritative architecture.

## Install

Each SDK is published to its language's public registry under the same
version:

```bash
# Python 3.10+ (import haikeilabs.agentware)
pip install haikeilabs-agentware

# TypeScript / Node 18+ (the examples need 22.18+)
npm i @haikeilabs/agentware

# Go 1.27+ for this checkout (module path is lowercase)
go get github.com/haikeilabs/agentware/go
```

To work on the SDKs themselves, see [Development](#development).

## Add Kei policies

Kei policies are deterministic guardrails for the tool calls your agent can make. Agentware runs on its own. When an agent needs permissions that someone else
manages, connect it to Kei: `KeiProxyEvaluator` asks the Kei runtime
(`kei-proxy`) to allow or deny each tool call, fails closed, and records the
decision. In Kei's harness types this is the **SDK (Agentware)** type
([docs/harness-contract.md](docs/harness-contract.md)).

### Quick starts with the Kei runtime

To have Kei allow or deny tool calls, connect to the `kei-proxy` Unix socket
first. Each language guide uses the minimal harness example and demonstrates an
allow and a deny against the local fake socket:

- [Go getting started](docs/getting-started/go.md)
- [Python getting started](docs/getting-started/python.md)
- [TypeScript getting started](docs/getting-started/typescript.md)
- [Agentware 60-minute cold-start dry run](docs/dry-run/60-minute-dry-run.md)
- [Using agentware as a team](docs/using-agentware-as-a-team.md)

The in-repo guides above describe the same path as the
[Agentware docs site](https://docs.haikeilabs.com/agentware/build/overview) —
the repo docs are the checked source, but both reflect the same quick-start
flow. The site also hosts language-specific pages for
[Go](https://docs.haikeilabs.com/agentware/build/go),
[Python](https://docs.haikeilabs.com/agentware/build/python), and
[TypeScript](https://docs.haikeilabs.com/agentware/build/typescript), plus
guides on [runtime setup](https://docs.haikeilabs.com/agentware/build/runtime-setup),
[policy and audit](https://docs.haikeilabs.com/agentware/build/policy-and-audit),
and the [deny-hints reference](https://docs.haikeilabs.com/agentware/reference/deny-hints).

The socket client is recommended. The older `KeiProxyAuthorizeClient`
subprocess integration (`kei-proxy authorize`) is deprecated.

## Compatibility

Protected tool calls go through `kei-proxy`, the runtime the harness calls, which
an org admin installs with the `kei` platform admin CLI (kei-cli). The
kei-cli installer bundles the pinned kei-proxy. Use at least these versions
with each SDK release:

<!-- compat:begin -->
| agentware SDK | kei-proxy (minimum) | kei-cli (minimum) |
| --- | --- | --- |
| 0.16.0 | v0.6.5 | 0.3.6 |
| 0.15.0 | v0.6.5 | 0.3.6 |
| 0.14.0 | v0.3.0 | 0.2.0 |
| 0.13.0 | v0.3.0 | 0.2.0 |
| 0.12.0 | v0.3.0 | 0.2.0 |
| 0.11.0 | v0.3.0 | 0.2.0 |
| 0.10.0 | v0.2.13 | 0.1.33 |
| 0.9.0 | v0.2.13 | 0.1.33 |
<!-- compat:end -->

`KEI_PROXY_VERSION` pins the kei-proxy entry for the current release, and CI
(`make compat-check`) fails if the table disagrees with it or has no row for
the version in `VERSION`. From Agentware 0.15.0, exact connector-target
authorization requires kei-proxy v0.6.5 or later; kei-cli v0.3.6 or later bundles that proxy
release. Earlier feature floors still apply to older proxies:
runtime agent identity needs kei-proxy 0.1.11 or later, and the runtime-link
heartbeat flags need 0.1.12 or later. kei-proxy releases older than the
minimum above are not tested against this SDK release. ToolSpec registration
over the socket requires kei-proxy v0.3.0 or later.

## Quick start (Python)

```python
from haikeilabs.agentware.middleware import AuditedToolClient, CallerContext

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

This table is the in-process record. Kei receives only kei-proxy's decision
record; see [docs/audit-visibility.md](docs/audit-visibility.md).

## Layout

| Path | Contents |
| --- | --- |
| `go/middleware/` | Policy engine, audit records, interception |
| `go/adapters/` | Framework adapters (ADK, hermes) |
| `go/memory/` | Wiki memory — see `SCHEMA.md` for the page contract |
| `go/mcp/`, `go/llm/`, `go/executor/` | MCP server, model clients, execution |
| `python/` | Python port (`haikeilabs.agentware`), including the `kei/` harness surface |
| `typescript/` | TypeScript port |
| `docs/{go,python,typescript}/` | Usage examples per language |
| `docs/engineering-design.md` | Architecture |
| `docs/tenant-proxy-reference.md` | Canonical tenant-side proxy architecture and metadata-only control-plane boundary |
| `docs/harness-contract.md` | Contract for third-party agent builders |
| `docs/audit-visibility.md` | What is audited locally vs in Kei, and how to see one protected call |
| `docs/runtime-link-integration.md` | RuntimeLink public API and usage boundaries for harness authors |

## Examples

- [TypeScript first agent](examples/typescript/first-agent/README.md): a model
  calls a tool, with local policy and audit, no Kei
- [Go minimal harness](examples/go/minimal-harness/README.md)
- [Python minimal harness](examples/python/minimal-harness/README.md)
- [TypeScript minimal harness](examples/typescript/minimal-harness/README.md)

To run an example with Kei policies, set it up in this order:

1. `kei bot init`
2. `kei harness add --kind custom`
3. Register tools with `kei harness sync --harness custom --manifest tools.json`
4. Start the runtime with `kei-proxy serve`
5. Call the harness.

## Development

```bash
# Go
cd go && go build ./... && go test ./...

# Python
cd python && pip install -e ".[dev]" && pytest && ruff check . && mypy src/

# TypeScript
cd typescript && npm ci && npm run build && npm test

# Shared version and compatibility table
make version-test version-check compat-check

# Docs (markdown lint; rules in .markdownlint.json)
npx markdownlint-cli2 "docs/**/*.md" README.md
```

The ontology T-box is a git submodule: run `git submodule update --init` after
cloning. It is read-only — missing terms go in `SCHEMA_GAPS.md`, never invented
locally.

## Community

- [Docs](https://docs.haikeilabs.com/agentware/build/overview)
- [Discord](https://discord.gg/g6QaTYWXDa), where contribution work is coordinated
- [Sovereign Context on Substack](https://haikeilabs.substack.com)

## License

Apache-2.0. See [LICENSE](LICENSE).
