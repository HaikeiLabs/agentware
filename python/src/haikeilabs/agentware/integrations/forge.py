"""Per-workflow Forge response-quality middleware.

This module never authorizes a call. Wrap governed execution with Agentware's
existing policy/audit boundary; an allowed decision is not proof of execution.
Install ``haikeilabs-agentware[forge]`` on Python 3.12 or later.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any, TypeVar

from haikeilabs.agentware.middleware.guardrails.progress import ProgressTracker

T = TypeVar("T")


@dataclass(frozen=True)
class QualityDecision:
    """A quality result; only ``execute`` may proceed to the authorization boundary."""

    action: str
    nudge: str = ""
    role: str = "user"
    reason: str = ""
    calls: tuple[tuple[str, dict[str, Any]], ...] = ()


class QualityBlockedError(Exception):
    """A recoverable quality rejection or exhausted quality budget."""

    def __init__(self, decision: QualityDecision) -> None:
        self.decision = decision
        super().__init__(
            decision.nudge or decision.reason or "tool call rejected by quality guardrails"
        )


class ForgeSession:
    """Forge-backed state for exactly one workflow. Do not share across runs.

    ``check_text`` operates at the model-response boundary and may rescue text.
    ``run_tool`` wraps an already-governed executor; it serializes calls so
    prerequisites cannot race. The executor remains responsible for policy.
    """

    def __init__(
        self,
        tool_names: list[str],
        *,
        required_steps: list[str] | None = None,
        terminal_tool: str = "",
        prerequisites: dict[str, list[Any]] | None = None,
        max_retries: int = 3,
        max_tool_errors: int = 2,
        max_prereq_violations: int = 2,
        progress: ProgressTracker | None = None,
        observe_result: Callable[[str, dict[str, Any], Any], Iterable[tuple[str, bool]]]
        | None = None,
        executed_args: Callable[[str, dict[str, Any], Any], dict[str, Any]] | None = None,
    ) -> None:
        try:
            if version("forge-guardrails") != "0.10.1":
                raise ImportError("Forge integration requires forge-guardrails==0.10.1")
            from forge.guardrails import ErrorTracker, Guardrails, StepEnforcer
        except (ImportError, SyntaxError, PackageNotFoundError) as exc:
            raise ImportError(
                "Forge integration requires Python >=3.12 and haikeilabs-agentware[forge] (forge-guardrails==0.10.1)"
            ) from exc
        if observe_result is not None and progress is None:
            raise ValueError("observe_result requires a per-run progress tracker")
        self._progress = progress
        self._observe_result = observe_result
        self._executed_args = executed_args
        self._rules = prerequisites or {}
        self._matched_sources = {
            rule["tool"]
            for rules in self._rules.values()
            for rule in rules
            if isinstance(rule, dict) and "match_arg" in rule
        }
        self._terminal_tool = terminal_tool
        self._guard = Guardrails(
            tool_names=tool_names,
            terminal_tool=terminal_tool,
            required_steps=required_steps or [],
            max_retries=max_retries,
            max_tool_errors=max_tool_errors,
        )
        self._prerequisites = StepEnforcer(
            required_steps=[],
            terminal_tools=frozenset(),
            tool_prerequisites=prerequisites or {},
            max_prereq_violations=max_prereq_violations,
        )
        self._errors = ErrorTracker(max_retries=max_retries, max_tool_errors=max_tool_errors)
        self._lock = asyncio.Lock()

    @staticmethod
    def _decision(result: Any) -> QualityDecision:
        nudge = result.nudge
        return QualityDecision(
            action=result.action,
            nudge=nudge.content if nudge else "",
            role=nudge.role if nudge else "user",
            reason=result.reason or "",
            calls=tuple((call.tool, call.args) for call in (result.tool_calls or [])),
        )

    def check_text(self, text: str) -> QualityDecision:
        """Validate/rescue a complete model response; caller injects any nudge."""
        from forge.core.workflow import TextResponse

        return self._decision(self._guard.check(TextResponse(content=text)))

    def check_call(self, name: str, args: dict[str, Any]) -> QualityDecision:
        """Check one parsed call, then evaluate configured prerequisites."""
        from forge.core.workflow import ToolCall

        if self._errors.tool_errors_exhausted:
            return QualityDecision(action="fatal", reason="consecutive tool errors exhausted")
        if self._progress is not None and self._progress.snapshot().exhausted:
            return QualityDecision(action="fatal", reason="no-progress budget exhausted")
        for rule in self._rules.get(name, []):
            if isinstance(rule, dict) and "match_arg" in rule:
                key = rule["match_arg"]
                if key not in args or args[key] is None:
                    return QualityDecision(
                        action="step_blocked",
                        nudge=f"A non-null {key} is required for the prerequisite check.",
                    )
        call = ToolCall(tool=name, args=args)
        decision = self._decision(self._guard.check([call]))
        if decision.action != "execute":
            return decision
        check = self._prerequisites.check_prerequisites([call])
        if check.needs_nudge:
            if self._prerequisites.prereq_exhausted:
                return QualityDecision(
                    action="fatal", reason="consecutive prerequisite violations exhausted"
                )
            nudge = check.nudge
            return QualityDecision(
                action="step_blocked",
                nudge=nudge.content if nudge else "Missing prerequisite",
                role=nudge.role if nudge else "user",
            )
        return decision

    def record_success(self, name: str, args: dict[str, Any]) -> bool:
        """Record successful execution and reset consecutive failure counters.

        ``args`` must be the actual arguments after any policy filtering.
        """
        done = self._guard.record([(name, args)])
        self._prerequisites.record(name, args)
        self._prerequisites.reset_prereq_violations()
        self._errors.reset_errors()
        return bool(done)

    def record_failure(self) -> bool:
        """Record a downstream failure; return whether the error budget is exhausted."""
        self._errors.record_result(success=False)
        if self._progress is not None:
            self._progress.failure()
        return bool(self._errors.tool_errors_exhausted)

    async def run_tool(
        self,
        name: str,
        args: dict[str, Any],
        execute: Callable[[], Awaitable[T]],
        *,
        succeeded: Callable[[T], bool] | None = None,
    ) -> T:
        """Check, execute through the caller's boundary, and record outcome.

        A framework returning errors as normal values must supply ``succeeded``.
        For argument-matched prerequisites, supply ``executed_args`` at session
        construction to report post-policy arguments; otherwise matching fails
        closed. Policy denials/execution errors are re-raised, never turned into
        success.
        """
        async with self._lock:
            decision = self.check_call(name, args)
            if decision.action != "execute":
                if self._progress is not None and self._progress.failure().exhausted:
                    raise QualityBlockedError(
                        QualityDecision(action="fatal", reason="no-progress budget exhausted")
                    )
                raise QualityBlockedError(decision)
            try:
                result = await execute()
                ok = succeeded(result) if succeeded else True
            except Exception:
                self.record_failure()
                raise
            if ok:
                actual_args = (
                    self._executed_args(name, args, result)
                    if self._executed_args is not None
                    else ({} if name in self._matched_sources else args)
                )
                self.record_success(name, actual_args)
                if self._progress is not None and self._observe_result is not None:
                    # The callback runs only after successful execution. If it fails,
                    # the side effect may already have happened; never auto-retry.
                    evidence_args = actual_args if self._executed_args is not None else {}
                    before = set(self._progress.snapshot().pending)
                    after = self._progress.observe_many(
                        self._observe_result(name, evidence_args, result)
                    )
                    if not before.difference(after.pending):
                        self._progress.failure()
            else:
                self.record_failure()
            return result

    def finish_nudge(self) -> QualityDecision | None:
        """Suggest a terminal call only when configured verified facts are complete."""
        if self._progress is None or not self._terminal_tool:
            return None
        snapshot = self._progress.snapshot()
        if not snapshot.ready or snapshot.exhausted:
            return None
        return QualityDecision(
            action="retry",
            nudge=f"The verified workflow requirements are complete. Call {self._terminal_tool} to finish.",
        )


def make_context_manager(budget_tokens: int, *, keep_recent: int = 2) -> Any:
    """Create Forge's tiered context manager for a harness-owned message loop.

    This does not alter framework history automatically. The harness must
    convert its messages to Forge's message type and call
    ``compact_forge_messages`` to check pairing after compaction.
    """
    if budget_tokens <= 0 or keep_recent < 1:
        raise ValueError("budget_tokens must be positive and keep_recent at least one")
    try:
        if version("forge-guardrails") != "0.10.1":
            raise ImportError("Forge integration requires forge-guardrails==0.10.1")
        from forge.context import ContextManager, TieredCompact
    except (ImportError, SyntaxError, PackageNotFoundError) as exc:
        raise ImportError("install haikeilabs-agentware[forge] on Python >=3.12") from exc
    return ContextManager(
        strategy=TieredCompact(keep_recent=keep_recent), budget_tokens=budget_tokens
    )


def compact_forge_messages(manager: Any, messages: list[Any], *, step_index: int = 0) -> list[Any]:
    """Compact Forge messages only if tool-call/result pairing remains valid.

    Forge 0.10.1's tiered strategy may drop or truncate result messages in a
    way that loses their call IDs. In that case raise rather than returning an
    invalid provider history; the caller owns a safe fallback or termination.
    Call only at a completed tool-batch boundary.
    """

    def validate(history: list[Any]) -> None:
        pending: set[str] = set()
        seen: set[str] = set()
        for message in history:
            calls = getattr(message, "tool_calls", None) or []
            role = getattr(getattr(message, "role", None), "value", getattr(message, "role", None))
            if pending and role != "tool":
                raise ValueError("tool results missing before the next message")
            for call in calls:
                call_id = getattr(call, "call_id", "")
                if not call_id or call_id in seen:
                    raise ValueError("missing or duplicate tool call ID")
                seen.add(call_id)
                pending.add(call_id)
            if role == "tool":
                call_id = getattr(message, "tool_call_id", None)
                if call_id not in pending:
                    raise ValueError("orphan or duplicate tool result")
                pending.remove(call_id)
        if pending:
            raise ValueError("incomplete tool-call batch")

    validate(messages)
    result = manager.maybe_compact(messages, step_index=step_index)
    try:
        validate(result)
    except ValueError as exc:
        raise ValueError("Forge compaction produced an invalid tool-call history") from exc
    return result
