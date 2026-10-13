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

## What's in the package

- `haikeilabs.agentware.middleware`: the policy evaluator, rules and conditions,
  rate limits, auditors, `AuditedToolClient`, and agent-loop guardrails.
- `haikeilabs.agentware.kei`: Kei integration. `KeiProxyEvaluator` is a policy
  evaluator backed by `kei-proxy` that fails closed on everything except an
  explicit allow. The harness contract and manifest loading also live here.
- `haikeilabs.agentware.toolformat`: tool-call formatting and parsing for open
  models without native tool-call APIs (Llama, Mistral, Qwen, Nemotron and
  others).
- `haikeilabs.agentware.llm`, `haikeilabs.agentware.llmcontext`: token counting,
  context-window accounting and conversation compaction.
- `haikeilabs.agentware.tools`, `executor`, `jobs`, `prompts`: the tool registry,
  dispatch, background jobs and system-prompt generation.

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

## License

Apache-2.0. See [LICENSE](https://github.com/HaikeiLabs/agentware/blob/main/LICENSE).
