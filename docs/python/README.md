# Python Middleware Usage Examples

This document provides examples of how to use the Python middleware
(`pedro_agentware`) for policy enforcement and audit logging. It mirrors the Go
reference implementation in `go/middleware/`.

## Installation

```bash
pip install -e ./python
# or, from the python/ directory:
cd python && pip install -e ".[dev]"
```

The package is not published to PyPI yet. See
[PyPI distribution strategy](pypi-distribution-strategy.md) for the artifact
contract, release process, and the packaging work required before the first
publish.

## Basic Usage

### Creating a Policy

```python
from pedro_agentware.middleware import Action, Condition, Operator, Policy, Rule

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
from pedro_agentware.middleware import CallerContext, MiddlewareImpl

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
from pedro_agentware.middleware import CallerContext

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
from pedro_agentware.middleware import AuditedToolClient

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
from pedro_agentware.middleware import AuditFilter, InMemoryAuditor

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

Third-party agent harnesses build against `pedro_agentware` through the
`kei/` module — see `docs/harness-contract.md` and
`python/tests/third_party_harness_test.py` for a complete example that imports
nothing outside this library.

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
