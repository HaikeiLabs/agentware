# Agent Adapters

Agentware provides adapters for multiple agent backends, adding guardrails, context management, and unified tooling.

## What Agentware Adds

- **Guardrails** - Policy enforcement, rate limiting, input/output validation
- **Context Management** - LLM context, conversation history, tool definitions
- **Unified Interface** - Same `AgentBackend` Protocol for all backends
- **Observability** - Auditing, logging, metrics
- **Tool Integration** - Standardized tool definitions across all backends

## Backends

### Kitaru Adapter

Durable execution framework built on ZenML. Long-running agents with checkpointing, replay, and state management.

```python
from adapters.kitaru import create_adapter

# Connect to local Kitaru (same pod as agent)
adapter = create_adapter(
    flow_mapping={
        "research": "research-flow",
        "analyze": "data-analyzer",
    }
)

# Execute via agentware middleware
result = await adapter.execute("research", {"topic": "AI"})
```

**Use when**: Long-running agents, need durability/checkpointing, using ZenML

### Pydantic AI Adapter

Type-safe agent framework using Pydantic models for validation.

```python
from adapters.pydantic import create_adapter

adapter = create_adapter(
    model="openai:gpt-4",
    system_prompt="You are a helpful assistant.",
)

result = await adapter.execute("What is 2+2?")
```

**Use when**: Type-safe agents, rapid prototyping, multiple LLM providers

### Hermes Adapter

CLI-based agent that runs as a subprocess.

```python
from adapters.hermes import create_adapter

adapter = create_adapter(
    hermes_path="/usr/local/bin/hermes",
    flow_mapping={
        "search": "search-tool",
    },
)

result = await adapter.execute("search", {"query": "weather"})
```

**Use when**: Legacy CLI agents, subprocess-based execution

## Go Adapters

The Go adapters (`go/adapters/`) provide the same functionality for Go-based tools:

- `go/adapters/kitaru/` - Kitaru client using kitaru-go SDK
- `go/adapters/hermes/` - Hermes subprocess wrapper
- `go/adapters/pydantic/` - Pydantic AI Go bindings

See `examples/go/minimal-harness` for usage.