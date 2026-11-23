"""OpenAI-compatible tool-call formatting and synchronous backend."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

from .request import Message, ToolDefinition
from .response import Response, TokenUsage, ToolCall


def _decode_arguments(name: str, raw: Any) -> dict[str, Any]:
    """Decode native tool-call arguments, failing closed (mirrors #300).

    Arguments arrive as a JSON string (most servers) or an object (some
    servers). Missing, empty or null arguments mean no arguments. Anything
    else that is not a JSON object raises, so a malformed call is never run
    with silently emptied arguments.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as e:
            raise ValueError(f"tool call {name!r} arguments: not valid JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise ValueError(f"tool call {name!r} arguments: not a JSON object")
        return parsed
    raise ValueError(f"tool call {name!r} arguments: not a JSON object")


def parse_tool_calls(raw: list[dict[str, Any]]) -> list[ToolCall]:
    """Parse native calls, keeping ids and opaque provider metadata.

    Fails closed (mirrors Go #300): a nameless call or arguments that are
    not a JSON object raise ValueError instead of yielding a call with
    silently emptied arguments.
    """
    calls = []
    for tc in raw:
        function = tc.get("function") or {}
        name = function.get("name")
        if not name:
            raise ValueError("tool call without a name")
        calls.append(
            ToolCall(
                id=tc.get("id", ""),
                name=name,
                arguments=_decode_arguments(name, function.get("arguments")),
                metadata=tc.get("extra_content"),
                _metadata_present="extra_content" in tc,
            )
        )
    return calls


def format_tool_call(call: ToolCall) -> dict[str, Any]:
    """Render a native call using this model family's wire format."""
    wire: dict[str, Any] = {
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.name,
            "arguments": json.dumps(call.arguments, separators=(",", ":"), ensure_ascii=False),
        },
    }
    if call.metadata is not None or call._metadata_present:
        wire["extra_content"] = call.metadata
    return wire


class OpenAIBackend:
    """Synchronous OpenAI-compatible chat backend."""

    def __init__(
        self,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        *,
        client: httpx.Client | None = None,
        context_window: int = 0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        if client is None:
            import httpx

            client = httpx.Client(timeout=60)
        self.client = client
        self.context_window = context_window

    def complete(
        self, messages: list[Message], tools: list[ToolDefinition] | None = None
    ) -> Response:
        wire: list[dict[str, Any]] = []
        for msg in messages:
            entry: dict[str, Any] = {"role": msg.role.value, "content": msg.content}
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            if msg.tool_calls:
                entry["tool_calls"] = [format_tool_call(tc) for tc in msg.tool_calls]
                if not msg.content:
                    entry["content"] = None
            wire.append(entry)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload: dict[str, Any] = {"model": self.model, "messages": wire}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in tools
            ]
        res = self.client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
        res.raise_for_status()
        body = res.json()
        choice = body["choices"][0]
        msg = choice["message"]
        usage = body.get("usage") or {}
        return Response(
            content=msg.get("content") or "",
            reasoning=msg.get("reasoning_content") or "",
            tool_calls=parse_tool_calls(msg.get("tool_calls") or []),
            finish_reason=choice.get("finish_reason") or "",
            usage_tokens=TokenUsage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            ),
        )

    def supports_native_tool_calling(self) -> bool:
        return True

    def model_name(self) -> str:
        return self.model

    def context_window_size(self) -> int:
        return self.context_window
