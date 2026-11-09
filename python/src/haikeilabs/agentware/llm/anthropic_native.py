"""Native Anthropic content-block formatting. Signed blocks are opaque."""

from copy import deepcopy
from typing import Any

from .request import Message, Role, ToolDefinition
from .response import Response, ToolCall


def parse_anthropic_response(data: dict[str, Any]) -> Response:
    """Parse native content blocks, retaining preceding blocks per tool call."""
    calls: list[ToolCall] = []
    pending: list[dict[str, Any]] = []
    text: list[str] = []
    for block in data.get("content", []):
        if block.get("type") == "tool_use":
            if (
                not isinstance(block.get("id"), str)
                or not block.get("id")
                or not isinstance(block.get("name"), str)
                or not block.get("name")
                or not isinstance(block.get("input"), dict)
            ):
                raise ValueError("invalid Anthropic tool_use")
            call = ToolCall(id=block["id"], name=block["name"], arguments=deepcopy(block["input"]))
            if pending:
                call.metadata = {"anthropic": {"before": pending, "after": []}}
            calls.append(call)
            pending = []
        else:
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text.append(block["text"])
            pending.append(deepcopy(block))
    if calls and pending:
        meta = calls[-1].metadata or {"anthropic": {"before": [], "after": []}}
        meta["anthropic"]["after"] = pending
        calls[-1].metadata = meta
    return Response(
        content="".join(text), tool_calls=calls, finish_reason=data.get("stop_reason") or ""
    )


def format_anthropic_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """Format the Anthropic tools parameter."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema or {"type": "object", "properties": {}},
        }
        for tool in tools
    ]


def format_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Format assistant calls and grouped user tool results.

    System instructions belong in the top-level ``system`` parameter.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == Role.SYSTEM:
            raise ValueError("Anthropic system instructions must be top-level")
        if message.role == Role.TOOL:
            if not message.tool_call_id:
                raise ValueError("tool_result requires tool_call_id")
            block = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.content,
            }
            previous = out[-1] if out else None
            if (
                previous
                and previous["role"] == "user"
                and isinstance(previous["content"], list)
                and all(b.get("type") == "tool_result" for b in previous["content"])
            ):
                previous["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif message.role == Role.ASSISTANT and message.tool_calls:
            blocks: list[dict[str, Any]] = []
            saved_blocks = any(
                isinstance(c.metadata, dict) and "anthropic" in c.metadata
                for c in message.tool_calls
            )
            if not saved_blocks and message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                saved = call.metadata.get("anthropic") if isinstance(call.metadata, dict) else None
                if saved:
                    blocks.extend(deepcopy(saved["before"]))
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": deepcopy(call.arguments),
                    }
                )
                if saved:
                    blocks.extend(deepcopy(saved["after"]))
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": message.role.value, "content": message.content})
    return out
