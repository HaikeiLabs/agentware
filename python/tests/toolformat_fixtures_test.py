"""Shared-fixture conformance runner for the toolformat family.

Replays every case in ``fixtures/toolformat/*-cases.json`` through the
Python formatters and records each passing case for the equal-coverage
gate (``scripts/conformance/gate.py``, see ``docs/testing.md``). The test
id contains ``toolformat`` so the conformance workflow's ``-k`` filter
selects it. The reasoning family has its own runner in
``reasoning_fixtures_test.py``.
"""

import json
from pathlib import Path

import pytest

from conformance import record

from haikeilabs.agentware.toolformat.formatter import (
    AnthropicFormatter,
    DeepSeekFormatter,
    GLMFormatter,
    OpenAIFOrmatter,
    QwenFormatter,
)
from haikeilabs.agentware.tools import Result

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"

TOOLFORMAT_FORMATTERS = {
    "anthropic": AnthropicFormatter,
    "deepseek": DeepSeekFormatter,
    "glm": GLMFormatter,
    "openai": OpenAIFOrmatter,
    "qwen": QwenFormatter,
}


class _FixtureTool:
    """Tool for the shared tool definitions (name, description, input_schema)."""

    def __init__(self, spec: dict):
        self._name = spec["name"]
        self._description = spec["description"]
        self._schema = spec.get("input_schema")

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def input_schema(self) -> dict | None:
        return self._schema


def _toolformat_fixtures() -> list[tuple[str, dict]]:
    return sorted(
        (p.name, json.loads(p.read_text(encoding="utf-8")))
        for p in (FIXTURES / "toolformat").glob("*-cases.json")
    )


@pytest.mark.parametrize(
    "fixture, data",
    _toolformat_fixtures(),
    ids=[f for f, _ in _toolformat_fixtures()],
)
def test_toolformat_fixtures(fixture: str, data: dict) -> None:
    family = data["family"]
    fmt = TOOLFORMAT_FORMATTERS[family]()
    tools_json = json.loads((FIXTURES / "toolformat" / "tools.json").read_text(encoding="utf-8"))
    tools = [_FixtureTool(spec) for spec in tools_json["tools"]]

    for case in data.get("format_definitions") or []:
        actual = fmt.format_tool_definitions(tools)
        assert actual == case["expected"], f"{fixture} format_definitions/{case['name']}"
        record(f"toolformat/{fixture}", "format_definitions", case["name"])

    for case in data.get("parse_calls") or []:
        calls = fmt.parse_tool_calls(case["input"])
        actual = [{"id": c.id, "name": c.name, "args": c.args} for c in calls]
        expected = [
            {"id": e.get("id", ""), "name": e["name"], "args": e["args"]} for e in case["expected"]
        ]
        assert actual == expected, f"{fixture} parse_calls/{case['name']}"
        record(f"toolformat/{fixture}", "parse_calls", case["name"])

    for case in data.get("format_result") or []:
        spec = case["result"]
        if spec["success"]:
            result = Result(success=True, data=spec.get("output"))
        else:
            result = Result(success=False, error=spec.get("error"))
        actual = fmt.format_tool_result(case["tool_name"], result)
        assert actual == case["expected"], f"{fixture} format_result/{case['name']}"
        record(f"toolformat/{fixture}", "format_result", case["name"])

    for case in data.get("validate_format") or []:
        error = fmt.validate_format(case["input"])
        assert (error is None) is case["valid"], f"{fixture} validate_format/{case['name']}: {error}"
        record(f"toolformat/{fixture}", "validate_format", case["name"])
