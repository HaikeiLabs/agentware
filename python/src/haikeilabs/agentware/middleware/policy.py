"""Policy evaluation for middleware."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol

from .types import Action, CallerContext, Decision


class Operator(str, Enum):
    """Condition operators."""

    EQ = "eq"
    NOT_EQ = "not_eq"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    MATCHES = "matches"
    NOT_MATCHES = "not_matches"
    EXISTS = "exists"
    NOT_EXISTS = "not_exists"


class PolicyEvaluator(Protocol):
    """Protocol for policy evaluators."""

    def evaluate(
        self, tool_name: str, args: dict[str, Any], caller: CallerContext
    ) -> Decision:
        """Evaluate a tool call against policy."""
        ...


@dataclass
class Condition:
    """A condition for rule matching."""

    field: str
    operator: Operator
    value: str = ""

    def evaluate(self, args: dict[str, Any], caller: CallerContext) -> bool:
        """Evaluate the condition."""
        value = self._get_value(args, caller)
        return self._compare(value)

    def _get_value(self, args: dict[str, Any], caller: CallerContext) -> str:
        """Get the value to compare against."""
        if self.field.startswith("caller."):
            field_name = self.field[7:]
            if field_name == "role":
                return caller.role
            elif field_name == "source":
                return caller.source
            elif field_name == "trusted":
                return "true" if caller.trusted else "false"
            elif field_name == "user_id":
                return caller.user_id
            elif field_name == "session_id":
                return caller.session_id
        elif self.field.startswith("args."):
            arg_key = self.field[5:]
            if arg_key in args:
                return str(args[arg_key])
        return ""

    def _compare(self, value: str) -> bool:
        """Compare the value against the condition."""
        op = self.operator
        if op == Operator.EQ:
            return value == self.value
        elif op == Operator.NOT_EQ:
            return value != self.value
        elif op == Operator.CONTAINS:
            return self.value in value
        elif op == Operator.NOT_CONTAINS:
            return self.value not in value
        elif op == Operator.MATCHES:
            try:
                return bool(re.match(self.value, value))
            except re.error:
                return False
        elif op == Operator.NOT_MATCHES:
            try:
                return not bool(re.match(self.value, value))
            except re.error:
                return True
        elif op == Operator.EXISTS:
            return value != ""
        elif op == Operator.NOT_EXISTS:
            return value == ""
        return False


@dataclass
class RateLimit:
    """Rate limit configuration."""

    count: int
    window: timedelta


@dataclass
class Rule:
    """A policy rule."""

    name: str
    tools: list[str] = field(default_factory=list)
    action: Action = Action.ALLOW
    conditions: list[Condition] = field(default_factory=list)
    max_rate: RateLimit | None = None
    redact_fields: list[str] = field(default_factory=list)

    def matches_tool(self, tool_name: str) -> bool:
        """Check if this rule applies to the tool."""
        if not self.tools:
            return True
        return tool_name in self.tools or "*" in self.tools

    def evaluate_conditions(
        self, args: dict[str, Any], caller: CallerContext
    ) -> bool:
        """Evaluate all conditions."""
        if not self.conditions:
            return True
        return all(
            condition.evaluate(args, caller) for condition in self.conditions
        )


@dataclass
class Policy:
    """Policy with rules."""

    rules: list[Rule] = field(default_factory=list)
    default_deny: bool = False

    def evaluate(
        self, tool_name: str, args: dict[str, Any], caller: CallerContext
    ) -> Decision:
        """Evaluate a tool call against all rules."""
        for rule in self.rules:
            if not rule.matches_tool(tool_name):
                continue
            if not rule.evaluate_conditions(args, caller):
                continue
            return Decision(
                action=rule.action,
                rule=rule.name,
                reason=f"matched rule {rule.name}",
                timestamp=datetime.now(),
            )

        if self.default_deny:
            return Decision(
                action=Action.DENY,
                rule="default",
                reason="no matching rules and default deny is enabled",
                timestamp=datetime.now(),
            )

        return Decision(
            action=Action.ALLOW,
            rule="default",
            reason="no matching rules and default allow is enabled",
            timestamp=datetime.now(),
        )


class SimplePolicyEvaluator:
    """Simple policy evaluator wrapping a Policy."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def evaluate(
        self, tool_name: str, args: dict[str, Any], caller: CallerContext
    ) -> Decision:
        return self.policy.evaluate(tool_name, args, caller)
