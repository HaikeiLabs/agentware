"""Tests for nudge system."""

from haikeilabs.agentware.middleware.guardrails.nudge import (
    NudgeKind,
    prerequisite_nudge,
    retry_nudge,
    step_nudge,
    unknown_tool_nudge,
)


class TestNudgeKind:
    def test_retry_kind(self):
        assert NudgeKind.RETRY.value == "retry"

    def test_unknown_tool_kind(self):
        assert NudgeKind.UNKNOWN_TOOL.value == "unknown_tool"

    def test_step_kind(self):
        assert NudgeKind.STEP.value == "step"

    def test_prerequisite_kind(self):
        assert NudgeKind.PREREQUISITE.value == "prerequisite"


class TestRetryNudge:
    def test_basic(self):
        tools = ["get_weather", "echo", "search"]
        nudge = retry_nudge("some text", tools)

        assert nudge.role == "user"
        assert nudge.kind == NudgeKind.RETRY
        assert nudge.tier == 0
        assert "get_weather" in nudge.content

    def test_empty_tools(self):
        nudge = retry_nudge("text", [])
        assert "(no tools available)" in nudge.content


class TestUnknownToolNudge:
    def test_basic(self):
        tools = ["echo", "search"]
        nudge = unknown_tool_nudge("nonexistent", tools)

        assert nudge.role == "user"
        assert nudge.kind == NudgeKind.UNKNOWN_TOOL
        assert "nonexistent" in nudge.content


class TestStepNudge:
    def test_tier_1(self):
        pending = ["validate", "prepare"]
        nudge = step_nudge("submit", pending, 1)

        assert nudge.kind == NudgeKind.STEP
        assert nudge.tier == 1

    def test_tier_2(self):
        pending = ["validate", "prepare"]
        nudge = step_nudge("submit", pending, 2)

        assert nudge.tier == 2

    def test_tier_3(self):
        pending = ["validate"]
        nudge = step_nudge("submit", pending, 3)

        assert nudge.tier == 3

    def test_tier_clamping_below_min(self):
        nudge = step_nudge("submit", ["validate"], 0)
        assert nudge.tier == 1

    def test_tier_clamping_above_max(self):
        nudge = step_nudge("submit", ["validate"], 10)
        assert nudge.tier == 3


class TestPrerequisiteNudge:
    def test_basic(self):
        missing = ["authenticate", "validate"]
        nudge = prerequisite_nudge("submit", missing)

        assert nudge.kind == NudgeKind.PREREQUISITE
        assert nudge.tier == 0
        assert "authenticate" in nudge.content
