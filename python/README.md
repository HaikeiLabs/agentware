# haikeilabs-agentware

Policy enforcement and audit middleware for LLM agent tool calls.

`haikeilabs-agentware` sits between an agent (the LLM orchestrator) and tool
execution. Every tool call is checked against policy (allow, deny, or filter
with redacted arguments, rate limits, caller-context conditions) and every
decision is audited. It is the Python implementation of
[agentware](https://github.com/HaikeiLabs/agentware), which also ships Go and
TypeScript implementations with the same behaviour.

## Install

```bash
pip install haikeilabs-agentware
```

## Governed harness quick start: socket client first

For an executable allow/deny walkthrough through the `kei-proxy` socket, see
the [Python getting started guide](../docs/getting-started/python.md) and the
[`examples/python/minimal-harness`](../examples/python/minimal-harness)
example. `KeiProxySocketAuthorizeClient` is the recommended client;
`KeiProxyAuthorizeClient` (`kei-proxy authorize` subprocess) is deprecated.
The package install lines and supported runtime versions are listed in the
[repository README](../README.md).

```python
from haikeilabs.agentware.middleware import AuditedToolClient
import haikeilabs.agentware.kei
```

The distribution is `haikeilabs-agentware` (like the npm package
`@haikeilabs/agentware`); the import package is `haikeilabs.agentware`, under
the `haikeilabs` namespace. Python 3.10 or later.

## Quick start

```python
from haikeilabs.agentware.middleware import (
    Action,
    AuditedToolClient,
    CallerContext,
    Policy,
    Rule,
    SimplePolicyEvaluator,
)

policy = Policy(rules=[
    Rule(name="no-repo-deletes", tools=["github.delete_repo"], action=Action.DENY),
    Rule(name="default", tools=["*"], action=Action.ALLOW),
])
evaluator = SimplePolicyEvaluator(policy)

# The human who started the request. Trusted defaults to False (fail closed).
caller = CallerContext(user_id="ada", invoking_subject="ada")
decision = evaluator.evaluate("github.delete_repo", {"repo": "pipeline"}, caller)
assert decision.action is Action.DENY

# AuditedToolClient enforces the decision and records it, allow or deny.
client = AuditedToolClient(evaluator=evaluator)
```

When an agent delegates to a subagent, `CallerContext.delegate()` carries the
invoking subject unchanged and records the parent span and delegation depth,
so the audit trail always leads back to the human who authorized the request.

Audit records stay in process (`InMemoryAuditor`); Kei sees only kei-proxy's
decision record. See [Seeing governed tool calls in Kei](https://github.com/HaikeiLabs/agentware/blob/main/docs/audit-visibility.md).

## What's in the package

- `haikeilabs.agentware.middleware`: the policy evaluator, rules and conditions,
  rate limits, auditors, `AuditedToolClient`, and agent-loop guardrails.
- `haikeilabs.agentware.kei`: Kei integration. `KeiProxyEvaluator` is a policy
  evaluator backed by `kei-proxy` that fails closed on everything except an
  explicit allow. The Python-only `HarnessContract` helper and manifest
  loading also live here; that helper is not a cross-language SDK contract.
- `haikeilabs.agentware.toolformat`: tool-call formatting and parsing for open
  models without native tool-call APIs (Llama, Mistral, Qwen, Nemotron and
  others).
- `haikeilabs.agentware.llm`, `haikeilabs.agentware.llmcontext`: token counting,
  context-window accounting and conversation compaction.
- `haikeilabs.agentware.tools`, `executor`, `jobs`, `prompts`: the tool registry,
  dispatch, background jobs and system-prompt generation.

The Guardrails facade follows the Forge guardrails API design. Forge is MIT
licensed; attribution and the license text are in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

```python
from haikeilabs.agentware.middleware.guardrails import Guardrails

guardrails = Guardrails.from_config(config_document)
result = guardrails.check(response)
if result["outcome"] in {"retry", "step_blocked"}:
    messages.append({"role": "user", "content": result.get("nudge_text", "Try again")})
elif result["outcome"] == "fatal":
    raise RuntimeError("guardrail retry limit reached")
else:
    for call in result["tool_calls"]:
        policy.require_allowed(call["tool"], call["args"])
        tool_result = execute(call)
        if tool_result.is_error:
            recorded = guardrails.record({"tool": call["tool"], "category": "unknown"})
            if "nudge_text" in recorded:
                messages.append({"role": "user", "content": recorded["nudge_text"]})
        else:
            guardrails.record({"tool": call["tool"], "args": call["args"]})
```

## Migrating from `pedro_agentware`

The import package used to be `pedro_agentware`. It still works as a
deprecated alias: `import pedro_agentware.kei.evaluator` gives you the same
module object as `haikeilabs.agentware.kei.evaluator`, and emits a
`DeprecationWarning`. Replace `pedro_agentware` with `haikeilabs.agentware` in
your imports; the alias will be removed in a future release.

## Documentation

- [Python usage guide](https://github.com/HaikeiLabs/agentware/tree/main/docs/python)
- [Harness contract](https://github.com/HaikeiLabs/agentware/blob/main/docs/harness-contract.md)
- [Changelog](https://github.com/HaikeiLabs/agentware/blob/main/python/CHANGELOG.md)

## MCP route client

Use `haikeilabs.agentware.kei.mcproute.Client` to call a connector through the
local `kei-proxy` MCP route. It supports `tools/list` and `tools/call` over the
configured Unix socket or loopback TCP listener:

```python
from haikeilabs.agentware.kei.mcproute import Client

client = Client("github-connector")
tools = client.list_tools()
result = client.call_tool("get_issue", {"id": "42"})
```

`KEI_MCP_ROUTE_KEY` is read from the environment per request and is never
cached or logged. Non-loopback hosts are rejected; provider traffic stays in
the tenant runtime.

## License

Apache-2.0. See [LICENSE](https://github.com/HaikeiLabs/agentware/blob/main/LICENSE).
