"""Tests for middleware package."""

import sys

sys.path.insert(0, "src")

from haikeilabs.agentware.middleware import (
    Action,
    CallerContext,
    InMemoryAuditor,
    MiddlewareImpl,
    Policy,
    Rule,
    SimplePolicyEvaluator,
)
from haikeilabs.agentware.middleware.policy import Condition, Operator


class MockExecutor:
    """Mock executor that returns success."""

    def execute(self, tool_name: str, args: dict):
        return ({"result": f"executed {tool_name}"}, True, "")


def mock_executor(tool_name: str, args: dict):
    """Mock executor that returns success."""
    return ({"result": f"executed {tool_name}"}, True, "")


def test_middleware_allow():
    mw = MiddlewareImpl(MockExecutor())
    result, success, error = mw.execute("test_tool", {"arg": "value"}, CallerContext())
    assert success
    assert error == ""


def test_middleware_deny():
    policy = Policy(rules=[Rule(name="deny_all", tools=["test_tool"], action=Action.DENY)])
    evaluator = SimplePolicyEvaluator(policy)
    mw = MiddlewareImpl(MockExecutor(), evaluator=evaluator)
    result, success, error = mw.execute("test_tool", {}, CallerContext())
    assert not success
    assert "denied by policy" in error


def test_middleware_audit():
    auditor = InMemoryAuditor()
    mw = MiddlewareImpl(MockExecutor(), auditor=auditor)
    mw.execute("test_tool", {"arg": "value"}, CallerContext(session_id="sess1"))
    records = auditor.query(type("Filter", (), {"session_id": "sess1", "tool_name": "", "action": None, "since": None, "limit": 0})())
    assert len(records) == 1


def test_policy_allow():
    policy = Policy(rules=[], default_deny=False)
    decision = policy.evaluate("test_tool", {}, CallerContext())
    assert decision.action == Action.ALLOW


def test_policy_deny():
    policy = Policy(rules=[], default_deny=True)
    decision = policy.evaluate("test_tool", {}, CallerContext())
    assert decision.action == Action.DENY


def test_rule_matches_tool():
    rule = Rule(name="test", tools=["foo", "bar"])
    assert rule.matches_tool("foo")
    assert rule.matches_tool("bar")
    assert not rule.matches_tool("baz")


def test_condition_evaluate():
    cond = Condition(field="caller.role", operator=Operator.EQ, value="admin")
    caller = CallerContext(role="admin")
    assert cond.evaluate({}, caller)

    caller = CallerContext(role="user")
    assert not cond.evaluate({}, caller)
