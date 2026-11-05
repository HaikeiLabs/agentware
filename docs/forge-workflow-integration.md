# Optional Forge workflow quality integrations

Agentware's Python SDK can use the upstream `forge-guardrails==0.10.1` package through the optional `forge` extra. Forge 0.10.1 requires Python 3.12 or later; Agentware's base package remains compatible with Python 3.10+. Install `haikeilabs-agentware[forge-pydantic]` for the Pydantic AI wrapper, or `haikeilabs-agentware[forge-adk]` for the Google ADK `FunctionTool` wrapper.

Forge supplies response validation and rescue, required-step handling, retries, and prerequisite checks. Its bundled facade does not itself check prerequisites, so the integration also uses Forge's `StepEnforcer`. The integration creates separate state per `ForgeSession`; applications must create and resolve one session per workflow run, not a shared global instance. Calls through `run_tool` are serialized within that session. Direct `check_call` / `record_success` users must serialize access themselves.

## Boundaries

These are **quality checks**, not authorization. A quality result of `execute` says only that a call passed the quality checks. The wrapped tool must still go through the existing policy and audit boundary; an allowed decision does not guarantee that downstream execution succeeded. Configure a `succeeded` predicate whenever a tool represents failures as normal return values. The ADK wrapper treats nonempty `error` fields as failures by default. Verify exact types, methods, and downstream failure behavior against the source and tests of the framework and Agentware port you use. For governed calls, preserve fail-closed authorization behavior. The audit stores a SHA-256 digest rather than raw or full tool arguments.

`check_text()` accepts a complete model text response and exposes Forge's rescue result. Response rescue must run at the model-response boundary; a tool-only wrapper cannot rescue output that the framework never dispatches as a tool call. The caller must check each rescued call's prerequisites through `check_call()` before execution. For argument-matched prerequisites, configure `executed_args` to return the actual arguments after any policy filtering; without this evidence, `run_tool` does not record the matching arguments and a dependent call remains blocked. A direct `record_success` caller must likewise supply post-policy arguments. Because framework tool wrappers check proposals before the wrapped authorization boundary, argument-matched prerequisites cannot guarantee post-filter semantics for the proposed call. If filtering can change a matched field, perform those checks inside the governed boundary after filtering. Quality checks must not be used as authorization. Preserve the returned nudge role when mapping feedback into the framework's messages.

## Python Pydantic AI

Wrap the actual toolset registered with the agent, not just the agent's outer `run` method. In this sketch `toolset` contains tools that already enforce the application's policy:

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from haikeilabs.agentware.integrations.forge import ForgeSession
from haikeilabs.agentware.integrations.pydantic_forge import ForgeToolset

@dataclass
class RunDeps:
    quality: ForgeSession

wrapped = ForgeToolset(toolset, session_for=lambda ctx: ctx.deps.quality)
agent = Agent(model, deps_type=RunDeps, toolsets=[wrapped])
# Create this per run; do not reuse it for another user or task.
deps = RunDeps(ForgeSession(["read", "write"], prerequisites={"write": ["read"]}))
result = await agent.run("...", deps=deps)
```

For an executable, provider-free example, see [`forge_pydantic_template.py`](../python/examples/forge_pydantic_template.py).

Nonfatal quality rejections become `pydantic_ai.ModelRetry`; fatal exhaustion stops the workflow. Verify that **all** agent tools requiring checks come from the wrapped toolset; separately registered tools bypass it. This wrapper is tested against Pydantic AI 2.45.0.

## Python Google ADK

`ForgeFunctionTool` wraps ADK `FunctionTool`, preserves its function declaration, and returns recoverable rejections as tool errors. Build a separate session and tool list for each run:

```python
from google.adk.tools import FunctionTool
from haikeilabs.agentware.integrations.forge import ForgeSession
from haikeilabs.agentware.integrations.adk_forge import ForgeFunctionTool

def build_tools_for_run(governed_read, governed_write):
    read_tool = FunctionTool(governed_read)
    write_tool = FunctionTool(governed_write)
    quality = ForgeSession(
        [read_tool.name, write_tool.name],
        prerequisites={write_tool.name: [read_tool.name]},
    )
    return [ForgeFunctionTool(tool, lambda tool_context: quality)
            for tool in (read_tool, write_tool)]
```

The function names must match the registered tool names. The wrapper supports ADK `FunctionTool` only, not built-in tools with custom request processing. It was tested against Google ADK 2.13.0. Ensure the factory itself is invoked for each run; a tool list reused across sessions would share state.

Existing Hermes and Kitaru adapters wrap CLI or remote flow execution, not individual inner tool calls. They cannot transparently receive per-tool checks through this wrapper. Integrate `ForgeSession` at a tool-call boundary owned by those harnesses; do not claim their inner tools are covered until that boundary is wired and tested.

## Context and progress

Forge's `ContextManager` and `TieredCompact` are available through `make_context_manager`. Tool wrappers do not own framework conversation history and do not automatically compact it. A harness that uses Forge `Message` objects may explicitly call `compact_forge_messages` at a completed tool-batch boundary. This helper validates call/result pairing before and after compaction and refuses invalid history; Forge 0.10.1's strategy can drop result messages or their call IDs. Handle that refusal without resending an invalid history, and retain a separate overall context limit.

A language-neutral `ProgressTracker` is available in Python, Go, and TypeScript. A Rust implementation is prepared in a separate follow-up. It tracks application-verified named requirements, invalidation, and a consecutive no-progress budget; it does not infer that an API response proves the desired outcome and it does not finish a workflow. In Python, an optional `ProgressTracker` and `observe_result` callback on `ForgeSession` let an application record validated observations after successful execution. The observation callback receives post-policy arguments only when `executed_args` is supplied; otherwise it receives an empty argument object so it cannot mistake proposed values for executed values; `finish_nudge()` suggests a terminal call only when the tracker is ready. The application still decides how to deliver the nudge. If an observation callback fails, tool execution may already have happened: surface the error without automatically retrying the tool. Other ports expose the tracker as a building block for their harnesses. Keep a separate total turn/time/cost limit. Shared examples replay `fixtures/workflow/progress.v1.json` in each port.

Go and TypeScript retain their existing guardrail and compaction implementations. The separate Rust follow-up should document exactly which behaviors it supports; do not assume parity from matching names.
