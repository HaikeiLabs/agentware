# Go Middleware Usage Examples

This document provides examples of how to use the Go middleware for policy
enforcement and audit logging. It is the reference implementation; the Python
and TypeScript ports mirror it.

## Basic Usage

### Creating a Policy

```go
package main

import (
    "time"

    "github.com/soypete/pedro-agentware/go/middleware"
)

func main() {
    policy := &middleware.Policy{
        DefaultDeny: false,
        Rules: []middleware.Rule{
            {
                Name:  "rate-limit-tools",
                Tools: []string{"*"},
                Action: middleware.ActionAllow,
                MaxRate: &middleware.RateLimit{
                    Count:  10,
                    Window: 60 * time.Second,
                },
            },
            {
                Name:  "deny-admin",
                Tools: []string{"delete_database", "drop_table"},
                Action: middleware.ActionDeny,
                Conditions: []middleware.Condition{
                    {
                        Field:    "caller.trusted",
                        Operator: middleware.OperatorEq,
                        Value:    "false",
                    },
                },
            },
        },
    }
}
```

### Wrapping a Tool Executor

```go
import (
    "context"

    "github.com/soypete/pedro-agentware/go/middleware"
    "github.com/soypete/pedro-agentware/go/tools"
)

// A ToolExecutor runs a named tool with the given args.
type MyToolExecutor struct{}

func (e *MyToolExecutor) Execute(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
    // Your tool execution logic here
    return &tools.Result{Success: true, Output: "Executed " + toolName}, nil
}

// Create middleware
exec := &MyToolExecutor{}
mw := middleware.NewMiddleware(exec).WithPolicy(policy).WithAuditor(auditor)

// Call a tool through middleware
result, err := mw.Execute(context.Background(), "read_file", map[string]any{"path": "/tmp/test.txt"})
```

### Using Caller Context and Delegation

```go
// Create caller context with user information. InvokingSubject is the human
// who started the request; Trusted defaults to false (fail-closed).
caller := middleware.CallerContext{
    Trusted:         true,
    Role:            "user",
    UserID:          "user-123",
    SessionID:       "session-456",
    Source:          "cli",
    InvokingSubject: "user-123",
}

// Attach it to the context, then spawn a subagent. The invoking subject is
// carried unchanged and the depth increments, so the audit trail resolves
// back to the human who authorized the request.
ctx := middleware.WithCallerContext(context.Background(), caller)
subagent := caller.Delegate("subagent-1")
// subagent.InvokingSubject == "user-123"
// subagent.DelegationDepth == 1
// subagent.ParentSpan == "subagent-1"

result, err := mw.Execute(ctx, "read_file", map[string]any{"path": "/tmp/test.txt"})
```

### Using Audit

```go
// Create in-memory auditor
auditor := middleware.NewInMemoryAuditor()
mw := middleware.NewMiddleware(exec).WithAuditor(auditor)

// After tool calls, query the audit log. Records carry the delegation chain:
// InvokingSubject, ParentSpan, DelegationDepth, Framework.
records := auditor.Query(middleware.AuditFilter{SessionID: "session-456"})
for _, entry := range records {
    fmt.Printf("Decision: %s, Tool: %s, Rule: %s\n", entry.Decision, entry.ToolName, entry.PolicyID)
}
```

### Audited Tool Client

```go
client := middleware.NewAuditedToolClient("my-agent", func(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
    return &tools.Result{Success: true}, nil
})

result, err := client.Execute(ctx, "read_file", map[string]any{"path": "/tmp/test.txt"})
// result: audited either way; framework stamped from the client source.
```

## kei-proxy Policy Evaluator

`evaluator.KeiProxyEvaluator` (`go/kei/evaluator`) is a
`middleware.PolicyEvaluator` that runs `kei-proxy authorize` before each tool
call. It fails closed on anything other than an explicit `allow`/`permit` with
exit 0. `evaluator.CLIClient` passes `KEI_RUNTIME_TOKEN` to the child through
an allowlisted environment only (no `PATH` or `HOME`), never argv. The child
runs in its own process group, and a timeout SIGKILLs the group. Set
`ExpectedSHA256` to pin the binary: it is re-hashed from an `O_NOFOLLOW`
descriptor before every spawn, and any drift denies with `kei-proxy
pin_mismatch`.

```go
import (
    "time"

    "github.com/soypete/pedro-agentware/go/kei/evaluator"
    "github.com/soypete/pedro-agentware/go/middleware"
)

eval := evaluator.NewKeiProxyEvaluator(&evaluator.CLIClient{
    Executable: "kei-proxy",
    Timeout:    10 * time.Second,
})
mw := middleware.NewMiddleware(exec).WithPolicy(eval)

decision := eval.Evaluate("github.get_issue", map[string]any{"owner": "acme", "repo": "pipe"}, caller)
if decision.Enrollment != nil {
    // deny + enrollment claim link: reply to the user privately; never log it.
}
```

Reasons read `kei-proxy <class>[: <detail>]` with a class from
`evaluator.ReasonClasses`. `span_id`, `agent_id`, `agent_version`, `framework`
and `workspace_id` are read from `caller.Metadata`. The behaviour matches the
Python and TypeScript ports case for case; see
`docs/kei-proxy-evaluator-parity.md`.

## Condition Operators

| Operator | Description |
|----------|-------------|
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

- A missing caller context is never trusted (`CallerContext.Trusted` defaults
  to false).
- The hermes and ADK adapters follow the same rule: no context, no trust.
- A policy denial is an audited outcome, not a transport error.
- `KEI_PROXY_DISABLED` denies every governed call; it is never an allow switch.
  See `docs/fail-closed-design.md`.
