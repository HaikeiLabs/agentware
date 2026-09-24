# Middleware for LLM Agents

This guide explains how to use the middleware to add policy enforcement, rate limiting, and auditing to your LLM agent tool calls. Designed for implementation in an agent harness.

## What Middleware Does

The middleware sits between your LLM (or agent loop) and tool execution:

```text
LLM Request → Middleware → Policy Check → (allow/deny/filter) → Tool Executor → Response
                              ↓
                         Auditor (logs every decision)
```

Every tool call is intercepted and evaluated against policies before execution.

## Quick Start

```go
import "github.com/soypete/pedro-agentware/go/middleware"

// 1. Create your tool executor (implements middleware.ToolExecutor)
type MyTools struct{}

func (t *MyTools) Execute(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error) {
    // Your tool logic here
    return &tools.Result{Content: "done", Success: true}, nil
}

// 2. Create a policy
policy := &middleware.Policy{
    DefaultDeny: false,
    Rules: []middleware.Rule{
        {
            Name:  "rate-limit-all",
            Tools: []string{"*"},
            Action: middleware.ActionAllow,
            MaxRate: &middleware.RateLimit{
                Count:  10,
                Window: 60 * time.Second,
            },
        },
    },
}

// 3. Wrap your executor with middleware
mw := middleware.NewMiddleware(&MyTools{}).
    WithPolicy(policy).
    WithAuditor(middleware.NewInMemoryAuditor())

// 4. Call tools through middleware (from your agent loop)
result, err := mw.Execute(ctx, "my_tool", map[string]any{"arg": "value"})
```

## Use Cases

### 1. Rate Limiting Per Tool

Prevent the agent from calling expensive tools too frequently:

```go
policy := &middleware.Policy{
    Rules: []middleware.Rule{
        {
            Name:  "limit-search",
            Tools: []string{"search", "web_fetch"},
            Action: middleware.ActionAllow,
            MaxRate: &middleware.RateLimit{
                Count:  5,   // max 5 calls
                Window: 60 * time.Second,  // in 60 seconds
            },
        },
        {
            Name:  "limit-db",
            Tools: []string{"query_database", "write_database"},
            Action: middleware.ActionAllow,
            MaxRate: &middleware.RateLimit{
                Count:  20,
                Window: 60 * time.Second,
            },
        },
    },
}
```

### 2. Deny Dangerous Tools for Untrusted Callers

Block destructive operations unless the caller is trusted:

```go
policy := &middleware.Policy{
    DefaultDeny: false,
    Rules: []middleware.Rule{
        {
            Name:  "deny-admin-unless-trusted",
            Tools: []string{"delete_database", "drop_table", "execute_shell"},
            Action: middleware.ActionDeny,
            Conditions: []middleware.Condition{
                {
                    Field:    "caller.trusted",
                    Operator: "eq",
                    Value:    "false",
                },
            },
        },
        {
            Name:  "allow-admin-if-trusted",
            Tools: []string{"delete_database", "drop_table", "execute_shell"},
            Action: middleware.ActionAllow,
            Conditions: []middleware.Condition{
                {
                    Field:    "caller.trusted",
                    Operator: "eq",
                    Value:    "true",
                },
            },
        },
    },
}
```

### 3. Block Tools Based on User Role

Restrict tools by role:

```go
policy := &middleware.Policy{
    DefaultDeny: true,  // deny by default
    Rules: []middleware.Rule{
        {
            Name:  "readers-read",
            Tools: []string{"read_file", "search"},
            Action: middleware.ActionAllow,
            Conditions: []middleware.Condition{
                {Field: "caller.role", Operator: "eq", Value: "reader"},
                {Field: "caller.role", Operator: "eq", Value: "admin"},
            },
        },
        {
            Name:  "writers-write",
            Tools: []string{"write_file", "create_file"},
            Action: middleware.ActionAllow,
            Conditions: []middleware.Condition{
                {Field: "caller.role", Operator: "eq", Value: "writer"},
                {Field: "caller.role", Operator: "eq", Value: "admin"},
            },
        },
    },
}
```

### 4. Filter Sensitive Arguments

Redact sensitive data from tool arguments:

```go
policy := &middleware.Policy{
    Rules: []middleware.Rule{
        {
            Name:  "redact-api-keys",
            Tools: []string{"call_api", "webhook"},
            Action: middleware.ActionAllow,
            RedactFields: []string{"api_key", "secret", "password"},
        },
    },
}
```

When a rule with `RedactFields` matches, those arguments are replaced with `******` before being passed to the tool.

### 5. Audit Everything

Log all tool calls for compliance and debugging:

```go
auditor := middleware.NewInMemoryAuditor()

mw := middleware.NewMiddleware(exec).
    WithPolicy(policy).
    WithAuditor(auditor)

// After agent runs, query the audit log
deniedCalls := auditor.Query(middleware.AuditFilter{
    Action: middleware.ActionDeny,
})

for _, record := range deniedCalls {
    fmt.Printf("Denied: %s (rule: %s, reason: %s)\n",
        record.ToolName, record.Decision.Rule, record.Decision.Reason)
}

// Get last hour of calls
recentCalls := auditor.Query(middleware.AuditFilter{
    Since:  time.Now().Add(-1 * time.Hour),
    Limit:  100,
})
```

## Caller Context

The middleware reads `CallerContext` from `context.Context`. You must attach it before calling tools:

```go
callerCtx := middleware.CallerContext{
    UserID:    "user-123",
    SessionID: "session-456",
    Role:      "admin",
    Source:    "cli",          // "api", "cli", "web", etc.
    Trusted:   true,           // whether caller is trusted (for dangerous tool checks)
    Metadata:  map[string]string{
        "org_id": "acme-corp",
    },
}

ctx := middleware.WithCallerContext(context.Background(), callerCtx)
result, err := mw.Execute(ctx, "tool_name", args)
```

Fields in `CallerContext`:

- `UserID` - unique user identifier
- `SessionID` - session/conversation ID
- `Role` - user role (reader, writer, admin, etc.)
- `Source` - where the call originated (api, cli, web)
- `Trusted` - whether to trust this caller with dangerous tools
- `Metadata` - arbitrary key-value pairs for conditions

## Condition Operators

Use conditions to make rules match based on caller or arguments:

| Operator | Description | Example |
|----------|-------------|---------|
| `eq` | equals | `caller.role eq "admin"` |
| `not_eq` | does not equal | `caller.source not_eq "api"` |
| `contains` | contains substring | `args.path contains "/etc/"` |
| `not_contains` | does not contain | `args.query not_contains "DROP"` |
| `matches` | matches pattern | `caller.user_id matches ".*@company.com"` |
| `not_matches` | does not match | `args.action not_matches "delete.*"` |
| `exists` | field exists and non-empty | `args.api_key exists` |
| `not_exists` | field missing or empty | `args.token not_exists` |

### Field References

Conditions can reference:

- `caller.role` - caller's role
- `caller.user_id` - user ID
- `caller.session_id` - session ID
- `caller.source` - call source
- `caller.trusted` - trusted flag (true/false)
- `args.<name>` - tool argument value

## Decision Actions

When evaluating a tool call, the policy returns one of:

- `allow` - execute the tool
- `deny` - don't execute, return error
- `filter` - execute but with modified arguments (for redaction)

## Loading Policy from YAML

For configuration without hardcoding:

```go
policy, err := middleware.LoadPolicyFromFile("policy.yaml")
```

Example `policy.yaml`:

```yaml
rules:
  - name: rate-limit-search
    tools:
      - search
      - web_fetch
    action: allow
    max_rate:
      count: 5
      window: 60

  - name: deny-delete-untrusted
    tools:
      - delete
      - drop
    action: deny
    conditions:
      - field: caller.trusted
        operator: eq
        value: "false"

  - name: redact-secrets
    tools:
      - api_call
    action: filter
    redact_fields:
      - api_key
      - secret

default_deny: false
```

## Integrating with Agent Frameworks

### Basic Agent Loop

```go
func runAgent(ctx context.Context, llm LLM, tools *middleware.Middleware) {
    messages := []Message{{Role: "user", Content: "List files in /tmp"}}

    for {
        resp, err := llm.Chat(ctx, messages)
        if err != nil {
            break
        }

        if len(resp.ToolCalls) == 0 {
            fmt.Println(resp.Content)
            break
        }

        for _, call := range resp.ToolCalls {
            // Attach caller context for each tool call
            callerCtx := middleware.CallerContext{
                UserID:    "agent-user",
                SessionID: "session-1",
                Role:      "agent",
                Trusted:   true,
            }
            ctx := middleware.WithCallerContext(ctx, callerCtx)

            result, err := tools.Execute(ctx, call.Name, call.Args)
            if err != nil {
                messages = append(messages, Message{
                    Role:    "tool",
                    Content: fmt.Sprintf("Error: %v", err),
                })
                continue
            }

            messages = append(messages, Message{
                Role:    "tool",
                Content: fmt.Sprintf("%v", result.Content),
                Name:    call.Name,
            })
        }
    }
}
```

### Multi-Tenant Agent

For agents serving multiple users:

```go
func handleUserRequest(userID string, toolName string, args map[string]any) (*tools.Result, error) {
    // Get user role from your auth system
    role := getUserRole(userID)

    callerCtx := middleware.CallerContext{
        UserID:    userID,
        SessionID: uuid.New().String(),
        Role:      role,
        Source:    "api",
        Trusted:   role == "admin",
    }

    ctx := middleware.WithCallerContext(context.Background(), callerCtx)
    return mw.Execute(ctx, toolName, args)
}
```

### With Rate Limiting Per User

```go
// Create a policy that limits per user via caller.session_id
policy := &middleware.Policy{
    Rules: []middleware.Rule{
        {
            Name:  "per-user-limit",
            Tools: []string{"*"},
            Action: middleware.ActionAllow,
            MaxRate: &middleware.RateLimit{
                Count:  100,
                Window: 60 * time.Second,
            },
        },
    },
}

// The rate limiter tracks by session_id automatically
```

## Full Example: API Server with Middleware

```go
package main

import (
    "context"
    "net/http"
    "time"

    "github.com/soypete/pedro-agentware/go/middleware"
    "github.com/soypete/pedro-agentware/go/tools"
)

type ToolExecutor struct{}

func (e *ToolExecutor) Execute(ctx context.Context, name string, args map[string]any) (*tools.Result, error) {
    // Your tool implementations
    return &tools.Result{Content: "ok", Success: true}, nil
}

func main() {
    // Setup middleware
    exec := &ToolExecutor{}
    auditor := middleware.NewInMemoryAuditor()

    policy := &middleware.Policy{
        DefaultDeny: false,
        Rules: []middleware.Rule{
            {
                Name:  "api-rate-limit",
                Tools: []string{"*"},
                Action: middleware.ActionAllow,
                MaxRate: &middleware.RateLimit{
                    Count:  50,
                    Window: 60 * time.Second,
                },
            },
            {
                Name:  "deny-destructive",
                Tools: []string{"delete", "drop", "truncate"},
                Action: middleware.ActionDeny,
                Conditions: []middleware.Condition{
                    {Field: "caller.trusted", Operator: "eq", Value: "false"},
                },
            },
        },
    }

    mw := middleware.NewMiddleware(exec).
        WithPolicy(policy).
        WithAuditor(auditor)

    // HTTP handler
    http.HandleFunc("/tool", func(w http.ResponseWriter, r *http.Request) {
        callerCtx := middleware.CallerContext{
            UserID:    r.Header.Get("X-User-ID"),
            SessionID: r.Header.Get("X-Session-ID"),
            Role:      r.Header.Get("X-User-Role"),
            Source:    "api",
            Trusted:   r.Header.Get("X-User-Role") == "admin",
        }

        ctx := middleware.WithCallerContext(r.Context(), callerCtx)
        result, err := mw.Execute(ctx, r.URL.Query().Get("tool"), map[string]any{
            "args": r.URL.Query().Get("args"),
        })

        if err != nil || !result.Success {
            http.Error(w, result.Error, http.StatusForbidden)
            return
        }
        w.Write([]byte(result.Content))
    })

    http.ListenAndServe(":8080", nil)
}
```

## Summary

| Feature | Use For |
|---------|---------|
| Rate Limiting | Prevent API abuse, control costs |
| Deny Rules | Block dangerous tools for untrusted callers |
| Conditions | Role-based access, argument filtering |
| Redaction | Hide sensitive data from logs/tools |
| Auditing | Compliance, debugging, monitoring |
| CallerContext | Multi-tenant isolation, user tracking |
