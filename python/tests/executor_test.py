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
from haikeilabs.agentware.llm.response import Response
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
