"""Context manager for conversation history."""

from dataclasses import dataclass
from typing import Any, Protocol

from ..llm import Message


@dataclass
class ToolResultEntry:
    """Pair a tool call with its result."""

    call_id: str
    tool_name: str
    args: dict[str, Any]
    output: str
    success: bool


class ContextManager(Protocol):
    """Protocol for context managers."""

    def append_prompt(self, job_id: str, msg: Message) -> None:
        """Record an outbound prompt message."""
        ...

    def append_response(self, job_id: str, msg: Message) -> None:
        """Record an inbound LLM response."""
        ...

    def append_tool_calls(self, job_id: str, calls: list[dict[str, Any]]) -> None:
        """Record parsed tool calls for this round."""
        ...

    def append_tool_results(self, job_id: str, results: list[ToolResultEntry]) -> None:
        """Record tool execution results."""
        ...

    def get_history(self, job_id: str) -> list[Message]:
        """Reconstruct full message history for a job."""
        ...

    def purge(self, job_id: str) -> None:
        """Delete all context for a job."""
        ...


class InMemoryContextManager:
    """In-memory implementation of ContextManager."""

    def __init__(self) -> None:
        self._history: dict[str, list[Message]] = {}

    def append_prompt(self, job_id: str, msg: Message) -> None:
        """Record an outbound prompt message."""
        self._ensure_history(job_id).append(msg)

    def append_response(self, job_id: str, msg: Message) -> None:
        """Record an inbound LLM response."""
        self._ensure_history(job_id).append(msg)

    def append_tool_calls(self, job_id: str, calls: list[dict[str, Any]]) -> None:
        """Record parsed tool calls (not stored in this implementation)."""
        pass

    def append_tool_results(self, job_id: str, results: list[ToolResultEntry]) -> None:
        """Record tool execution results (not stored in this implementation)."""
        pass

    def get_history(self, job_id: str) -> list[Message]:
        """Reconstruct full message history for a job."""
        return list(self._history.get(job_id, []))

    def purge(self, job_id: str) -> None:
        """Delete all context for a job."""
        self._history.pop(job_id, None)

    def _ensure_history(self, job_id: str) -> list[Message]:
        """Ensure history exists for a job."""
        if job_id not in self._history:
            self._history[job_id] = []
        return self._history[job_id]
