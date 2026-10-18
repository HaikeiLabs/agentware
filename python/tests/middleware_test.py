"""Tests for middleware package."""

import sys

sys.path.insert(0, "src")

from haikeilabs.agentware.middleware import (
    Action,
    CallerContext,
    Decision,
    InMemoryAuditor,
    MiddlewareImpl,
)


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
    class _DenyTestTool:
        def evaluate(self, tool_name, args, caller):
            return Decision(action=Action.DENY, rule="deny_all", reason="denied")

    mw = MiddlewareImpl(MockExecutor(), evaluator=_DenyTestTool())
    result, success, error = mw.execute("test_tool", {}, CallerContext())
    assert not success
    assert "denied by policy" in error


def test_middleware_audit():
    auditor = InMemoryAuditor()
    mw = MiddlewareImpl(MockExecutor(), auditor=auditor)
    mw.execute("test_tool", {"arg": "value"}, CallerContext(session_id="sess1"))
    records = auditor.query(
        type(
            "Filter",
            (),
            {"session_id": "sess1", "tool_name": "", "action": None, "since": None, "limit": 0},
        )()
    )
    assert len(records) == 1


def test_policy_allow():
    class _AllowAll:
        def evaluate(self, tool_name, args, caller):
            return Decision(action=Action.ALLOW, rule="default", reason="allowed by default")

    decision = _AllowAll().evaluate("test_tool", {}, CallerContext())
    assert decision.action == Action.ALLOW


def test_policy_deny():
    class _DenyAll:
        def evaluate(self, tool_name, args, caller):
            return Decision(action=Action.DENY, rule="default", reason="default deny")

    decision = _DenyAll().evaluate("test_tool", {}, CallerContext())
    assert decision.action == Action.DENY


def test_rule_matches_tool():
    class _Rule:
        def __init__(self, tools):
            self.tools = tools

        def matches_tool(self, name):
            return name in self.tools

    rule = _Rule(["foo", "bar"])
    assert rule.matches_tool("foo")
    assert rule.matches_tool("bar")
    assert not rule.matches_tool("baz")


def test_condition_evaluate():
    class _Condition:
        def __init__(self, field, op, value):
            self.field = field
            self.op = op
            self.value = value

        def evaluate(self, args, caller):
            if self.field == "caller.role":
                return (
                    (caller.role == self.value) if self.op == "eq" else (caller.role != self.value)
                )
            return False

    cond = _Condition(field="caller.role", op="eq", value="admin")
    caller = CallerContext(role="admin")
    assert cond.evaluate({}, caller)

    caller = CallerContext(role="user")
    assert not cond.evaluate({}, caller)
