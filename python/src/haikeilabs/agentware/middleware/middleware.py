"""Middleware implementation."""

from typing import Any, Protocol

from .audit import Auditor, AuditRecord
from .policy import PolicyEvaluator
from .types import Action, CallerContext, Decision


class ToolExecutor(Protocol):
    """Protocol for tool executors."""

    def execute(self, tool_name: str, args: dict[str, Any]) -> tuple[Any, bool, str]:
        """Execute a tool. Returns (result, success, error)."""
        ...


class Middleware(Protocol):
    """Protocol for middleware."""

    def execute(
        self,
        tool_name: str,
        args: dict[str, Any],
        caller: CallerContext,
        framework: str = "",
    ) -> tuple[Any, bool, str]:
        """Execute a tool call through middleware."""
        ...

    def with_policy(self, evaluator: PolicyEvaluator) -> "MiddlewareImpl":
        """Add a policy evaluator."""
        ...

    def with_auditor(self, auditor: Auditor) -> "MiddlewareImpl":
        """Add an auditor."""
        ...


class MiddlewareImpl:
    """Middleware implementation with policy and audit."""

    def __init__(
        self,
        executor: ToolExecutor,
        evaluator: PolicyEvaluator | None = None,
        auditor: Auditor | None = None,
    ) -> None:
        self._executor = executor
        self._evaluator = evaluator
        self._auditor = auditor

    def execute(
        self,
        tool_name: str,
        args: dict[str, Any],
        caller: CallerContext,
        framework: str = "",
    ) -> tuple[Any, bool, str]:
        """Execute a tool call through middleware."""
        if self._evaluator:
            decision = self._evaluator.evaluate(tool_name, args, caller)
        else:
            decision = Decision(action=Action.ALLOW, reason="no policy configured")

        if self._auditor:
            self._auditor.record(
                AuditRecord(
                    session_id=caller.session_id,
                    invoking_subject=caller.invoking_subject,
                    parent_span=caller.parent_span,
                    delegation_depth=caller.delegation_depth,
                    framework=framework,
                    tool_name=tool_name,
                    args=args,
                    decision=decision,
                )
            )

        if decision.action == Action.DENY:
            return (None, False, f"denied by policy: {decision.reason}")

        if decision.action == Action.FILTER and decision.redacted_args:
            args = {**args, **decision.redacted_args}

        return self._executor.execute(tool_name, args)

    def with_policy(self, evaluator: PolicyEvaluator) -> "MiddlewareImpl":
        """Add a policy evaluator."""
        self._evaluator = evaluator
        return self

    def with_auditor(self, auditor: Auditor) -> "MiddlewareImpl":
        """Add an auditor."""
        self._auditor = auditor
        return self


def new_middleware(executor: ToolExecutor) -> MiddlewareImpl:
    """Create a new middleware instance."""
    return MiddlewareImpl(executor)
