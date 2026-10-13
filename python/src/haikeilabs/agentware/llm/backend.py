"""LLM backend abstractions."""

from typing import TYPE_CHECKING, Protocol

from .request import Message

if TYPE_CHECKING:
    from .response import Response


class Backend(Protocol):
    """Protocol for LLM backends."""

    def complete(self, messages: list[Message]) -> "Response":
        """Send a conversation to the LLM and return its response."""
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
