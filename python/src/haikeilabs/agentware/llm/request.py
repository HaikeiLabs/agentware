"""LLM request types."""

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .response import ToolCall

from haikeilabs.agentware.middleware.types import MessageMeta


class Role(str, Enum):
    """Message role."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class Message:
    """A single turn in a conversation."""

    role: Role
    content: str = ""
    tool_call_id: str = ""
    tool_calls: list["ToolCall"] = field(default_factory=list)
    meta: MessageMeta = field(default_factory=MessageMeta)


@dataclass
class ToolDefinition:
    """Schema for a tool, used in native tool calling."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class Request:
    """Input to a completion."""

    messages: list[Message] = field(default_factory=list)
    tools: list[ToolDefinition] = field(default_factory=list)
    temperature: float = 0.7
    max_tokens: int = 2048
    stop: list[str] = field(default_factory=list)
