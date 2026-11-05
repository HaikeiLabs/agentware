"""Google ADK FunctionTool wrapper for a per-run Forge quality session.

Wrap a FunctionTool that already enforces application policy. This adapter is
for function tools, not built-in tools with custom model request processing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from google.adk.tools import FunctionTool
from google.adk.tools.base_tool import BaseTool

from .forge import ForgeSession, QualityBlockedError


class ForgeFunctionTool(BaseTool):  # type: ignore[misc]
    """Apply Forge checks to a Google ADK FunctionTool without changing its schema."""

    def __init__(
        self,
        wrapped: FunctionTool,
        session_for: Callable[[Any], ForgeSession],
        *,
        succeeded: Callable[[Any], bool] | None = None,
    ) -> None:
        if not isinstance(wrapped, FunctionTool):
            raise TypeError("ForgeFunctionTool supports Google ADK FunctionTool only")
        super().__init__(
            name=wrapped.name,
            description=wrapped.description,
            is_long_running=wrapped.is_long_running,
            custom_metadata=wrapped.custom_metadata,
            behavior=wrapped.behavior,
            response_scheduling=wrapped.response_scheduling,
        )
        self._wrapped = wrapped
        self._session_for = session_for
        self._succeeded = succeeded or (
            lambda result: not (isinstance(result, dict) and result.get("error"))
        )

    def _get_declaration(self) -> Any:
        """Preserve the wrapped function tool's declaration."""
        return self._wrapped._get_declaration()

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        """Run a quality-checked call; send recoverable feedback as a tool result."""
        session = self._session_for(tool_context)
        try:
            return await session.run_tool(
                self.name,
                args,
                lambda: self._wrapped.run_async(args=args, tool_context=tool_context),
                succeeded=self._succeeded,
            )
        except QualityBlockedError as exc:
            if exc.decision.action == "fatal":
                raise
            return {"error": str(exc)}
