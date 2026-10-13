"""Audited tool client.

Python port of ``go/middleware/tool_client.go``. Wraps a tool function so that
every call is evaluated against policy before execution and recorded to the
audit log afterwards, giving callers a small surface -- a function in, a result
out -- instead of requiring them to assemble a Middleware, a PolicyEvaluator
and an Auditor themselves.
"""

import inspect
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from uuid import uuid4

from .audit import AuditFilter, Auditor, AuditRecord, InMemoryAuditor
from .policy import PolicyEvaluator
from .types import Action, CallerContext, Decision

__all__ = ["AuditedToolClient"]


class AuditedToolClient:
    """Runs tool calls behind policy evaluation and audit logging.

    ``source`` identifies this client in the audit trail. Policy evaluation and
    auditing are in-process: without a policy evaluator every call is allowed,
    but it is still audited. A call denied by policy raises
    ``PermissionError`` -- denial is an outcome of the call, and surfacing it
    is what makes the audit trail meaningful.
    """

    def __init__(
        self,
        middleware_url: str = "",
        source: str = "pedro-agentware",
        evaluator: PolicyEvaluator | None = None,
        auditor: Auditor | None = None,
        executor: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.middleware_url = middleware_url
        self.source = source
        self.auditor: Auditor = auditor if auditor is not None else InMemoryAuditor()
        self._evaluator = evaluator
        self._executor = executor

    def with_policy(self, evaluator: PolicyEvaluator) -> "AuditedToolClient":
        """Set the policy evaluator consulted before each execution."""
        self._evaluator = evaluator
        return self

    def with_auditor(self, auditor: Auditor) -> "AuditedToolClient":
        """Replace the auditor. Records written before the swap stay behind."""
        self.auditor = auditor
        return self

    def records(self, filter: AuditFilter | None = None) -> list[AuditRecord]:
        """Return this client's audit records, filtered by ``filter``."""
        query = getattr(self.auditor, "query", None)
        if query is None:
            return []
        return query(filter if filter is not None else AuditFilter())

    async def Execute(  # noqa: N802 - name mirrors the Go method
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        user_id: str,
        channel_id: str,
        guild_id: str | None,
        func: Callable[..., Any] | None = None,
        caller: CallerContext | None = None,
    ) -> Any:
        """Authorize ``tool_name``, run ``func`` when allowed, and audit either way.

        ``caller`` carries the delegation chain. When omitted, one is built from
        ``user_id`` -- which at a human entry point is the invoking subject.
        """
        if func is None:
            if self._executor is None:
                raise TypeError("func is required when no executor is configured")
            executor = self._executor

            def execute_from_contract(**args: Any) -> Any:
                return executor(tool_name, args)

            func = execute_from_contract

        if caller is None:
            caller = CallerContext(
                user_id=user_id,
                session_id=channel_id,
                source=self.source,
                invoking_subject=user_id,
                metadata={"channel_id": channel_id, "guild_id": guild_id or ""},
            )

        # The span identifies this exact tool authorization/execution pair.
        # Keep it on the per-call context so delegated children can use it as
        # their parent span without mutating a caller context shared by peers.
        if not caller.span_id:
            caller = replace(caller, span_id=str(uuid4()))

        if self._evaluator is not None:
            decision = self._evaluator.evaluate(tool_name, tool_args, caller)
        else:
            decision = Decision(action=Action.ALLOW, reason="no policy configured")

        self.auditor.record(
            AuditRecord(
                session_id=caller.session_id,
                invoking_subject=caller.invoking_subject,
                parent_span=caller.parent_span,
                delegation_depth=caller.delegation_depth,
                framework=self.source,
                tool_name=tool_name,
                args=tool_args,
                decision=decision,
            )
        )

        if decision.action == Action.DENY:
            raise PermissionError(f"denied by policy: {decision.reason}")

        if decision.action == Action.FILTER and decision.redacted_args:
            tool_args = {**tool_args, **decision.redacted_args}

        result = func(**tool_args)
        if inspect.isawaitable(result):
            return await result
        return result
