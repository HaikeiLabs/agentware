"""Nudge messages for guardrail components."""

from dataclasses import dataclass
from enum import Enum


class NudgeKind(str, Enum):
    """Kind of nudge message."""

    RETRY = "retry"
    UNKNOWN_TOOL = "unknown_tool"
    STEP = "step"
    PREREQUISITE = "prerequisite"


@dataclass(frozen=True)
class Nudge:
    """Message to inject into conversation history.

    Returned by guardrail components when the model needs correction.

    Attributes:
        role: Message role for injection ("user", "system", or "tool").
        content: The nudge text.
        kind: Identifies what generated the nudge ("retry", "unknown_tool", "step").
        tier: Escalation level for step nudges (0 = N/A, 1-3 = escalating).
    """

    role: str
    content: str
    kind: NudgeKind
    tier: int = 0


def _join_tool_names(names: list[str]) -> str:
    """Join tool names into a readable string."""
    if not names:
        return "(no tools available)"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def retry_nudge(raw_response: str, tool_names: list[str]) -> Nudge:
    """Nudge for when the model returns text instead of a tool call."""
    tools_list = _join_tool_names(tool_names)
    content = (
        f"Your previous response was not a valid tool call. "
        f"You must respond with a tool call, not free text. "
        f"Available tools: {tools_list}. Please try again with a valid tool call."
    )
    return Nudge(role="user", content=content, kind=NudgeKind.RETRY, tier=0)


def unknown_tool_nudge(tool_name: str, available_tools: list[str]) -> Nudge:
    """Nudge for when the model calls a tool that doesn't exist."""
    tools_list = _join_tool_names(available_tools)
    content = (
        f"Tool '{tool_name}' does not exist. "
        f"Available tools: {tools_list}. Call one of them."
    )
    return Nudge(role="user", content=content, kind=NudgeKind.UNKNOWN_TOOL, tier=0)


def step_nudge(terminal_tool: str, pending_steps: list[str], tier: int = 1) -> Nudge:
    """Escalating nudge for premature terminal tool attempts.

    Args:
        terminal_tool: The name of the terminal tool the model tried to call.
        pending_steps: The required steps that must be completed first.
        tier: Escalation level (1=polite, 2=direct, 3=aggressive).
    """
    tier = max(1, min(3, tier))
    steps = _join_tool_names(pending_steps)

    if tier == 1:
        content = (
            f"You cannot call {terminal_tool} yet. "
            f"You must first complete these required steps: {steps}. "
            "Call one of them now."
        )
    elif tier == 2:
        content = f"You must call one of these tools now: {steps}. Pick one."
    else:
        content = (
            f"STOP. You MUST call one of: {steps}. "
            f"Do NOT call {terminal_tool}. "
            f"Your next response MUST be a tool call to one of: {steps}."
        )

    return Nudge(role="user", content=content, kind=NudgeKind.STEP, tier=tier)


def prerequisite_nudge(tool_name: str, missing_prereqs: list[str]) -> Nudge:
    """Nudge for when a tool is called without its prerequisites."""
    prereqs = _join_tool_names(missing_prereqs)
    content = (
        f"You cannot call {tool_name} yet. "
        f"You must first call: {prereqs}. "
        "Call the prerequisite tool now."
    )
    return Nudge(role="user", content=content, kind=NudgeKind.PREREQUISITE, tier=0)
