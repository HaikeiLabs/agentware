"""
Model backends and Agent executor for evals.
"""

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ModelBackend(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    VLLM = "vllm"
    LMSTUDIO = "lmstudio"
    LLAMACPP = "llamacpp"
    CUSTOM = "custom"


@dataclass
class ModelResult:
    content: str
    tool_calls: list[dict[str, Any]]
    finish_reason: str
    usage: dict[str, int]


class BaseModelClient(ABC):
    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> ModelResult: ...


class OpenAIClient(BaseModelClient):
    def __init__(
        self,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        timeout: int = 60,
    ):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.timeout = timeout

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> ModelResult:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if tools:
            payload["tools"] = tools

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8')}")

        choice = data["choices"][0]

        tool_calls = []
        if choice["message"].get("tool_calls"):
            for tc in choice["message"]["tool_calls"]:
                tool_calls.append(
                    {
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    }
                )

        return ModelResult(
            content=choice["message"].get("content", ""),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", ""),
            usage=data.get("usage", {}),
        )


class AnthropicClient(BaseModelClient):
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
        timeout: int = 60,
    ):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = base_url
        self.timeout = timeout

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> ModelResult:
        system_msg = ""
        filtered_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                system_msg = msg.get("content", "")
            else:
                filtered_messages.append(msg)

        anthropic_messages = []
        for msg in filtered_messages:
            if msg.get("tool_calls"):
                continue
            role = msg.get("role", "user")
            if role == "tool":
                role = "user"
            anthropic_messages.append({"role": role, "content": msg.get("content", "")})

        payload = {
            "model": self.model,
            "messages": anthropic_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "system": system_msg,
        }

        if tools:
            payload["tools"] = tools

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        req = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8')}")

        content = ""
        tool_calls = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                content += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    }
                )

        return ModelResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason", ""),
            usage=data.get("usage", {}),
        )


class OllamaClient(BaseModelClient):
    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434",
        timeout: int = 60,
    ):
        self.model = model
        self.base_url = base_url
        self.timeout = timeout

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> ModelResult:
        ollama_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                ollama_messages.append({"role": "system", "content": msg.get("content", "")})
            elif msg.get("tool_calls"):
                for tc in msg.get("tool_calls", []):
                    ollama_messages.append(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": tc.get("id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": tc.get("name", ""),
                                        "arguments": tc.get("arguments", ""),
                                    },
                                }
                            ],
                        }
                    )
            elif msg.get("role") == "tool":
                ollama_messages.append(
                    {"role": "user", "content": f"[tool result]: {msg.get('content', '')}"}
                )
            else:
                ollama_messages.append(msg)

        payload = {
            "model": self.model,
            "messages": ollama_messages,
            "temperature": temperature,
            "options": {"num_predict": max_tokens},
            "stream": False,
        }

        if tools:
            payload["tools"] = tools

        headers = {"Content-Type": "application/json"}

        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8')}")

        message = data.get("message", {})
        content = message.get("content", "")

        tool_calls = []
        if message.get("tool_calls"):
            for tc in message["tool_calls"]:
                tool_calls.append(
                    {
                        "id": tc.get("id", ""),
                        "name": tc.get("function", {}).get("name", ""),
                        "arguments": tc.get("function", {}).get("arguments", ""),
                    }
                )

        return ModelResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason=data.get("done", False) and "stop" or "",
            usage={},
        )


class VLLMClient(BaseModelClient):
    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:8000/v1",
        api_key: str = "EMPTY",
        timeout: int = 60,
    ):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> ModelResult:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if tools:
            payload["tools"] = tools

        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8')}")

        choice = data["choices"][0]

        tool_calls = []
        if choice["message"].get("tool_calls"):
            for tc in choice["message"]["tool_calls"]:
                tool_calls.append(
                    {
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    }
                )

        return ModelResult(
            content=choice["message"].get("content", ""),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", ""),
            usage=data.get("usage", {}),
        )


class LMStudioClient(VLLMClient):
    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:1234/v1",
        timeout: int = 60,
    ):
        super().__init__(model, base_url, api_key="not-needed", timeout=timeout)


class LlamaCPPClient(VLLMClient):
    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:8000",
        timeout: int = 60,
    ):
        super().__init__(model, base_url, api_key="", timeout=timeout)

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> ModelResult:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if tools:
            payload["tools"] = tools
        # Reuse the KV cache for the shared prefix (system prompt + tools)
        # across turns and cases; llama.cpp ignores it where unsupported.
        payload["cache_prompt"] = True

        headers = {"Content-Type": "application/json"}

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raise Exception(f"HTTP {e.code}: {e.read().decode()}")

        data = json.loads(resp.read().decode())

        tool_calls = []
        content = ""
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        content = msg.get("content", "")

        for tc in msg.get("tool_calls", []):
            tool_calls.append(
                {
                    "id": tc.get("id", ""),
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": tc.get("function", {}).get("arguments", ""),
                }
            )

        return ModelResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", ""),
            usage=data.get("usage", {}),
        )


def create_model_client(
    backend: ModelBackend,
    model: str,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: int = 60,
) -> BaseModelClient:
    if backend == ModelBackend.OPENAI:
        return OpenAIClient(model, base_url or "https://api.openai.com/v1", api_key, timeout)
    elif backend == ModelBackend.ANTHROPIC:
        return AnthropicClient(model, api_key, base_url or "https://api.anthropic.com", timeout)
    elif backend == ModelBackend.OLLAMA:
        return OllamaClient(model, base_url or "http://localhost:11434", timeout)
    elif backend == ModelBackend.VLLM:
        return VLLMClient(
            model, base_url or "http://localhost:8000/v1", api_key or "EMPTY", timeout
        )
    elif backend == ModelBackend.LMSTUDIO:
        return LMStudioClient(model, base_url or "http://localhost:1234/v1", timeout)
    elif backend == ModelBackend.LLAMACPP:
        return LlamaCPPClient(model, base_url or "http://localhost:8000", timeout)
    else:
        return OpenAIClient(model, base_url or "http://localhost:8080/v1", api_key, timeout)


ToolExecutor = Callable[[str, dict[str, Any]], str]


class AgentExecutor:
    """Agent that calls LLM and executes tool calls.

    For the harness contract, see ``docs/harness-contract.md``.
    """

    def __init__(
        self,
        model_client: BaseModelClient,
        tool_executor: ToolExecutor,
        max_turns: int = 10,
    ):
        self.model_client = model_client
        self.tool_executor = tool_executor
        self.max_turns = max_turns

    def run(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        Run agent with given prompt and tools.
        Returns dict with content, tool_calls, turns, success.
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        tool_calls_made: list[dict[str, Any]] = []
        turns = 0
        result = None

        while turns < self.max_turns:
            result = self.model_client.complete(messages, tools, 0.0, 2048)
            turns += 1

            if not result.tool_calls:
                break

            for tc in result.tool_calls:
                tool_calls_made.append(
                    {"turn": turns, "name": tc["name"], "arguments": tc["arguments"]}
                )

                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                tool_result = self.tool_executor(tc["name"], args)

                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["name"], "arguments": tc["arguments"]},
                            }
                        ],
                    }
                )
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_result})

        return {
            "content": result.content if result else "",
            "tool_calls": tool_calls_made,
            "turns": turns,
            "success": len(tool_calls_made) > 0,
        }
