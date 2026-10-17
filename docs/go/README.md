# Go Middleware Usage Examples

This document provides examples of how to use the Go middleware for policy
enforcement and audit logging. It is the reference implementation; the Python
and TypeScript ports mirror it.

## Installation

```bash
go get github.com/haikeilabs/agentware/go
```

The module path is lowercase; `github.com/HaikeiLabs/agentware/go` does not
resolve. Releases are tagged `go/vX.Y.Z` at the same version as the npm and
PyPI packages (`go get github.com/haikeilabs/agentware/go@vX.Y.Z`).

## Governed harness quick start: socket client first

For a working socket-based harness, start with the [Go getting started
guide](../getting-started/go.md) and the
[`examples/go/minimal-harness`](../../examples/go/minimal-harness) example.
The socket client is recommended. `evaluator.CLIClient` and
`kei-proxy authorize` subprocess integration are deprecated; use
`runtimesocket.Client` or `runtimesocket.SelectAuthorizationClient` with a
configured socket.

## Basic Usage

### Creating a Policy

```go
package main

import (
    "time"

    "github.com/haikeilabs/agentware/go/middleware"
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

    "github.com/haikeilabs/agentware/go/middleware"
    "github.com/haikeilabs/agentware/go/tools"
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

## Deprecated: subprocess kei-proxy policy evaluator

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

    "github.com/haikeilabs/agentware/go/kei/evaluator"
    "github.com/haikeilabs/agentware/go/middleware"
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

### Recommended: over the kei-proxy daemon socket

When `kei-proxy serve` runs beside the harness, talk to it over its Unix
socket (`KEI_RUNTIME_SOCKET_PATH`, default `/run/kei-proxy/runtime.sock`)
instead of spawning a process per call. `runtimesocket.Client` (zero value
usable) opens a `kei.session/v2` session per `(source, external_id)`, and
authorizes, lists models and runs chat completions inside it. The request never
names the user; the daemon decides for the session's subject. An unreachable
daemon, a non-2xx answer or a body that is not the expected JSON is a
`*runtimesocket.Error`, a refusal that unwraps to `*evaluator.AuthorizeError`.
A `session_not_found` session is reopened once. Unix only: on Windows every
call is a `proxy_unavailable` refusal wrapping `ErrUnsupportedPlatform`.

```go
import (
    "github.com/haikeilabs/agentware/go/kei/evaluator"
    "github.com/haikeilabs/agentware/go/kei/runtimesocket"
)

runtime := &runtimesocket.Client{}
session, err := runtime.OpenSession(ctx, runtimesocket.Identity{Source: "openwebui", ExternalID: userID})
answer, err := runtime.Authorize(ctx, session, evaluator.AuthorizeRequest{Tool: "github.get_issue", Action: "execute"})
reply, err := runtime.ChatCompletion(ctx, session, map[string]any{"model": "gpt-4.1", "messages": messages})
err = runtime.StreamChatCompletion(ctx, session, body, func(line string) error { // SSE "data:" lines
    return nil
})
err = runtime.CloseSession(ctx, session)

// The evaluator uses the socket when one is configured, else `kei-proxy authorize`.
client, err := runtimesocket.SelectAuthorizationClient("openwebui", runtimesocket.SelectOptions{})
eval := evaluator.NewKeiProxyEvaluator(client)
```

Register the harness's tools before serving with `toolsync.SyncDocument` (or
`toolsync.Sync` for JSON text): it `PUT`s the tool manifest v2 to
`$KEI_RUNTIME_CONTROL_PLANE_URL/api/v1/runtime/tools` with `KEI_RUNTIME_TOKEN`,
replacing the installation's tools, so it refuses to send an empty manifest.
It honours `KEI_PROXY_EGRESS_PROXY` and `KEI_PROXY_NO_PROXY`, and every failure
wraps `toolsync.ErrSyncFailed`: the harness should not serve.

```go
cfg, err := toolsync.ConfigFromEnv(nil)
result, err := toolsync.SyncDocument(ctx, manifest, cfg, len(packs) > 0)
```

The Python, TypeScript and Go clients are compared in
`docs/kei-runtime-socket-parity.md`.

## MCP route client

Use `mcproute.NewClient` to call a connector's MCP route through the local
`kei-proxy` runtime. The client discovers the configured Unix socket or local
loopback listener, reads `KEI_MCP_ROUTE_KEY` for each request, and exposes
`tools/list` and `tools/call`:

```go
client := mcproute.NewClient("github-connector")
tools, err := client.ListTools(ctx)
result, err := client.CallTool(ctx, "get_issue", map[string]any{"id": "42"})
```

The route key stays in the tenant runtime. The client rejects non-loopback
hosts; provider payloads and results do not pass through Kei's control plane.

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
