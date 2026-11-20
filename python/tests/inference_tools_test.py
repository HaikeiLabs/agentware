"""run_inference must pass the tool definitions to the backend (HAI-1107 item 3).

Mirrors the Go fix in #300: the inference loop sends the registry's tool
definitions on every completion so a native tool-calling backend can offer
them to the model. Third-party backends whose complete() predates the tools
parameter keep working (no tools kwarg, one warning).
"""

import logging

import pytest

from haikeilabs.agentware.llm import Message, Role
from haikeilabs.agentware.llm.request import ToolDefinition
from haikeilabs.agentware.llm.response import Response, TokenUsage
from haikeilabs.agentware.middleware.inference import InferenceConfig, run_inference


class RecordingBackend:
    """Records every complete() call, like the Go scriptedBackend in #300."""

    def __init__(self) -> None:
        self.complete_calls: list[dict] = []

    def complete(self, messages, tools=None):
        self.complete_calls.append({"messages": messages, "tools": tools})
        return Response(content="done", usage_tokens=TokenUsage(1, 1, 2))

    def supports_native_tool_calling(self) -> bool:
        return True

    def model_name(self) -> str:
        return "mock"

    def context_window_size(self) -> int:
        return 0


class OldStyleBackend:
    """A third-party backend whose complete() predates the tools parameter."""

    def __init__(self) -> None:
        self.messages_seen: list[list[Message]] = []

    def complete(self, messages: list[Message]) -> Response:
        self.messages_seen.append(messages)
        return Response(content="done", usage_tokens=TokenUsage(1, 1, 2))

    def supports_native_tool_calling(self) -> bool:
        return False

    def model_name(self) -> str:
        return "legacy"

    def context_window_size(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_run_inference_tolerates_backend_without_tools_parameter(caplog):
    """An old-style backend still runs, without tools and with one warning."""
    tool_def = ToolDefinition(
        name="hn_top_stories",
        description="List top stories",
        input_schema={"type": "object", "properties": {}},
    )
    backend = OldStyleBackend()
    cfg = InferenceConfig(client=backend, tool_specs=[tool_def], max_attempts=2)

    with caplog.at_level(logging.WARNING, logger="haikeilabs.agentware.middleware.inference"):
        result = await run_inference([Message(role=Role.USER, content="go")], cfg)

    assert result is not None
    assert len(backend.messages_seen) == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "hn_top_stories" in warnings[0].getMessage()
    assert "native tool" in warnings[0].getMessage().lower()


@pytest.mark.asyncio
async def test_run_inference_sends_tool_definitions_to_backend():
    """run_inference passes its tool_specs to backend.complete()."""
    tool_def = ToolDefinition(
        name="hn_top_stories",
        description="List top stories",
        input_schema={"type": "object", "properties": {}},
    )
    backend = RecordingBackend()
    cfg = InferenceConfig(client=backend, tool_specs=[tool_def], max_attempts=2)

    result = await run_inference([Message(role=Role.USER, content="summarize")], cfg)

    assert result is not None
    assert len(backend.complete_calls) == 1
    assert backend.complete_calls[0]["tools"] == [tool_def]
