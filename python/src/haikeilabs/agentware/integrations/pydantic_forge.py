"""Pydantic AI toolset wrapper for a per-run Forge quality session.

Install Pydantic AI and ``haikeilabs-agentware[forge]``. Wrap the toolset
that already contains the application's policy-protected tools. The caller
must resolve a distinct ForgeSession from each run context.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic_ai import ModelRetry
from pydantic_ai.toolsets import WrapperToolset

from .forge import ForgeSession, QualityBlockedError


class ForgeToolset(WrapperToolset[Any]):  # type: ignore[misc]
    """Apply Forge quality checks around an existing Pydantic AI toolset."""

    def __init__(
        self,
        wrapped: Any,
        session_for: Callable[[Any], ForgeSession],
        *,
        succeeded: Callable[[Any], bool] | None = None,
    ) -> None:
        super().__init__(wrapped)
        self._session_for = session_for
        self._succeeded = succeeded

    async def call_tool(self, name: str, tool_args: dict[str, Any], ctx: Any, tool: Any) -> Any:
        """Return the wrapped tool result, or a framework-native retry on quality rejection."""
        session = self._session_for(ctx)
        try:
            return await session.run_tool(
                name,
                tool_args,
                lambda: super(ForgeToolset, self).call_tool(name, tool_args, ctx, tool),
                succeeded=self._succeeded,
            )
        except QualityBlockedError as exc:
            if exc.decision.action == "fatal":
                raise
            raise ModelRetry(str(exc)) from exc
