"""Cross-language contract tests for the executor agent loop."""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from haikeilabs.agentware.executor.executor import (
    ExecuteRequest,
    InferenceExecutor,
    InferenceExecutorConfig,
)
from haikeilabs.agentware.llm import Message, Role
from haikeilabs.agentware.llm.request import ToolDefinition
from haikeilabs.agentware.llm.response import Response, TokenUsage
from haikeilabs.agentware.llm.response import ToolCall as LLMToolCall
from haikeilabs.agentware.tools import ToolRegistry


@dataclass
class MockBackend:
    """Mock backend that captures the conversations it is given."""

    responses: list[Response]
    calls: list[list[Message]] = field(default_factory=list)
    call_count: int = 0

    def complete(self, messages: list[Message]) -> Response:
        self.calls.append(list(messages))
        if self.call_count >= len(self.responses):
            return self.responses[-1]
        resp = self.responses[self.call_count]
        self.call_count += 1
        return resp

    def supports_native_tool_calling(self) -> bool:
        return True

    def model_name(self) -> str:
        return "mock"

    def context_window_size(self) -> int:
        return 8192


class NoopFormatter:
    """Formatter stub; the loop under test never formats tool calls."""

    def format_tool_definitions(self, tools: list[Any]) -> str:
        return ""

    def parse_tool_calls(self, response: str) -> list[Any]:
        return []

    def format_tool_result(self, name: str, result: Any) -> str:
        return ""

    def model_family(self) -> str:
        return "generic"


class RecordingBackend:
    """Records the conversations and tool definitions it is given (#300)."""

    def __init__(self, responses: list[Response]) -> None:
        self.responses = responses
        self.calls: list[list[Message]] = []
        self.tools_seen: list[list[ToolDefinition] | None] = []
        self.call_count = 0

    def complete(self, messages: list[Message], tools=None) -> Response:
        self.calls.append(list(messages))
        self.tools_seen.append(tools)
        if self.call_count >= len(self.responses):
            return self.responses[-1]
        resp = self.responses[self.call_count]
        self.call_count += 1
        return resp

    def supports_native_tool_calling(self) -> bool:
        return True

    def model_name(self) -> str:
        return "mock"

    def context_window_size(self) -> int:
        return 0


class RecordingTool:
    """Registry tool with a schema; records executions like the Go test in #300."""

    def __init__(self) -> None:
        self.executed: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "hn_top_stories"

    @property
    def description(self) -> str:
        return "List top stories"

    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"limit": {"type": "integer"}}}

    def execute(self, args: dict[str, Any]) -> Any:
        self.executed.append(args)
        return "1. Story one"


def _executor(backend: MockBackend) -> InferenceExecutor:
    return InferenceExecutor(
        InferenceExecutorConfig(
            backend=backend,
            registry=ToolRegistry(),
            tool_executor=None,
            formatter=NoopFormatter(),
            max_iterations=5,
        )
    )


def test_system_prompt_leads_history() -> None:
    """The system prompt must lead the conversation, ahead of history."""
    history = [
        Message(role=Role.USER, content="h1"),
        Message(role=Role.ASSISTANT, content="h2"),
    ]
    backend = MockBackend(responses=[Response(content="done")])
    executor = _executor(backend)

    result = executor.execute(
        ExecuteRequest(
            system_prompt="sys",
            user_message="user",
            history=history,
        )
    )

    sent = backend.calls[0]
    assert sent[0].role == Role.SYSTEM
    assert sent[0].content == "sys"
    assert [m.content for m in sent[1:3]] == ["h1", "h2"]
    assert sent[-1].role == Role.USER
    assert sent[-1].content == "user"
    assert result.final_response == "done"


def test_native_tool_history_keeps_metadata() -> None:
    from haikeilabs.agentware.llm import ToolCall

    metadata = {"google": {"thought_signature": "fixture-signature"}}
    backend = MockBackend(
        responses=[
            Response(
                tool_calls=[ToolCall(id="call-1", name="lookup", arguments={}, metadata=metadata)]
            ),
            Response(content="done"),
        ]
    )

    class ToolExecutor:
        def execute(self, *args: Any) -> tuple[str, bool, str]:
            return ("ok", True, "")

    executor = _executor(backend)
    executor._config.tool_executor = ToolExecutor()
    executor.execute(ExecuteRequest(system_prompt="sys", user_message="go"))
    assistant = next(m for m in backend.calls[1] if m.role == Role.ASSISTANT)
    assert assistant.tool_calls[0].metadata == metadata
    assert "fixture-signature" not in repr(assistant.tool_calls[0])


def _native_executor(
    backend: RecordingBackend, tool: RecordingTool
) -> tuple[InferenceExecutor, list[tuple[str, dict[str, Any]]]]:
    """Executor over a registry holding one tool, with a recording executor."""
    registry = ToolRegistry()
    registry.register(tool)
    executed: list[tuple[str, dict[str, Any]]] = []

    class ToolExecutor:
        def execute(self, name: str, args: dict[str, Any], ctx: Any) -> tuple[str, bool, str]:
            executed.append((name, args))
            return (tool.execute(args), True, "")

    executor = InferenceExecutor(
        InferenceExecutorConfig(
            backend=backend,
            registry=registry,
            tool_executor=ToolExecutor(),
            formatter=NoopFormatter(),
            max_iterations=5,
        )
    )
    return executor, executed


def test_executor_passes_registry_tools_to_backend() -> None:
    """The executor offers the registry's tool definitions on every completion."""
    backend = RecordingBackend(responses=[Response(content="done")])
    executor, _ = _native_executor(backend, RecordingTool())

    executor.execute(ExecuteRequest(system_prompt="sys", user_message="go"))

    assert backend.tools_seen == [
        [
            ToolDefinition(
                name="hn_top_stories",
                description="List top stories",
                input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
            )
        ]
    ]


def test_executor_runs_native_tool_calls_and_keeps_ids() -> None:
    """A native tool call runs once; the tool message answers its tool_call_id."""
    backend = RecordingBackend(
        responses=[
            Response(
                tool_calls=[LLMToolCall(id="call_1", name="hn_top_stories", arguments={"limit": 5})]
            ),
            Response(content="Story one leads today."),
        ]
    )
    tool = RecordingTool()
    executor, executed = _native_executor(backend, tool)

    result = executor.execute(ExecuteRequest(system_prompt="sys", user_message="summarize"))

    assert result.termination_reason.value == "complete"
    assert result.tool_calls_made == 1
    assert executed == [("hn_top_stories", {"limit": 5})]

    second = backend.calls[1]
    assert len(second) == 4
    assistant, tool_msg = second[2], second[3]
    assert assistant.role == Role.ASSISTANT
    assert assistant.tool_calls[0].id == "call_1"
    assert assistant.tool_calls[0].name == "hn_top_stories"
    assert tool_msg.role == Role.TOOL
    assert tool_msg.tool_call_id == "call_1"
    assert tool_msg.content == "Tool hn_top_stories result: 1. Story one"


def test_executor_plain_text_answer_completes() -> None:
    """Prose from a native tool-calling backend is the final answer (#300)."""
    backend = RecordingBackend(responses=[Response(content="Here are today's top stories.")])
    executor, _ = _native_executor(backend, RecordingTool())

    result = executor.execute(ExecuteRequest(system_prompt="sys", user_message="summarize"))

    assert result.termination_reason.value == "complete"
    assert result.final_response == "Here are today's top stories."
    assert result.tool_calls_made == 0
