# Evals Package

A framework for evaluating LLM agents on tool-calling capabilities. Supports multiple model backends and runs evals against real models.

## Quick Start

```bash
# Python - run all evals against local llama.cpp
cd python
PYTHONPATH=src python3 -m evals.main --all --models qwen3.6-27b-mtp

# Go - run all evals
cd go && go run ./cmd/evals

# TypeScript - run all evals
cd typescript && node dist/evals/main.js
```

## CLI Options

| Flag | Description | Default |
|------|-------------|---------|
| `--file-search` | Run file search evals only | - |
| `--general` | Run general tool calling evals | - |
| `--github` | Run GitHub tool evals | - |
| `--calendar` | Run calendar tool evals | - |
| `--beta-tools` | Run beta tool surface evals | - |
| `--all` | Run all evals (default) | true |
| `--models` | Comma-separated model list | `$EVAL_MODELS` |
| `--base-url` | API base URL | `$EVAL_BASE_URL` |
| `--backend` | Model backend (Python only) | llamacpp |
| `--max-turns` | Max turns per eval | 10 |

## Beta tool surface evals

`--beta-tools` runs the model-driven cases in
`python/src/evals/cases/beta_tools.py` over the governed connector reads,
write action tools, and local capabilities.

```bash
EVAL_BASE_URL=<endpoint> EVAL_MODELS=<model-id> make evals-beta-tools
# or, choosing a backend explicitly:
EVAL_BASE_URL=<endpoint> EVAL_MODELS=<model-id> EVAL_BACKEND=llamacpp \
  make evals-beta-tools
```

Use a model id the endpoint actually serves — query `GET /v1/models` (or the
backend's equivalent) rather than assuming the defaults in this repo are
available.

### What a case asserts

Beyond the tool *name*, a case may assert arguments:

| Field | Meaning |
|---|---|
| `expected_args` | argument must be present with exactly this value |
| `required_arg_keys` | argument must be present, any value |
| `forbidden_arg_keys` | argument must **not** appear |

The runner additionally checks that arguments parse as a JSON object matching
the called tool's declared schema: every `required` property present, and no
property the schema does not declare. Reaching the expected tool with
arguments that fail any of these is a **failure**, recorded with the reason.

`forbidden_arg_keys` carries the tenancy property. Scoping the tenant-side
proxy supplies (`tenant_id`, `workspace`, `repository`, `bucket`, `drive_id`)
is absent from every connector-read schema, so a model that emits one has
tried to choose its own scope. See `docs/tenant-proxy-reference.md`.

These evals measure **model behaviour only**. Whether a call is permitted is
enforced by the middleware policy layer and pinned by deterministic tests:

```bash
cd python && pytest tests/beta_tool_authorization_test.py
```

### Blocked runs

If the endpoint is unreachable, the runner raises `EndpointUnavailableError`
during preflight and `evals.main` exits **2** with a `BLOCKED:` message. A
blocked run is never written out as a 0% score, which would be
indistinguishable from a model that failed every case. Report it as blocked.

### Where results live

This repository owns the **runner**, not run results. Do not commit scored
runs, per-case tool-call tables, or model-specific numbers here; eval output
under `python/src/evals/output/` is gitignored for that reason.

The exact beta tool inventory and its recorded results are owned by
**Kei-Chat-Harness**. Attach a run — model id, endpoint/backend, timestamp,
per-case pass/fail with the tool call and arguments — to the harness PR as
review evidence, and keep it maintained there.

## Model Backends

### Python

```python
from evals.models import ModelBackend, create_model_client

client = create_model_client(
    backend=ModelBackend.LLAMACPP,
    model="qwen3.6-27b-mtp",
    base_url="http://pedrogpt:8000"
)
```

Supported backends:

- `ollama` - Local Ollama (`http://localhost:11434`)
- `llamacpp` - llama.cpp server (`http://localhost:8000`)
- `vllm` - vLLM server
- `lmstudio` - LM Studio
- `openai` - OpenAI API (requires OPENAI_API_KEY)
- `anthropic` - Anthropic API (requires ANTHROPIC_API_KEY)

### Go

```go
runner := evals.NewEvalRunner("http://pedrogpt:8000", 10)
```

### TypeScript

```typescript
const runner = new EvalRunner("http://pedrogpt:8000", 10);
```

## Test Cases

### File Search

- `glob_python_files` - Find Python files
- `glob_md_files` - Find Markdown files
- `search_code` - Search for code patterns
- `read_file` - Read configuration files

### General

- `calculator_add` - Simple addition
- `calculator_complex` - Complex expressions
- `get_weather` - Get weather info
- `translate_english_to_spanish` - Translation
- `translate_with_source` - Translation with source language

### GitHub

- `list_prs` - List pull requests
- `list_issues` - List issues
- `create_issue` - Create an issue
- `workflow_status` - Get workflow status

### Calendar

- `schedule_meeting` - Schedule a meeting
- `list_events` - List calendar events
- `find_free_time` - Find free time slots

## Adding New Test Cases

### Python example

```python
from evals.runner import EvalCase

MY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "my_tool",
            "description": "Does something useful",
            "parameters": {
                "type": "object",
                "properties": {
                    "arg1": {"type": "string", "description": "First argument"}
                },
                "required": ["arg1"]
            }
        }
    }
]

MY_CASES = [
    EvalCase(
        name="my_tool_test",
        description="Test my tool",
        system_prompt="You are a helpful assistant with access to tools.",
        user_message="Use my_tool with arg1=value",
        tools=MY_TOOLS,
        expected_tool="my_tool",
        max_turns=10
    )
]
```

### Go example

```go
var MyTools = []ToolDefinition{
    {
        Type: "function",
        Function: ToolFunc{
            Name:        "my_tool",
            Description: "Does something useful",
            Parameters: map[string]interface{}{...},
        },
    },
}

var MyCases = []EvalCase{
    {
        Name:         "my_tool_test",
        Description:  "Test my tool",
        SystemPrompt: "You are a helpful assistant with access to tools.",
        UserMessage:  "Use my_tool with arg1=value",
        Tools:        MyTools,
        ExpectedTool: "my_tool",
        MaxTurns:     10,
    },
}
```

### TypeScript example

```typescript
const myTools: ToolDefinition[] = [
  {
    type: "function",
    function: {
      name: "my_tool",
      description: "Does something useful",
      parameters: { ... },
    },
  },
];

const myCases: EvalCase[] = [
  {
    name: "my_tool_test",
    description: "Test my tool",
    systemPrompt: "You are a helpful assistant with access to tools.",
    userMessage: "Use my_tool with arg1=value",
    tools: myTools,
    expectedTool: "my_tool",
    maxTurns: 10,
  },
];
```

## Custom Tool Executor

The tool executor function receives tool name and arguments, returns a string result:

```python
def tool_executor(tool_name: str, args: dict) -> str:
    if tool_name == "my_tool":
        # Call actual tool or return mock
        return '{"result": "success"}'
    return "Unknown tool"
```

## Agent Executor (Python)

For running agents that make multiple tool calls in a loop:

```python
from evals.models import AgentExecutor

agent = AgentExecutor(
    model_client=client,
    tool_executor=tool_executor,
    max_turns=10
)

result = agent.run(
    system_prompt="You are a helpful assistant.",
    user_message="Do something complex",
    tools=my_tools
)

print(result["tool_calls"])  # All tool calls made
print(result["turns"])       # Number of turns taken
print(result["success"])     # Whether any tool was called
```

## Output

Results are saved to `python/src/evals/output/results.json`:

```json
{
  "timestamp": "2024-01-15T10:00:00Z",
  "models": ["qwen3.6-27b-mtp"],
  "results": [
    {
      "case": "calculator_add",
      "model": "qwen3.6-27b-mtp",
      "success": true,
      "turns": 1,
      "tool_calls": [...],
      "duration_ms": 500
    }
  ]
}
```
