import pytest

from haikeilabs.agentware.middleware.guardrails.step_enforcer import (
    StepEnforcer,
    StepNotAllowedError,
)


def test_add_step():
    se = StepEnforcer()
    se.add_step("deploy", ["build", "test"])

    allowed, missing = se.can_execute("session1", "deploy")
    assert allowed is False
    assert len(missing) == 2


def test_mark_step_complete():
    se = StepEnforcer()
    se.add_step("deploy", ["build", "test"])

    se.mark_step_complete("session1", "build")
    allowed, missing = se.can_execute("session1", "deploy")

    assert allowed is False
    assert missing == ["test"]


def test_validate_execution_success():
    se = StepEnforcer()
    se.add_step("deploy", ["build"])

    se.mark_step_complete("session1", "build")
    se.validate_execution("session1", "deploy")


def test_validate_execution_failure():
    se = StepEnforcer()
    se.add_step("deploy", ["build"])

    with pytest.raises(StepNotAllowedError) as exc_info:
        se.validate_execution("session1", "deploy")

    assert exc_info.value.tool == "deploy"
    assert "build" in exc_info.value.missing_steps


def test_reset_session():
    se = StepEnforcer()
    se.add_step("deploy", ["build"])

    se.mark_step_complete("session1", "build")
    se.reset_session("session1")

    allowed, _ = se.can_execute("session1", "deploy")
    assert allowed is False


def test_is_terminal_allowed():
    se = StepEnforcer()
    se.add_step("deploy", ["build"])
    se.add_step("build", [])

    se.mark_step_complete("session1", "build")
    allowed = se.is_terminal_allowed("session1", "deploy")

    assert allowed is True


def test_get_allowed_terminals():
    se = StepEnforcer()
    se.add_step("deploy", ["build"])
    se.add_step("test", [])

    allowed = se.get_allowed_terminals("session1")

    assert allowed == ["test"]


def test_no_prerequisites():
    se = StepEnforcer()
    se.add_step("build", [])

    allowed, _ = se.can_execute("session1", "build")
    assert allowed is True


def test_invalid_session():
    se = StepEnforcer()
    se.add_step("deploy", ["build"])

    allowed, _ = se.can_execute("nonexistent", "deploy")
    assert allowed is False
