# Python Middleware Usage Examples

This document provides examples of how to use the Python middleware
(`haikeilabs.agentware`) for policy enforcement and audit logging. It mirrors the Go
reference implementation in `go/middleware/`.

## Installation

```bash
pip install haikeilabs-agentware   # the distribution; the import is haikeilabs.agentware
# or, from a checkout:
cd python && pip install -e ".[dev]"
```

The distribution is published to PyPI as `haikeilabs-agentware` (like the npm
package `@haikeilabs/agentware`); the import package is `haikeilabs.agentware`
(`from haikeilabs.agentware import ...`). The old `pedro_agentware` name is a
deprecated alias that resolves to the same modules. See
[PyPI distribution strategy](pypi-distribution-strategy.md) for the artifact
contract and the release process.

## Governed harness quick start: socket client first

Start with the [Python getting started guide](../getting-started/python.md)
and the [`examples/python/minimal-harness`](../../examples/python/minimal-harness)
example. It demonstrates an allow and deny through the `kei-proxy` socket.
The socket client is recommended. `KeiProxyAuthorizeClient`, which starts
`kei-proxy authorize` as a subprocess, is deprecated; use
`KeiProxySocketAuthorizeClient`.

## Basic Usage

### Creating a Policy

```python
from haikeilabs.agentware.middleware import Action, Condition, Operator, Policy, Rule

policy = Policy(
    default_deny=False,
    rules=[
        Rule(
            name="rate-limit-tools",
            tools=["*"],
            action=Action.ALLOW,
        ),
        Rule(
            name="deny-admin",
            tools=["delete_database", "drop_table"],
            action=Action.DENY,
            conditions=[
                Condition(field="caller.trusted", operator=Operator.EQ, value="false"),
            ],
        ),
    ],
)
```

### Creating Middleware

```python
from haikeilabs.agentware.middleware import CallerContext, MiddlewareImpl

class MyToolExecutor:
    def execute(self, tool_name: str, args: dict) -> tuple:
        # Your tool execution logic here
        return ({"output": f"Executed {tool_name}"}, True, "")

# Create middleware
mw = MiddlewareImpl(MyToolExecutor())

# Call a tool through middleware
result, success, error = mw.execute("read_file", {"path": "/tmp/test.txt"}, CallerContext())
```

### Using Caller Context and Delegation

```python
from haikeilabs.agentware.middleware import CallerContext

# At a human entry point the user IS the invoking subject. Trusted defaults to
# False (fail-closed); it must be set explicitly to be true.
caller = CallerContext(
    trusted=True,
    role="user",
    user_id="user-123",
    session_id="session-456",
    source="cli",
    invoking_subject="user-123",
)

# When the agent spawns a subagent, delegate: the invoking subject is carried
# unchanged and the depth increments, so the audit trail resolves back to the
# human who authorized the request.
subagent = caller.delegate(span="subagent-1")
assert subagent.invoking_subject == "user-123"
assert subagent.delegation_depth == 1
assert subagent.parent_span == "subagent-1"
```

### Audited Tool Client

```python
from haikeilabs.agentware.middleware import AuditedToolClient

async def echo(**kwargs):
    return {"echo": kwargs.get("value", "")}

client = AuditedToolClient(source="my-agent", evaluator=policy_evaluator)

result = await client.Execute(
    tool_name="echo",
    tool_args={"value": "hi"},
    user_id="user-123",
    channel_id="session-456",
    guild_id=None,
    func=echo,
    caller=caller,  # CallerContext; omitted builds a fail-closed untrusted one
)
```

### Using Audit

```python
from haikeilabs.agentware.middleware import AuditFilter, InMemoryAuditor

auditor = InMemoryAuditor()
client = AuditedToolClient(source="my-agent", auditor=auditor)

# After tool calls, query the audit log. Records carry the delegation chain:
# invoking_subject, parent_span, delegation_depth, framework.
records = auditor.query(AuditFilter(invoking_subject="user-123"))
for entry in records:
    print(f"Decision: {entry.decision.action.value}, Tool: {entry.tool_name}")
```

## Condition Operators

| Operator | Description |
| --- | --- |
| `eq` | Field equals value |
| `not_eq` | Field does not equal value |
| `contains` | Field contains value |
| `not_contains` | Field does not contain value |
| `matches` | Field matches regex pattern |
| `not_matches` | Field does not match regex pattern |
| `exists` | Field exists |
| `not_exists` | Field does not exist |

## Field Resolution

Conditions can reference:

- `caller.role` - Caller's role
- `caller.user_id` - User ID
- `caller.session_id` - Session ID
- `caller.source` - Call source
- `caller.trusted` - Whether caller is trusted
- `args.<name>` - Tool argument values

## Harness Contract

Third-party agent harnesses build against `haikeilabs.agentware` through the
`kei/` module — see `docs/harness-contract.md` and
`python/tests/third_party_harness_test.py` for a complete example that imports
nothing outside this library.

## Deprecated: subprocess policy evaluator

`KeiProxyEvaluator` with `KeiProxyAuthorizeClient` asks `kei-proxy authorize`
before every tool call and fails closed on anything that is not an explicit
`allow`/`permit`. `KeiProxyAuthorizeClient` runs the binary; `KEI_RUNTIME_TOKEN`
reaches it only through its (allowlisted) environment, never argv. The child
gets no `PATH` or `HOME`, so the executable is resolved to an absolute path in
the parent. It runs in its own session, and a timeout SIGKILLs the whole
process group. Pass `expected_sha256=` to pin the binary: it is re-hashed from
an `O_NOFOLLOW` descriptor before every spawn, and any drift denies with
`kei-proxy pin_mismatch`.

```python
from haikeilabs.agentware.kei import KeiProxyAuthorizeClient, KeiProxyEvaluator
from haikeilabs.agentware.middleware import AuditedToolClient

evaluator = KeiProxyEvaluator(KeiProxyAuthorizeClient(executable="kei-proxy", timeout=10.0))
client = AuditedToolClient(evaluator=evaluator)

decision = evaluator.evaluate("github.get_issue", {"owner": "acme", "repo": "pipe"}, caller)
if decision.enrollment:  # deny + enrollment claim link: show it to the user, never log it
    harness_dm(caller, decision.enrollment.get("url"))  # your harness's private reply
```

Reasons read `kei-proxy <class>[: <detail>]`, where the class is one of
`REASON_CLASSES`. The behaviour is shared with the TypeScript and Go ports —
see `docs/kei-proxy-evaluator-parity.md`.

### Recommended: over the kei-proxy daemon socket

When `kei-proxy serve` runs beside the harness, talk to it over its Unix
socket (`KEI_RUNTIME_SOCKET_PATH`, default `/run/kei-proxy/runtime.sock`)
instead of spawning a process per call. `KeiProxySocketAuthorizeClient` is the
async client, named and behaving like the TypeScript SDK's: it opens a
`kei.session/v2` session per `(source, external_id)`, and authorizes, lists
models and runs chat completions inside it. The request never names the user;
the daemon decides for the session's subject. An unreachable daemon, a non-2xx
answer or a body that is not the expected JSON raises `KeiProxySocketError`,
a refusal. A `session_not_found` session is reopened once.

```python
from haikeilabs.agentware.kei import (
    KeiProxyEvaluator,
    KeiProxySocketAuthorizeClient,
    KeiProxySocketIdentity,
    select_authorization_client,
)

runtime = KeiProxySocketAuthorizeClient()
session = await runtime.open_session(KeiProxySocketIdentity("openwebui", user_id))
answer = await runtime.authorize(session, tool="github.get_issue", action="execute")
reply = await runtime.chat_completion(session, {"model": "gpt-4.1", "messages": messages})
async for line in runtime.stream_chat_completion(session, body):  # SSE "data:" lines
    ...
await runtime.close_session(session)

# The evaluator uses the socket when one is configured, else `kei-proxy authorize`.
evaluator = KeiProxyEvaluator(select_authorization_client("openwebui"))
```

Register the harness's tools before serving with `sync_tool_manifest`: it
`PUT`s the tool manifest v2 to `$KEI_RUNTIME_CONTROL_PLANE_URL/api/v1/runtime/tools`
with `KEI_RUNTIME_TOKEN`, replacing the installation's tools, so it refuses to
send an empty manifest. It honours `KEI_PROXY_EGRESS_PROXY` and
`KEI_PROXY_NO_PROXY`.

```python
from haikeilabs.agentware.kei import ToolManifestSyncConfig, sync_tool_manifest

sync_tool_manifest(manifest, ToolManifestSyncConfig.from_env(), expect_tools=bool(packs))
```

For the fail-closed design principles — the decision table,
`KEI_PROXY_DISABLED` as fail-closed (never an allow switch), governed vs
non-governed tool calls, and testing patterns — see `docs/fail-closed-design.md`.

## API Reference

### Core Classes

- `MiddlewareImpl` / `Middleware` - Main middleware class for policy enforcement
- `AuditedToolClient` - Small surface: a tool function in, a result out, audited either way
- `Policy` - Policy container with rules
- `Rule` - Individual policy rule
- `CallerContext` - Context about the caller (with delegation fields and `delegate()`)
- `Decision` - Policy decision result
- `Condition` / `Operator` - Rule conditions and operators

### Auditors

- `InMemoryAuditor` - Stores audit records in memory
- `AuditRecord` - One record per tool call, carrying the delegation chain
- `AuditFilter` - Query filter over `session_id`, `parent_span`, `invoking_subject`, `tool_name`, `action`, `since`, `limit`
