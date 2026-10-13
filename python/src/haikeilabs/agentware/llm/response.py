"""LLM response types."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """A structured tool invocation from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenUsage:
    """Token usage counts."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class Response:
    """Output from a completion."""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage_tokens: TokenUsage = field(default_factory=TokenUsage)
