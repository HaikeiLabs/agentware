# Evals Package

A framework for evaluating LLM agents on tool-calling capabilities. Supports multiple model backends and runs evals against real models.

## Quick Start

```bash
# Python - table-test suites (canonical; see "Table-test suites" below)
cd python
PYTHONPATH=src python3 -m evals.main --suite ../evals/suites \
  --model-profile deepseek-v4-flash --out /tmp/eval-out --jobs 1

# Python - legacy case modules against local llama.cpp
PYTHONPATH=src python3 -m evals.main --all --models qwen3.6-27b-mtp

# Go - run all evals
cd go && go run ./cmd/evals

# TypeScript - run all evals
cd typescript && node dist/evals/main.js
```

## Table-test suites (canonical runner)

`python/src/evals` is the canonical runner for Haikei agent and skill table
tests (contract EV-C1). There is no LLM grader. A suite is a JSON file of
cases, each mapping a prompt to an expected outcome. Every case is checked
deterministically and counted as pass or fail. The local target is 90% or
better per suite per model; the CI gate fails a suite below 95%.

```bash
cd python && pip install -e ".[evals]"   # PyYAML for model profiles
export EVAL_DEEPSEEK_BASE_URL=<openai-compatible base URL incl. /v1>
PYTHONPATH=src python3 -m evals.main \
  --suite ../evals/suites/agentware.beta-tools.json \
  --model-profile deepseek-v4-flash \
  --out ../evals/results/$(date +%F)-deepseek-v4-flash \
  --jobs 1 --threshold 0.95
# or: make evals-suite EVAL_PROFILE=qwen3.8-27b
```

| Flag | Meaning | Default |
|---|---|---|
| `--suite` | Suite file, or a directory of `*.json` suites (repeatable) | required |
| `--model-profile` | Profile name in the profiles file | `$EVAL_MODEL_PROFILE` |
| `--profiles` | Profiles YAML | `./evals/model-profiles.yaml`, else agentware's |
| `--out` | Directory for `benchmark.json` + `benchmark.md` | required |
| `--threshold` | Minimum pass rate for every suite | `0.95` |
| `--jobs` | Cases in flight, capped by the profile's `concurrency` | `1` |
| `--max-turns` | Tool-loop turns per case | `10` |
| `--timeout` | Per-request model timeout, seconds | `600` |
| `--max-tokens` | Completion budget per turn; a reply cut off here fails as truncated | `1024` |
| `--case` | Run only this case id (repeatable), e.g. to re-run failures | all |
| `--transcripts` | JSONL of tool calls + final text per run (diagnostics, scrubbed) | off |

Exit codes: `0` every suite meets the threshold; `1` at least one suite is
below it; `2` the run did not execute. A `2` covers a missing profile env
var, an unreachable endpoint, an unknown profile, or an invalid suite, and
no benchmark is written. A blocked run is never scored as 0%.

### Suite format (`agentware.eval-suite.v1`)

Suites live in the repo that owns the agent, at
`<repo>/evals/suites/<suite>.json`. The reference suite is
[`evals/suites/agentware.beta-tools.json`](../../evals/suites/agentware.beta-tools.json).

```json
{
  "schema": "agentware.eval-suite.v1",
  "suite": "kei-agents.pedro",
  "kind": "agent",
  "system_prompt": "...",
  "tools": [{"type": "function", "function": {"name": "file_bug", "parameters": {}}}],
  "repeats": 1,
  "cases": [{
    "id": "file-bug-basic",
    "prompt": "...",
    "context": {"role": "member", "groups": ["default"], "allowed_tools": ["file_bug"]},
    "expect": {
      "tool": "file_bug",
      "args": {"team_key": "KEI"},
      "required_arg_keys": ["title"],
      "forbidden_arg_keys": ["tenant_id"],
      "forbidden_tools": ["crm_get_lead"],
      "deny": false,
      "content": {"contains_all": [], "contains_any": [], "regex": [], "not_contains": []}
    }
  }]
}
```

Validation is strict. The loader rejects any of these before a model is
called:

- unknown keys, except inside `context` and tool `function` objects
- wrong types
- case ids not matching `^[a-z0-9][a-z0-9._-]{0,63}$`
- duplicate case ids or suite names
- an `expect.tool` that is not a suite tool
- arg assertions without `expect.tool`
- an invalid regex
- having both or neither of `system_prompt` and `system_prompt_file`

`system_prompt_file` is a relative path and resolves against the suite file's
directory.

### How a case is scored

The runner drives a tool loop at temperature 0. Each extra turn re-sends
the whole prompt, so a case stops at its **first decisive event**:

- A case with `"tool": "x"`, or with no `tool` key, is graded on the first
  assistant message: its tool calls, or its text if it made none. No
  further turns run.
- A `"tool": null` case stops at the first final text.
- A `deny: true` case feeds the first DENY back and gets exactly one more
  turn, whose reply the content checks grade.
- Any other case with `content` checks runs until a reply without tool
  calls, or until `--max-turns`.

A reply cut off at `--max-tokens` with no tool call fails, and its reason
says it was truncated. The system prompt and tools are byte-identical
across a suite's cases. llama.cpp requests send `cache_prompt: true`, and
vLLM uses its automatic prefix cache, so the shared prefix is cached.

 Every tool call goes through agentware
`MiddlewareImpl` and an `InMemoryAuditor`, as an untrusted `CallerContext`
built from `context.role` and `context.groups`. Caller context is never
injected into the prompt. Tool results come from mock executors: the beta
connector mocks for those names, otherwise a generic JSON success stub.
Suites carry no tool results.

| `expect` field | Rule |
|---|---|
| `tool` absent | No assertion on tool calls. |
| `"tool": null` | The model must not call any tool. |
| `"tool": "x"` | The **first** call must be `x`. Its args must satisfy the tool schema (schema-`required` present, nothing undeclared) plus `args`, `required_arg_keys` and `forbidden_arg_keys`. The call must not be denied. |
| `forbidden_tools` | Never called or attempted in any turn the case runs (see the stop rules above). |
| `content` | Case-insensitive checks on the final reply. `contains_all` needs every phrase; `contains_any` needs at least one; `regex` needs every pattern to match; `not_contains` allows none. A final reply is required. |
| `deny: true` | See below. |

**Deny.** When `context.allowed_tools` is present, the runner builds an
agentware `Policy`: an allow rule for exactly those tools, default-deny for
everything else. A disallowed call is refused by the middleware and is never
executed. The model receives this tool result:

```json
{"error": "denied", "reason": "denied by policy: ...", "source": "policy"}
```

The loop then continues. A `deny: true` case requires `allowed_tools`, and
`expect.tool` must not be in that list. The case passes when all of these
hold:

- every attempted call to a non-allowed tool was denied, or the model made
  no such attempt (it refused or used allowed tools only)
- if `expect.tool` was attempted, its arguments are valid
- the `content` checks pass (use `not_contains` for claims of success)

**Repeats.** Each case runs `repeats` times and passes only if every repeat
passes. A model or transport exception counts in `errors`, not `failed`.

### Model profiles

[`evals/model-profiles.yaml`](../../evals/model-profiles.yaml) is the
canonical profile file; other repos copy its shape. A profile names the
*environment variable* that holds the endpoint (`base_url_env`), never the
URL, so endpoints, tailnet IPs and hostnames stay out of public repos.

| Profile | Backend | Model | Env | Concurrency |
|---|---|---|---|---|
| `deepseek-v4-flash` | vllm | `deepseek-ai/DeepSeek-V4-Flash` | `EVAL_DEEPSEEK_BASE_URL` | 4 |
| `qwen3.8-27b` | llamacpp | `qwen3.8-27b` | `EVAL_QWEN_BASE_URL` | 2 |

Add a model by adding one entry. The endpoints are shared with other
workers, so use `--jobs 1` unless you hold the host.

### Benchmark output (`haikei.eval-benchmark.v1`)

```json
{"schema": "haikei.eval-benchmark.v1", "harness": "agentware",
 "model_profile": "deepseek-v4-flash", "model": "...", "created_at": "ISO", "git_sha": "...",
 "results": [{"suite": "...", "kind": "agent", "passed": 0, "failed": 0, "errors": 0,
              "total": 0, "pass_rate": 0.0, "cases": [{"id": "...", "passed": true, "reason": "", "duration_ms": 0}]}]}
```

`git_sha` is the runner checkout's HEAD, read when the run **starts**. It
gets a `-dirty` suffix when tracked files differ from HEAD. Untracked
files don't count, so results written into the checkout don't trigger it.
`cases[].duration_ms` is the case's wall time summed across repeats. It
is an additive v1 field (an EV-C1 contract change); consumers ignore
fields they don't know. `benchmark.md` holds the same numbers as a table,
with the mean case time, plus the failing cases with
their reasons. Reasons are scrubbed of URLs, IPv4 addresses and `*.ts.net`
hostnames. Committed results go in
`evals/results/<YYYY-MM-DD>-<model_profile>/benchmark.{json,md}`.

## Legacy CLI Options

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
    base_url="http://localhost:8000"
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
runner := evals.NewEvalRunner("http://localhost:8000", 10)
```

### TypeScript

```typescript
const runner = new EvalRunner("http://localhost:8000", 10);
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
