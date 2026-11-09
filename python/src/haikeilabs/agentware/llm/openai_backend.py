"""OpenAI-compatible tool-call formatting and synchronous backend."""

import json
from typing import Any

import httpx

from .request import Message, ToolDefinition
from .response import Response, TokenUsage, ToolCall


def parse_tool_calls(raw: list[dict[str, Any]]) -> list[ToolCall]:
    """Parse native calls and preserve opaque provider metadata."""
    calls = []
    for tc in raw:
        function = tc.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        args = function.get("arguments", "{}")
        try:
            parsed = json.loads(args) if isinstance(args, str) else args
        except (ValueError, TypeError):
            parsed = {}
        calls.append(
            ToolCall(
                id=tc.get("id", ""),
                name=name,
                arguments=parsed if isinstance(parsed, dict) else {},
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
        self.client = client or httpx.Client(timeout=60)
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
