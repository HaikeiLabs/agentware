# pedro-agentware: Engineering Design Document

**Status**: Living Document — v0.1 Draft  
**Repo**: github.com/Soypete/pedro-agentware  
**Author**: SoypeteTech  
**Last Updated**: September 2026

---

## Table of Contents

1. [Purpose & Scope](#1-purpose--scope)
2. [Design Philosophy](#2-design-philosophy)
3. [Architecture Overview](#3-architecture-overview)
4. [Package Organization](#4-package-organization)
5. [Core Interfaces](#5-core-interfaces)
6. [Data Flow](#6-data-flow)
7. [Package Specifications](#7-package-specifications)
8. [Cross-Language Porting Guide](#8-cross-language-porting-guide)
9. [Extension Points](#9-extension-points)
10. [Versioning & Stability Contract](#10-versioning--stability-contract)
11. [Contributing Guidelines](#11-contributing-guidelines)
12. [Tenant-Side Distributed Proxy & Control-Plane Boundary](#12-tenant-side-distributed-proxy--control-plane-boundary)

---

## 1. Purpose & Scope

### What pedro-agentware Is

pedro-agentware is a **language-agnostic agent middleware SDK** for building self-hosted AI agent systems. It provides the infrastructure layer between your application and your LLM — the plumbing that makes agentic tool use reliable, auditable, and safe.

It is **not** a full agent framework. It does not dictate how you write prompts, which model you use, or how your application is structured. It gives you composable primitives that you wire together.

### Problem Statement

Building agents on self-hosted LLMs is harder than building on cloud APIs because:

- Different models (Qwen, Llama, Mistral) use completely different tool call wire formats
- There is no standard middleware layer for policy enforcement, rate limiting, or auditing
- The inference loop (parse → execute → feedback → repeat) is re-implemented from scratch by everyone
- Context window management is model-specific and easy to get wrong
- There is no standard way to define tools and auto-generate their prompt representations

pedro-agentware solves these problems in a model-agnostic, self-hosted-first way.

### Scope

**In scope:**

- Tool definition contracts and registries
- Model-specific tool call formatting (Qwen, Llama, Mistral, OpenAI-compatible)
- Policy enforcement on tool calls (rate limits, allow/deny, field redaction)
- Audit logging of all tool call decisions
- The inference loop: parse → dispatch → feedback → iterate
- Dynamic prompt generation from tool schemas
- LLM backend abstraction (OpenAI-compatible HTTP)
- Job lifecycle management for async agent tasks
- File-based context management (crash-recoverable agent state)

**Out of scope:**

- Application-specific tool implementations (file editors, git tools, etc.)
- Agent-specific system prompts and domain logic
- UI or HTTP server layers
- Fine-tuning or model training

---

## 2. Design Philosophy

### 2.1 Middleware, Not Framework

pedro-agentware occupies the middleware layer. It enforces contracts and policies but does not own the application. You can adopt one package at a time without buying into the whole stack.

### 2.2 Self-Hosted First

Every design decision prioritizes running on your own hardware with your own models. Cloud API compatibility is a bonus, never a requirement.

### 2.3 Interface-Driven

All major components are defined as interfaces before implementations. This enables easy substitution and clean porting to Python and TypeScript.

### 2.4 Explicit Over Magic

No reflection-based tool registration. No hidden middleware chains. Everything is wired explicitly.

### 2.5 Composable Primitives

Each package should be usable in isolation.

---

## 3. Architecture Overview

```text
┌─────────────────────────────────────────────────────────────┐
│                    Your Agent Application                     │
└───────────────────────┬─────────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────────┐
│                    pedro-agentware SDK                        │
│                                                               │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────────────┐  │
│  │   executor  │   │  middleware  │   │    toolformat    │  │
│  │             │──▶│              │──▶│                  │  │
│  │ Inference   │   │ Policy       │   │ Model-specific   │  │
│  │ loop        │   │ enforcement  │   │ serialization    │  │
│  │ Tool        │   │ Rate limits  │   │ Qwen / Llama /   │  │
│  │ dispatch    │   │ Audit log    │   │ Mistral / JSON   │  │
│  └──────┬──────┘   └──────────────┘   └──────────────────┘  │
│         │                                                      │
│  ┌──────▼──────┐   ┌──────────────┐   ┌──────────────────┐  │
│  │    tools    │   │    prompts   │   │      llm         │  │
│  │             │   │              │   │                  │  │
│  │ Tool        │   │ Dynamic      │   │ Backend          │  │
│  │ interface   │   │ prompt gen   │   │ abstraction      │  │
│  │ Registry    │   │ from schemas │   │ OpenAI-compat    │  │
│  └─────────────┘   └──────────────┘   └──────────────────┘  │
│                                                               │
│  ┌─────────────┐   ┌──────────────┐                          │
│  │    jobs     │   │  llmcontext  │                          │
│  │             │   │              │                          │
│  │ Async job   │   │ File-based   │                          │
│  │ lifecycle   │   │ context mgmt │                          │
│  └─────────────┘   └──────────────┘                          │
└─────────────────────────────────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────────┐
│                    LLM Backend                                │
│           (llama.cpp / Ollama / vLLM / OpenAI)               │
└─────────────────────────────────────────────────────────────┘
```

### Package Dependency Graph

```text
executor
  ├── middleware
  ├── tools
  ├── toolformat
  ├── prompts
  └── llm

middleware
  └── tools

prompts
  └── tools

toolformat
  └── (no internal deps)

tools
  └── (no internal deps)

llm
  └── tools

jobs
  └── (no internal deps)

llmcontext
  └── (no internal deps)
```

---

## 4. Package Organization

```text
github.com/Soypete/pedro-agentware/
├── go.mod
│
├── tools/
│   ├── tool.go
│   ├── result.go
│   ├── registry.go
│   └── tools_test.go
│
├── toolformat/
│   ├── formatter.go
│   ├── selector.go
│   ├── generic.go
│   ├── qwen.go
│   ├── llama.go
│   ├── mistral.go
│   └── toolformat_test.go
│
├── middleware/
│   ├── middleware.go
│   ├── policy.go
│   ├── audit.go
│   ├── types.go
│   ├── condition.go
│   ├── ratelimit.go
│   └── middleware_test.go
│
├── prompts/
│   ├── generator.go
│   ├── tool_section.go
│   ├── schema.go
│   └── prompts_test.go
│
├── llm/
│   ├── backend.go
│   ├── request.go
│   ├── response.go
│   ├── server.go
│   ├── tokens.go
│   ├── factory.go
│   └── llm_test.go
│
├── executor/
│   ├── executor.go
│   ├── inference.go
│   ├── parser.go
│   ├── dispatcher.go
│   ├── completion.go
│   └── executor_test.go
│
├── jobs/
│   ├── job.go
│   ├── manager.go
│   └── jobs_test.go
│
├── llmcontext/
│   ├── manager.go
│   ├── entry.go
│   └── llmcontext_test.go
│
└── docs/
    ├── design.md
    ├── porting-guide.md
    └── examples/
        ├── minimal/
        └── full-stack/
```

---

## 5. Core Interfaces

### 5.1 Tool Contract (`tools` package)

```go
// tools/tool.go
package tools

import "context"

type Tool interface {
    Name() string
    Description() string
    Execute(ctx context.Context, args map[string]any) (*Result, error)
}

type ExtendedTool interface {
    Tool
    InputSchema() map[string]any
    Examples() []ToolExample
}

type ToolExample struct {
    Input       map[string]any
    Output      string
    Explanation string
}

type Result struct {
    Success      bool
    Output       string
    Error        string
    ModifiedFiles []string
    Metadata     map[string]any
}
```

### 5.2 Tool Registry (`tools` package)

```go
// tools/registry.go
package tools

type ToolRegistry struct {
    tools map[string]Tool
}

func NewToolRegistry() *ToolRegistry
func (r *ToolRegistry) Register(t Tool)
func (r *ToolRegistry) Get(name string) (Tool, bool)
func (r *ToolRegistry) All() []Tool
func (r *ToolRegistry) Names() []string
func (r *ToolRegistry) Schemas() map[string]map[string]any
```

### 5.3 Tool Formatter (`toolformat` package)

```go
// toolformat/formatter.go
package toolformat

import "github.com/Soypete/pedro-agentware/tools"

type ToolFormatter interface {
    FormatToolDefinitions(tools []tools.Tool) string
    ParseToolCalls(response string) ([]ParsedToolCall, error)
    FormatToolResult(name string, result *tools.Result) string
    ModelFamily() string
}

type ParsedToolCall struct {
    ID   string
    Name string
    Args map[string]any
    Raw  string
}
```

### 5.4 LLM Backend (`llm` package)

```go
// llm/backend.go
package llm

import (
    "context"
    "github.com/Soypete/pedro-agentware/tools"
)

type Backend interface {
    Complete(ctx context.Context, req *Request) (*Response, error)
    SupportsNativeToolCalling() bool
    ModelName() string
    ContextWindowSize() int
}

type Request struct {
    Messages    []Message
    Tools       []ToolDefinition
    Temperature float64
    MaxTokens   int
    Stop        []string
}

type Message struct {
    Role       Role
    Content    string
    ToolCallID string
    ToolCalls  []ToolCall
}

type Role string

const (
    RoleSystem    Role = "system"
    RoleUser      Role = "user"
    RoleAssistant Role = "assistant"
    RoleTool      Role = "tool"
)

type ToolDefinition struct {
    Name        string
    Description string
    InputSchema map[string]any
}

type Response struct {
    Content       string
    ToolCalls     []ToolCall
    FinishReason  string
    UsageTokens   TokenUsage
}

type ToolCall struct {
    ID   string
    Name string
    Args map[string]any
}

type TokenUsage struct {
    PromptTokens     int
    CompletionTokens int
    TotalTokens      int
}
```

### 5.5 Policy & Middleware (`middleware` package)

```go
// middleware/types.go
package middleware

import "time"

type Action string

const (
    ActionAllow  Action = "allow"
    ActionDeny   Action = "deny"
    ActionFilter Action = "filter"
)

type CallerContext struct {
    UserID    string
    SessionID string
    Role      string
    Source    string
    Trusted   bool
    Metadata  map[string]string
}

type Decision struct {
    Action       Action
    Rule         string
    Reason       string
    RedactedArgs map[string]any
    Timestamp    time.Time
}
```

```go
// middleware/policy.go
package middleware

type PolicyEvaluator interface {
    Evaluate(toolName string, args map[string]any, caller CallerContext) Decision
}

type Policy struct {
    Rules       []Rule
    DefaultDeny bool
}

type Rule struct {
    Name         string
    Tools        []string
    Action       Action
    Conditions   []Condition
    MaxRate      *RateLimit
    RedactFields []string
}

type RateLimit struct {
    Count  int
    Window time.Duration
}

type Condition struct {
    Field    string
    Operator Operator
    Value    string
}

type Operator string

const (
    OperatorEq           Operator = "eq"
    OperatorNotEq        Operator = "not_eq"
    OperatorContains     Operator = "contains"
    OperatorNotContains  Operator = "not_contains"
    OperatorMatches      Operator = "matches"
    OperatorNotMatches   Operator = "not_matches"
    OperatorExists       Operator = "exists"
    OperatorNotExists    Operator = "not_exists"
)
```

```go
// middleware/audit.go
package middleware

import "time"

type AuditRecord struct {
    SessionID string
    ToolName  string
    Args      map[string]any
    Decision  Decision
    Timestamp time.Time
}

type Auditor interface {
    Record(record AuditRecord)
    Query(filter AuditFilter) []AuditRecord
}

type AuditFilter struct {
    SessionID string
    ToolName  string
    Action    Action
    Since     time.Time
    Limit     int
}
```

```go
// middleware/middleware.go
package middleware

import (
    "context"
    "github.com/Soypete/pedro-agentware/tools"
)

type ToolExecutor interface {
    Execute(ctx context.Context, toolName string, args map[string]any) (*tools.Result, error)
}

type Middleware interface {
    ToolExecutor
    WithPolicy(p PolicyEvaluator) Middleware
    WithAuditor(a Auditor) Middleware
}
```

### 5.6 Prompt Generator (`prompts` package)

```go
// prompts/generator.go
package prompts

import "github.com/Soypete/pedro-agentware/tools"

type PromptGenerator interface {
    GenerateToolSection(registry *tools.ToolRegistry) string
    GenerateToolSchemas(registry *tools.ToolRegistry) []map[string]any
}
```

### 5.7 Inference Executor (`executor` package)

```go
// executor/executor.go
package executor

import (
    "context"
    "github.com/Soypete/pedro-agentware/llm"
    "github.com/Soypete/pedro-agentware/middleware"
    "github.com/Soypete/pedro-agentware/toolformat"
    "github.com/Soypete/pedro-agentware/tools"
)

type Executor interface {
    Execute(ctx context.Context, req ExecuteRequest) (*ExecuteResult, error)
}

type ExecuteRequest struct {
    SystemPrompt  string
    UserMessage   string
    History       []llm.Message
    MaxIterations int
    CallerCtx     middleware.CallerContext
    JobID         string
}

type ExecuteResult struct {
    FinalResponse    string
    Iterations       int
    ToolCallsMade    int
    TerminationReason TerminationReason
    Conversation     []llm.Message
}

type TerminationReason string

const (
    TerminationComplete     TerminationReason = "complete"
    TerminationMaxIterations TerminationReason = "max_iterations"
    TerminationError        TerminationReason = "error"
    TerminationCanceled     TerminationReason = "canceled"
)

type InferenceExecutorConfig struct {
    Backend        llm.Backend
    Registry       *tools.ToolRegistry
    ToolExec       middleware.ToolExecutor
    Formatter      toolformat.ToolFormatter
    MaxIterations  int
    CompletionSignal string
}

func NewInferenceExecutor(cfg InferenceExecutorConfig) Executor
```

### 5.8 Job Manager (`jobs` package)

```go
// jobs/job.go
package jobs

import "time"

type Status string

const (
    StatusPending  Status = "pending"
    StatusRunning  Status = "running"
    StatusComplete Status = "complete"
    StatusFailed   Status = "failed"
    StatusCanceled Status = "canceled"
)

type Job struct {
    ID          string
    Status      Status
    Description string
    CreatedAt   time.Time
    UpdatedAt   time.Time
    Result      string
    Error       string
}
```

```go
// jobs/manager.go
package jobs

import "context"

type JobManager interface {
    Create(description string) (jobID string, err error)
    Start(jobID string) error
    Complete(jobID string, result string) error
    Fail(jobID string, errMsg string) error
    Cancel(jobID string) error
    Get(jobID string) (*Job, error)
    List(status *Status) ([]*Job, error)
    Watch(ctx context.Context, jobID string) (<-chan *Job, error)
}
```

### 5.9 Context Manager (`llmcontext` package)

```go
// llmcontext/manager.go
package llmcontext

import (
    "github.com/Soypete/pedro-agentware/llm"
    "github.com/Soypete/pedro-agentware/toolformat"
)

type ContextManager interface {
    AppendPrompt(jobID string, msg llm.Message) error
    AppendResponse(jobID string, msg llm.Message) error
    AppendToolCalls(jobID string, calls []toolformat.ParsedToolCall) error
    AppendToolResults(jobID string, results []ToolResultEntry) error
    GetHistory(jobID string) ([]llm.Message, error)
    Purge(jobID string) error
}

type ToolResultEntry struct {
    CallID   string
    ToolName string
    Args     map[string]any
    Output   string
    Success  bool
}
```

---

## 6. Data Flow

```text
Application
    │
    │ ExecuteRequest
    ▼
InferenceExecutor.Execute()
    │
    │ builds []Message
    ▼
llm.Backend.Complete(Request)
    │
    │ Response{Content, ToolCalls}
    ▼
toolformat.ToolFormatter.ParseToolCalls()
    │
    │ []ParsedToolCall
    ▼
for each ParsedToolCall:
    │
    ▼
middleware.Middleware.Execute(toolName, args)
    │
    ├─► PolicyEvaluator.Evaluate()
    ├─► Auditor.Record()
    └─► ToolRegistry.Get(toolName).Execute()
    │
    ▼ *tools.Result
toolformat.ToolFormatter.FormatToolResult()
    │
    ▼
llmcontext.ContextManager.AppendToolResults()
    │
    ▼
[loop back to Backend.Complete]
    │
    ▼
ExecuteResult
```

---

## 7. Package Specifications

### Stability Tiers

|Package     |Tier            |Notes                                                            |
|------------|----------------|-----------------------------------------------------------------|
|`tools`     |**Stable**      |Core contract.                                                   |
|`middleware`|**Stable**      |Policy and audit interfaces are stable.                         |
|`toolformat`|**Stable**      |Formatter interface is stable.                                  |
|`llm`       |**Stable**      |Backend interface is stable.                                    |
|`executor`  |**Beta**        |Interfaces may change as loop semantics evolve.                 |
|`prompts`   |**Beta**        |Generator output format may change.                             |
|`jobs`      |**Beta**        |JobManager interface is stable.                                 |
|`llmcontext`|**Experimental**|File-based approach may be abstracted further.                 |

---

## 8. Cross-Language Porting Guide

|Go               |Python                              |TypeScript                   |
|-----------------|------------------------------------|-----------------------------|
|`interface`      |`Protocol` or `ABC`                 |`interface`                  |
|`struct`         |`dataclass`                         |`interface` or `type`        |
|`map[string]any` |`dict[str, Any]`                    |`Record<string, unknown>`    |
|`context.Context`|`asyncio.Task` or custom `Context` |`AbortSignal`                |
|`error` return   |`raise Exception`                   |`throw Error`                |

---

## 9. Extension Points

### Adding a New Model Formatter

1. Create `toolformat/<modelname>.go`
2. Implement `ToolFormatter` interface
3. Add detection logic to `selector.go`
4. Add tests

### Adding a New LLM Backend

1. Create `llm/<backendname>.go`
2. Implement `Backend` interface
3. Add to `factory.go`

### Adding a New Auditor

1. Implement the `Auditor` interface
2. Pass it to `middleware.WithAuditor()`

---

## 10. Versioning & Stability Contract

pedro-agentware follows **semantic versioning** (semver).

|Change type                           |Version bump  |
|--------------------------------------|--------------|
|New interface added                   |Minor         |
|New method on existing stable interface|Major        |
|New field on existing struct (additive)|Minor        |
|Removing or renaming Stable tier      |Major         |

---

## 11. Contributing Guidelines

### Adding a New Package

New top-level packages require an ADR in `docs/adr/` before implementation.

### Test Requirements

- Every exported interface must have a test
- Every `ToolFormatter` must have tests with actual LLM output
- Test coverage target: 80% per package

### PR Checklist

- [ ] Interfaces defined before implementations
- [ ] No new dependencies on concrete types from sibling packages
- [ ] `go vet ./...` passes
- [ ] Tests added for new behavior
- [ ] CHANGELOG.md updated

---

## 12. Tenant-Side Distributed Proxy & Control-Plane Boundary

> Before changing middleware, tool bindings, or the `kei/` surface, read the
> canonical `docs/tenant-proxy-reference.md` and search the shared Herdr wiki
> (`wiki search "tenant data distributed proxy"`). This section is the
> abbreviated contract every design change must preserve.

### Roles in one sentence

**Agentware** is the tenant-side execution layer (local tool middleware,
delegation, semantic tool bindings, and proxy integration). **The distributed
proxy** is the connector/provider runtime and the policy enforcement point for
governed external operations. **Kei** is a metadata-only control plane:
registration, auth method and *references*, tool bindings, scopes/resources,
and audit metadata. **ABAC** is a metadata policy decision point only.

### Metadata-only invariant

- `tool_bindings` and `secret_refs` are **non-secret metadata**. They are
  routing/reference identifiers, never permissions, and never materialized
  credentials. The bootstrap secret is rejected from any manifest
  (`python/src/pedro_agentware/kei/config.py` fails closed on this).
- **ABAC is called for metadata policy decisions only**: subject/object/
  operation/environment over data-source resources. ABAC **decides**; it does
  **not** execute writes and does **not** serve or store credentials.
- Provider payloads/results, customer content, credentials, embeddings, and
  indexes **never enter Kei**.

### Execution topology

- **Local agent execution** stays fully possible **without a direct ABAC
  connector dependency**: the local middleware chain evaluates policy and
  audits locally, fail-closed by default.
- **Governed external operations** — actions that cross out of the tenant to
  connectors/providers — use the **proxy boundary**: the proxy resolves
  bindings/auth from control-plane metadata (references, not credentials),
  invokes provider adapters, and returns data only inside the tenant runtime.
  Mutations such as GitHub/CRM/Linear writes are agent capabilities executed in
  the harness/agentware/agents local tool loop; the proxy is the explicit
  approval/governance boundary for them.

### Contraindications

- Do not describe ABAC as a credential or data service.
- Do not describe agentware as sending customer data through the control plane.
- Do not put provider payloads, results, content, credentials, embeddings, or
  indexes into Kei metadata.
