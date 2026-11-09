"""LLM response types."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """A structured tool invocation from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # Opaque model-family metadata. Do not log or display it.
    metadata: Any = field(default=None, repr=False)
    _metadata_present: bool = field(default=False, repr=False, compare=False)


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
