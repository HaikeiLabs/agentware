"""LLM backend abstractions."""

import inspect
from typing import TYPE_CHECKING, Protocol

from .request import Message, ToolDefinition

if TYPE_CHECKING:
    from .response import Response


class Backend(Protocol):
    """Protocol for LLM backends."""

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
    ) -> "Response":
        """Send a conversation to the LLM and return its response.

        ``tools`` carries the tool definitions the backend may offer to the
        model natively (mirrors Go's ``llm.Request.Tools`` from #300).
        """
        ...

    def supports_native_tool_calling(self) -> bool:
        """Return True if backend supports native tool calling."""
        ...

    def model_name(self) -> str:
        """Return the model identifier."""
        ...

    def context_window_size(self) -> int:
        """Return the max tokens the model can process. 0 if unknown."""
        ...


def accepts_tools(backend: "Backend") -> bool:
    """Return True if the backend's complete() accepts a tools parameter.

    Third-party backends written before the tools parameter keep working:
    callers invoke them without tools instead of raising TypeError.
    """
    try:
        params = inspect.signature(backend.complete).parameters
    except (TypeError, ValueError):
        return False
    if "tools" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
