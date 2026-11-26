"""Cross-language contract tests for the executor agent loop."""

import json
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
from haikeilabs.agentware.llm.response import Response
from haikeilabs.agentware.llm.response import ToolCall as LLMToolCall
from haikeilabs.agentware.toolformat import AnthropicFormatter, GenericFormatter, QwenFormatter
from haikeilabs.agentware.tools import Result, ToolRegistry


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

    def format_tool_result(self, name: str, result: Any, call_id: str | None = None) -> str:
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
    backend: RecordingBackend,
    tool: RecordingTool,
    formatter: Any = None,
    tool_error: str = "",
) -> tuple[InferenceExecutor, list[tuple[str, dict[str, Any]]]]:
    """Executor over a registry holding one tool, with a recording executor.

    ``formatter`` defaults to none configured, the plain-text tool message.
    A non-empty ``tool_error`` makes the tool executor report a failure.
    """
    registry = ToolRegistry()
    registry.register(tool)
    executed: list[tuple[str, dict[str, Any]]] = []

    class ToolExecutor:
        def execute(self, name: str, args: dict[str, Any], ctx: Any) -> tuple[str, bool, str]:
            executed.append((name, args))
            if tool_error:
                return (None, False, tool_error)
            return (tool.execute(args), True, "")

    executor = InferenceExecutor(
        InferenceExecutorConfig(
            backend=backend,
            registry=registry,
            tool_executor=ToolExecutor(),
            formatter=formatter,
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


class AttrSchemaTool:
    """Tool that declares its JSON schema as a plain dict attribute."""

    name = "attr_tool"
    description = "attribute-style schema tool"

    def __init__(self) -> None:
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        }

    def execute(self, args: dict[str, Any]) -> Any:
        return None


class MethodSchemaTool:
    """Tool that declares its JSON schema via a zero-argument method."""

    name = "method_tool"
    description = "method-style schema tool"

    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"query": {"type": "string"}}}

    def execute(self, args: dict[str, Any]) -> Any:
        return None


def test_executor_sends_attribute_and_method_schemas_to_backend() -> None:
    """Both schema styles reach the backend; an attribute schema is not dropped."""
    backend = RecordingBackend(responses=[Response(content="done")])
    registry = ToolRegistry()
    registry.register(AttrSchemaTool())
    registry.register(MethodSchemaTool())
    executor = InferenceExecutor(
        InferenceExecutorConfig(
            backend=backend,
            registry=registry,
            tool_executor=None,
            formatter=NoopFormatter(),
            max_iterations=5,
        )
    )

    executor.execute(ExecuteRequest(system_prompt="sys", user_message="go"))

    assert backend.tools_seen == [
        [
            ToolDefinition(
                name="attr_tool",
                description="attribute-style schema tool",
                input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
            ),
            ToolDefinition(
                name="method_tool",
                description="method-style schema tool",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            ),
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


def _tool_message(formatter: Any, tool_error: str = "") -> Message:
    """Run one native tool call and return the tool message sent back to the model."""
    backend = RecordingBackend(
        responses=[
            Response(
                tool_calls=[LLMToolCall(id="call_1", name="hn_top_stories", arguments={"limit": 5})]
            ),
            Response(content="done"),
        ]
    )
    executor, _ = _native_executor(backend, RecordingTool(), formatter, tool_error)
    executor.execute(ExecuteRequest(system_prompt="sys", user_message="summarize"))
    return backend.calls[1][3]


def test_executor_tool_message_uses_configured_formatter() -> None:
    """With a formatter configured, the tool message is its format_tool_result (mirrors Go)."""
    formatter = QwenFormatter()
    tool_msg = _tool_message(formatter)

    want = formatter.format_tool_result(
        "hn_top_stories", Result(success=True, data="1. Story one"), "call_1"
    )
    assert tool_msg.role == Role.TOOL
    assert tool_msg.tool_call_id == "call_1"
    assert tool_msg.content == want


def test_executor_tool_message_passes_call_id_to_formatter() -> None:
    """The tool call id reaches the formatter (Anthropic renders it as tool_use_id)."""
    tool_msg = _tool_message(AnthropicFormatter())

    assert json.loads(tool_msg.content) == {
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": "1. Story one",
    }


def test_executor_tool_error_uses_configured_formatter() -> None:
    """A failed tool call is formatted as an error result by the formatter."""
    formatter = GenericFormatter()
    tool_msg = _tool_message(formatter, tool_error="rate limited")

    assert tool_msg.content == formatter.format_tool_result(
        "hn_top_stories", Result(success=False, error="rate limited"), "call_1"
    )
    assert tool_msg.content == "Error: rate limited"


def test_executor_tool_error_without_formatter_keeps_plain_text() -> None:
    """With no formatter configured, a failure keeps today's plain text."""
    tool_msg = _tool_message(None, tool_error="rate limited")

    assert tool_msg.content == "Tool hn_top_stories result: rate limited"


def test_executor_plain_text_answer_completes() -> None:
    """Prose from a native tool-calling backend is the final answer (#300)."""
    backend = RecordingBackend(responses=[Response(content="Here are today's top stories.")])
    executor, _ = _native_executor(backend, RecordingTool())

    result = executor.execute(ExecuteRequest(system_prompt="sys", user_message="summarize"))

    assert result.termination_reason.value == "complete"
    assert result.final_response == "Here are today's top stories."
    assert result.tool_calls_made == 0
