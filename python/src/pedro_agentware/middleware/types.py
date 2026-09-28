"""Middleware types."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any


class MessageType(str, Enum):
    """Semantic type of a message for smart compaction and guardrails."""

    SYSTEM_PROMPT = "system_prompt"
    USER_INPUT = "user_input"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    REASONING = "reasoning"
    TEXT_RESPONSE = "text_response"
    STEP_NUDGE = "step_nudge"
    PREREQUISITE_NUDGE = "prerequisite_nudge"
    RETRY_NUDGE = "retry_nudge"
    CONTEXT_WARNING = "context_warning"
    SUMMARY = "summary"


@dataclass(frozen=True)
class MessageMeta:
    """Metadata for a message used in smart compaction and guardrails."""

    type: MessageType = MessageType.USER_INPUT
    step_index: int | None = None
    original_type: MessageType | None = None
    token_estimate: int | None = None


class Action(str, Enum):
    """Action to take on a tool call."""

    ALLOW = "allow"
    DENY = "deny"
    FILTER = "filter"


@dataclass
class CallerContext:
    """Context about the caller making the tool call.

    Mirrors the Go ``middleware.CallerContext`` struct field for field.

    ``invoking_subject`` is the identity of the *human* who originated the
    request. It is set once at the human-facing entry point and is carried
    unchanged across every delegation hop, so a tool call made by a subagent
    several levels deep still resolves back to a person rather than to the
    agent's own service identity. ``parent_span`` and ``delegation_depth``
    record where in the delegation chain the call was made.
    """

    user_id: str = ""
    session_id: str = ""
    role: str = ""
    source: str = ""
    trusted: bool = False
    metadata: dict[str, str] = field(default_factory=dict)
    invoking_subject: str = ""
    parent_span: str = ""
    delegation_depth: int = 0
    span_id: str = ""
    agent_id: str = ""
    agent_version: str = ""
    framework: str = ""
    workspace_id: str = ""

    def delegate(self, span: str = "", **overrides: Any) -> "CallerContext":
        """Return the context a subagent spawned by this caller should run under.

        The child inherits ``invoking_subject`` unchanged, takes ``span`` (or
        this context's own ``parent_span`` when none is given) as its
        ``parent_span``, and sits one level deeper in the delegation chain.
        ``invoking_subject`` is deliberately not accepted as an override:
        overwriting it with the delegating agent's own identity is the exact
        attribution loss this field exists to prevent.
        """
        overrides.pop("invoking_subject", None)
        overrides.setdefault("metadata", self.metadata)
        overrides["metadata"] = dict(overrides["metadata"])
        return replace(
            self,
            parent_span=span or self.span_id or self.parent_span,
            delegation_depth=self.delegation_depth + 1,
            span_id="",
            **overrides,
        )


@dataclass
class Decision:
    """Decision made by policy evaluator."""

    action: Action
    rule: str = ""
    reason: str = ""
    redacted_args: dict[str, Any] = field(default_factory=dict)
    enrollment: dict[str, Any] | None = None
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "action": self.action.value,
            "rule": self.rule,
            "reason": self.reason,
            "redacted_args": self.redacted_args,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.enrollment is not None:
            d["enrollment"] = self.enrollment
        return d
