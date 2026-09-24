# TypeScript Middleware Usage Examples

This document provides examples of how to use the TypeScript middleware
(`@haikeilabs/agentware`) for policy enforcement and audit logging. It mirrors the
Go reference implementation in `go/middleware/`.

For the package artifact, release, and npm publishing contract, see the [npm distribution strategy](./npm-distribution-strategy.md).

## Installation

```bash
npm install @haikeilabs/agentware
```

## Basic Usage

### Creating a Policy

`Policy` and `Rule` are interfaces; build them as plain objects and evaluate
with `SimplePolicyEvaluator`:

```typescript
import {
  Action,
  Operator,
  Policy,
  Rule,
  SimplePolicyEvaluator,
} from "@haikeilabs/agentware";

const policy: Policy = {
  default_deny: false,
  rules: [
    {
      name: "rate-limit-tools",
      tools: ["*"],
      action: Action.ALLOW,
    },
    {
      name: "deny-admin",
      tools: ["delete_database", "drop_table"],
      action: Action.DENY,
      conditions: [
        { field: "caller.trusted", operator: Operator.EQ, value: "false" },
      ],
    },
  ],
};

const evaluator = new SimplePolicyEvaluator(policy);
```

### Creating Middleware

```typescript
import {
  CallerContext,
  MiddlewareImpl,
  ToolExecutor,
} from "@haikeilabs/agentware";

const executor: ToolExecutor = {
  execute(toolName: string, args: Record<string, unknown>): [unknown, boolean, string] {
    // Your tool execution logic here
    return [{ output: `Executed ${toolName}` }, true, ""];
  },
};

const mw = new MiddlewareImpl(executor).withPolicy(evaluator);

// Call a tool through middleware
const [result, success, error] = mw.execute("read_file", { path: "/tmp/test.txt" }, {
  trusted: false,
});
```

### Using Caller Context

```typescript
// Create caller context with user information. trusted defaults to false
// (fail-closed) and must be set explicitly to true.
const caller: CallerContext = {
  trusted: true,
  role: "user",
  user_id: "user-123",
  session_id: "session-456",
  source: "cli",
};

const [result, success, error] = mw.execute("read_file", { path: "/tmp/test.txt" }, caller);
```

### Using Audit

```typescript
import { AuditFilter, InMemoryAuditor } from "@haikeilabs/agentware";

const auditor = new InMemoryAuditor();
const mw = new MiddlewareImpl(executor).withPolicy(evaluator).withAuditor(auditor);

// After tool calls, query the audit log
const records = auditor.query({ session_id: "session-456" } as AuditFilter);
for (const entry of records) {
  console.log(`Decision: ${entry.decision.action}, Tool: ${entry.tool_name}`);
}
```

## Condition Operators

| Operator | Description |
| --- | --- |
| `eq` | Field equals value |
| `not_eq` | Field does not equal value |
| `contains` | Field contains value |
| `not_contains` | Field does not contain value |
| `matches` | Field matches pattern |
| `not_matches` | Field does not match pattern |
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

## Fail-Closed Defaults

- `CallerContext.trusted` is a required field that callers must set explicitly;
  a caller that was never marked trusted is untrusted.
- A policy denial is an audited outcome, not a transport error.
