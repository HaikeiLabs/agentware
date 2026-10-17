"""Shared v1 contract tests for the Guardrails facade."""

import json
from pathlib import Path
from typing import Any

from haikeilabs.agentware.middleware.guardrails import Guardrails


def _cases() -> list[dict[str, Any]]:
    fixture = Path(__file__).parents[2] / "fixtures" / "guardrails" / "cases.v1.json"
    return json.loads(fixture.read_text())["cases"]


def test_shared_guardrails_v1_cases() -> None:
    for case in _cases():
        guardrails = Guardrails.from_config(case["config"])
        event = case["input"]
        if "record" in event:
            result = guardrails.record(event["record"], event["state"])
        else:
            result = guardrails.check(event["response"], event["state"])
        assert result == case["expected"], case["name"]


def test_guardrails_outcomes_never_decide_authorization() -> None:
    result = Guardrails.from_config({"version": 1, "tool_names": ["search"]}).check(
        {"tool_calls": [{"tool": "search", "args": {}}]}
    )

    assert result["outcome"] == "execute"
    assert "allow" not in result and "deny" not in result


def test_recorded_success_advances_completed_steps() -> None:
    guardrails = Guardrails.from_config(
        {
            "version": 1,
            "tool_names": ["inspect", "finish"],
            "required_steps": ["inspect"],
            "terminal_tool": "finish",
        }
    )

    result = guardrails.record({"tool": "inspect", "args": {"id": "item-1"}}, {})

    assert result == {
        "outcome": "recorded",
        "completed_steps": [{"tool": "inspect", "args": {"id": "item-1"}}],
    }
    assert guardrails.check({"tool_calls": [{"tool": "finish", "args": {}}]}) == {
        "outcome": "execute",
        "tool_calls": [{"tool": "finish", "args": {}}],
    }


def test_recorded_error_returns_configured_retry_nudge() -> None:
    guardrails = Guardrails.from_config(
        {
            "version": 1,
            "tool_names": ["inspect"],
            "nudges": {"retry": "Try another query."},
        }
    )

    result = guardrails.record({"tool": "inspect", "error": "timeout", "category": "timeout"})

    assert result["outcome"] == "recorded"
    assert result["nudge_text"] == "Try another query."
