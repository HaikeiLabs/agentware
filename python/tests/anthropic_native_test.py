import json
from pathlib import Path

from haikeilabs.agentware.llm import Message, Role
from haikeilabs.agentware.llm.anthropic_native import (
    format_anthropic_messages,
    format_anthropic_tools,
    parse_anthropic_response,
)
from haikeilabs.agentware.llm.request import ToolDefinition


def test_anthropic_native_fixtures():
    cases = json.loads(
        (
            Path(__file__).resolve().parents[2] / "fixtures/llm/anthropic-native/cases.json"
        ).read_text()
    )
    for case in cases:
        response = parse_anthropic_response(case["response"])
        if case["name"] == "no thinking":
            assert response.tool_calls[0].metadata is None
        else:
            assert response.tool_calls[0].metadata["anthropic"]["before"]
            assert "signed+opaque" not in repr(response.tool_calls)
            assert "opaque+encrypted" not in repr(response.tool_calls)
        tools = [ToolDefinition(**tool) for tool in case["tools"]]
        assert format_anthropic_tools(tools) == case["expected_tools"], case["name"]
        messages = [Message(Role.ASSISTANT, response.content, tool_calls=response.tool_calls)] + [
            Message(Role.TOOL, "done", tool_call_id=call.id) for call in response.tool_calls
        ]
        assert format_anthropic_messages(messages) == case["expected_messages"], case["name"]
