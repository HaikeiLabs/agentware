# Python Agent Adapters - Implementation Prompt

## Goal
Create Python adapter packages for Kitaru, Hermes, and Pydantic agent backends that integrate with pedro-agentware middleware. These adapters should follow the same patterns as the Go implementations.

## Context
- Branch: `feature/kitaru-integration` (or create new branch for Python adapters)
- Location: `python/adapters/`
- Use existing open-source libraries where available
- Match the Go adapter patterns for consistency

## Adapters to Implement

### 1. Kitaru Adapter (`python/adapters/kitaru/`)
- **Purpose**: Connect to Kitaru flow execution engine
- **API**: REST API on port 8080
- **Libraries**: `httpx` or `requests`
- **Features**:
  - Execute flows via REST API
  - Poll for completion with configurable interval
  - Checkpoint/restore support
  - Artifact storage
  - Logging

**Reference**: See `go/adapters/kitaru/adapter.go` for interface patterns

### 2. Hermes Adapter (`python/adapters/hermes/`)
- **Purpose**: Connect to Hermes agent runtime
- **Library**: Use `hermes-agent` from NousResearch (https://github.com/nousresearch/hermes-agent)
- **Package**: Python-based (88.7% of repo), install via `pip install hermes-agent` or from source
- **CLI**: `hermes` command for CLI execution, or import as Python module
- **Features**:
  - Agent execution via CLI or programmatic API
  - Checkpoint/restore via `hermes` CLI
  - Artifact management
  - Tool execution via MCP integration

**Reference**: See https://github.com/nousresearch/hermes-agent for Python API details

### 3. Pydantic Adapter (`python/adapters/pydantic/`)
- **Purpose**: Connect to Pydantic AI agents
- **Library**: Use `pydantic-ai` or `pydantic-agent-framework`
- **Features**:
  - Agent tool execution
  - Streaming responses
  - Result handling

## Implementation Guidelines

### Package Structure
```
python/adapters/
├── kitaru/
│   ├── __init__.py
│   ├── adapter.py      # Main adapter implementation
│   ├── client.py       # HTTP client wrapper
│   └── pyproject.toml
├── hermes/
│   ├── __init__.py
│   ├── adapter.py
│   ├── client.py
│   └── pyproject.toml
└── pydantic/
    ├── __init__.py
    ├── adapter.py
    ├── client.py
    └── pyproject.toml
```

### Interface Patterns (Match Go implementations)
```python
class AgentBackend(Protocol):
    """Protocol for agent backends - matches Go ToolExecutor interface"""
    def execute(self, tool_name: str, args: dict) -> AgentResult: ...
    def list_tools(self) -> list[AgentTool]: ...

class AgentTool:
    name: str
    description: str
    input_schema: dict

class AgentResult:
    success: bool
    output: str | dict
    error: str | None
    metadata: dict
```

### Python-Specific Considerations
- Use `typing.Protocol` for interfaces (structural subtyping)
- Use `dataclasses` for result types
- Use `httpx` for async HTTP (or `requests` for sync)
- Include proper error handling
- Add type hints throughout
- Write unit tests with `pytest`

### Configuration
- Support YAML or environment variable config
- Default timeouts, retry counts
- Request/response logging

## Testing Requirements
- Unit tests for each adapter
- Mock implementations for external dependencies
- Test both success and failure paths
- Minimum 80% coverage

## Example Usage
```python
from haikeilabs.agentware.middleware import Middleware
from adapters.kitaru import KitaruAdapter
from adapters.hermes import HermesAdapter
from adapters.pydantic import PydanticAdapter

# All adapters implement the same interface
adapter = KitaruAdapter(base_url="http://localhost:8080", api_key="...")
middleware = Middleware(adapter)
result = middleware.execute("my_tool", {"arg": "value"})
```

## Original GitHub Issue
Reference: Create adapter implementations for multiple agent backends to enable middleware flexibility across different agent runtimes (Kitaru, Hermes, Pydantic)

## Output
Create a stacked PR with:
1. Kitaru Python adapter
2. Hermes Python adapter  
3. Pydantic Python adapter
4. Shared base classes/interfaces if useful
5. Tests for all adapters

## Notes
- Pydantic is Python-native, so it should have the most complete implementation
- Hermes may need research - create interface-first if no library exists
- Kitaru is REST-based, most straightforward implementation