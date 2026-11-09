"""LLM package - LLM backend abstractions and types."""

from .anthropic_native import (
    format_anthropic_messages,
    format_anthropic_tools,
    parse_anthropic_response,
)
from .backend import Backend
from .openai_backend import OpenAIBackend
from .request import Message, Request, Role
from .response import Response, ToolCall

__all__ = [
    "format_anthropic_messages",
    "format_anthropic_tools",
    "parse_anthropic_response",
    "OpenAIBackend",
    "Backend",
    "Response",
    "Message",
    "Request",
    "Role",
    "ToolCall",
]
