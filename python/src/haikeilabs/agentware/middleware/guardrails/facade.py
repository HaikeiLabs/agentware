"""Forge-inspired, framework-neutral facade for response-quality guardrails.

This API follows the Forge guardrails design (MIT; see python/THIRD_PARTY_NOTICES.md).
Guardrails describe response quality and progress only. They do not authorize
tool execution; callers must still pass tool calls through their policy layer.
"""

from __future__ import annotations

from typing import Any


class Guardrails:
    """Check tool-call responses and record tool errors against v1 config."""

    def __init__(
        self,
        tool_names: list[str],
        required_steps: list[str] | None = None,
        terminal_tool: str | None = None,
        prerequisites: list[dict[str, str]] | None = None,
        max_prereq_violations: int = 2,
        nudges: dict[str, str] | None = None,
    ) -> None:
        self.tool_names = set(tool_names)
        self.required_steps = required_steps or []
        self.terminal_tool = terminal_tool
        self.prerequisites = prerequisites or []
        self.max_prereq_violations = max_prereq_violations
        self.nudges = nudges or {}
        self._state: dict[str, Any] = {}

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> Guardrails:
        """Build guardrails from a v1 config document."""
        if config.get("version") != 1:
            raise ValueError("unsupported guardrails config version")
        return cls(
            tool_names=config["tool_names"],
            required_steps=config.get("required_steps"),
            terminal_tool=config.get("terminal_tool"),
            prerequisites=config.get("prerequisites"),
            max_prereq_violations=config.get("max_prereq_violations", 2),
            nudges=config.get("nudges"),
        )

    def check(
        self, response: dict[str, Any], state: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Classify a response as execute, retry, step_blocked, or fatal."""
        if state is not None:
            self._state = dict(state)
        current = self._state
        calls = response.get("tool_calls", [])
        unknown = next(
            (call["tool"] for call in calls if call.get("tool") not in self.tool_names), None
        )
        if unknown is not None:
            return self._result("retry", "unknown_tool")

        completed = current.get("completed_steps", [])
        if calls and calls[0]["tool"] == self.terminal_tool:
            missing = [
                step
                for step in self.required_steps
                if not any(entry.get("tool") == step for entry in completed)
            ]
            if missing:
                return self._result("step_blocked", "step", missing_steps=missing)

        for call in calls:
            for prerequisite in self.prerequisites:
                if call["tool"] != prerequisite["tool"]:
                    continue
                match_arg = prerequisite.get("match_arg")
                target = call.get("args", {}).get(match_arg) if match_arg else None
                found = any(
                    entry.get("tool") == prerequisite["requires"]
                    and (not match_arg or entry.get("args", {}).get(match_arg) == target)
                    for entry in completed
                )
                if not found:
                    violations = current.get("prereq_violations", 0) + 1
                    self._state = {**current, "prereq_violations": violations}
                    outcome = "fatal" if violations > self.max_prereq_violations else "retry"
                    return self._result(
                        outcome,
                        "prerequisite",
                        missing_prerequisites=[prerequisite["requires"]],
                        prereq_violations=violations,
                    )

        return {"outcome": "execute", "tool_calls": calls}

    def record(self, record: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record a completed tool step or tool error as quality state."""
        if state is not None:
            self._state = dict(state)
        current = self._state
        if "error" not in record and "category" not in record:
            completed = [
                *current.get("completed_steps", []),
                {"tool": record["tool"], "args": record.get("args", {})},
            ]
            self._state = {**current, "completed_steps": completed}
            return {"outcome": "recorded", "completed_steps": completed}
        errors = [
            *current.get("tool_errors", []),
            {"tool": record["tool"], "category": record.get("category", "unknown")},
        ]
        self._state = {**current, "tool_errors": errors}
        result = {"outcome": "recorded", "tool_errors": errors}
        if text := self.nudges.get("retry"):
            result["nudge_text"] = text
        return result

    def _result(self, outcome: str, kind: str, **details: Any) -> dict[str, Any]:
        result = {"outcome": outcome, **details, "nudge_kind": kind}
        nudge_key = (
            "fatal"
            if outcome == "fatal"
            else {"step": "step_blocked", "unknown_tool": "retry"}.get(kind, kind)
        )
        if text := self.nudges.get(nudge_key):
            result["nudge_text"] = text
        return result
