"""Throwaway prototype: an agentware llm.Backend over the Bedrock Converse API.

Maps agentware's provider-neutral Message/ToolCall/Response types onto Converse
content blocks (text, toolUse, toolResult, reasoningContent). Not production code.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "python" / "src"))  # repo checkout, read-only

from haikeilabs.agentware.llm.request import Message, Role, ToolDefinition
from haikeilabs.agentware.llm.response import Response, TokenUsage, ToolCall


class StubConverseClient:
    """Scripted stand-in for boto3 bedrock-runtime, used when BEDROCK_STUB=1."""

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        msgs = kwargs["messages"]
        last = msgs[-1]["content"]
        if any("toolResult" in b for b in last):
            res = next(b["toolResult"] for b in last if "toolResult" in b)
            text = res["content"][0].get("text", "")
            return _resp([{"text": f"(stub) tool said: {text}"}], "end_turn")
        prompt = last[0].get("text", "").lower()
        tool = "github_delete_repo" if "delete" in prompt else "get_weather"
        args = {"owner": "acme", "repo": "prod"} if tool == "github_delete_repo" else {"city": "Paris"}
        return _resp(
            [
                {"reasoningContent": {"reasoningText": {"text": f"(stub) I should call {tool}.", "signature": "stub"}}},
                {"toolUse": {"toolUseId": "tu-1", "name": tool, "input": args}},
            ],
            "tool_use",
        )


def _resp(content: list[dict[str, Any]], stop: str) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop,
        "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
    }


class BedrockConverseBackend:
    """Satisfies haikeilabs.agentware.llm.backend.Backend (structural Protocol)."""

    def __init__(
        self,
        model_id: str,
        tools: list[ToolDefinition],
        client: Any = None,
        thinking_budget: int = 0,
        max_tokens: int = 2048,
    ) -> None:
        self._model_id = model_id
        self._tools = tools
        self._max_tokens = max_tokens
        self._thinking_budget = thinking_budget
        if client is None:
            if os.environ.get("BEDROCK_STUB") == "1":
                client = StubConverseClient()
            else:
                import boto3

                client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        self._client = client
        # GAP FOUND: agentware's Message has no slot for provider-opaque blocks.
        # Claude extended thinking + tool use REQUIRES the prior assistant turn's
        # signed reasoning block to be sent back verbatim, so we keep the raw
        # Converse content keyed by the Message object identity.
        self._raw_assistant: dict[int, list[dict[str, Any]]] = {}

    # --- Backend protocol -------------------------------------------------
    def model_name(self) -> str:
        return self._model_id

    def supports_native_tool_calling(self) -> bool:
        return True

    def context_window_size(self) -> int:
        return 0

    def complete(self, messages: list[Message]) -> Response:
        system = [{"text": m.content} for m in messages if m.role == Role.SYSTEM]
        conv = self._to_converse(messages)
        kwargs: dict[str, Any] = {
            "modelId": self._model_id,
            "messages": conv,
            "inferenceConfig": {"maxTokens": self._max_tokens},
            "toolConfig": {
                "tools": [
                    {"toolSpec": {"name": t.name, "description": t.description, "inputSchema": {"json": t.input_schema}}}
                    for t in self._tools
                ]
            },
        }
        if system:
            kwargs["system"] = system
        if self._thinking_budget:
            # Anthropic-on-Bedrock extended thinking goes through the
            # provider passthrough field; output comes back normalized as
            # reasoningContent blocks.
            kwargs["additionalModelRequestFields"] = {
                "thinking": {"type": "enabled", "budget_tokens": self._thinking_budget}
            }
        extra = os.environ.get("ADDITIONAL_FIELDS")
        if extra:
            kwargs["additionalModelRequestFields"] = json.loads(extra)
        out = self._client.converse(**kwargs)
        content = out["output"]["message"]["content"]
        text, reasoning, calls = [], [], []
        for block in content:
            if "text" in block:
                text.append(block["text"])
            elif "reasoningContent" in block:
                rt = block["reasoningContent"].get("reasoningText")
                if rt:
                    reasoning.append(rt.get("text", ""))
            elif "toolUse" in block:
                tu = block["toolUse"]
                calls.append(ToolCall(id=tu["toolUseId"], name=tu["name"], arguments=tu.get("input") or {}))
        usage = out.get("usage", {})
        resp = Response(
            content="".join(text),
            reasoning="\n".join(reasoning),
            tool_calls=calls,
            finish_reason=out.get("stopReason", ""),
            usage_tokens=TokenUsage(
                prompt_tokens=usage.get("inputTokens", 0),
                completion_tokens=usage.get("outputTokens", 0),
                total_tokens=usage.get("totalTokens", 0),
            ),
        )
        resp._raw_content = content  # type: ignore[attr-defined]  # prototype hack
        return resp

    def remember(self, assistant_msg: Message, resp: Response) -> None:
        self._raw_assistant[id(assistant_msg)] = getattr(resp, "_raw_content", [])

    # --- mapping ------------------------------------------------------------
    def _to_converse(self, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == Role.SYSTEM:
                continue
            if m.role == Role.USER:
                out.append({"role": "user", "content": [{"text": m.content}]})
            elif m.role == Role.ASSISTANT:
                raw = self._raw_assistant.get(id(m))
                if raw is None:
                    raw = ([{"text": m.content}] if m.content else []) + [
                        {"toolUse": {"toolUseId": c.id, "name": c.name, "input": c.arguments}} for c in m.tool_calls
                    ]
                out.append({"role": "assistant", "content": raw})
            elif m.role == Role.TOOL:
                payload = json.loads(m.content)
                block = {
                    "toolResult": {
                        "toolUseId": m.tool_call_id,
                        "content": [{"text": payload["text"]}],
                        "status": "success" if payload["ok"] else "error",
                    }
                }
                # Converse wants all toolResults for one assistant turn in one user message.
                if out and out[-1]["role"] == "user" and any("toolResult" in b for b in out[-1]["content"]):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out
