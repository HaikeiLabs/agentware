"""Policy evaluation protocol for middleware."""

from typing import Any, Protocol

from .types import CallerContext, Decision


class PolicyEvaluator(Protocol):
    """Protocol for policy evaluators."""

    def evaluate(self, tool_name: str, args: dict[str, Any], caller: CallerContext) -> Decision:
        """Evaluate a tool call against policy."""
        ...
