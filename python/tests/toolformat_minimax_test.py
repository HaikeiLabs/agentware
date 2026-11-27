"""MiniMax formatter checks the shared fixture runner does not cover.

The shared runner (``toolformat_fixtures_test.py``) replays
``fixtures/toolformat/minimax-cases.json`` through the default (M2)
formatter. This file checks the M3 variant against the fixture's
``m3_format_definitions`` section and the selector routing for MiniMax ids.
"""

import json
from pathlib import Path

import pytest

from haikeilabs.agentware.toolformat import MiniMaxFormatter, get_formatter
from haikeilabs.agentware.tools import Result

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "toolformat"
CASES = json.loads((FIXTURES / "minimax-cases.json").read_text(encoding="utf-8"))


class _Tool:
    def __init__(self, spec: dict):
        self.name = spec["name"]
        self.description = spec["description"]
        self.input_schema = spec.get("input_schema")


def _tools() -> list[_Tool]:
    data = json.loads((FIXTURES / "tools.json").read_text(encoding="utf-8"))
    return [_Tool(spec) for spec in data["tools"]]


@pytest.mark.parametrize("case", CASES["m3_format_definitions"], ids=lambda c: c["name"])
def test_m3_format_definitions(case: dict) -> None:
    assert MiniMaxFormatter(version="m3").format_tool_definitions(_tools()) == case["expected"]


def test_m3_variant_parses_validates_and_formats_like_m2() -> None:
    m2, m3 = MiniMaxFormatter(), MiniMaxFormatter(version="m3")
    for case in CASES["parse_calls"]:
        name = case["name"]
        assert m3.parse_tool_calls(case["input"]) == m2.parse_tool_calls(case["input"]), name
    for case in CASES["validate_format"]:
        assert (m3.validate_format(case["input"]) is None) is case["valid"], case["name"]
    result = Result(success=True, data="Sunny")
    assert m3.format_tool_result("get_weather", result) == "<response>Sunny</response>"


def test_empty_tool_list_renders_nothing() -> None:
    assert MiniMaxFormatter().format_tool_definitions([]) == ""
    assert MiniMaxFormatter(version="m3").format_tool_definitions([]) == ""


def test_unknown_version_is_rejected() -> None:
    with pytest.raises(ValueError):
        MiniMaxFormatter(version="m1")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "model, version",
    [
        ("MiniMax-M2", "m2"),
        ("MiniMaxAI/MiniMax-M2.5", "m2"),
        ("minimax-m2.1", "m2"),
        ("MiniMax-M3", "m3"),
        ("MiniMaxAI/MiniMax-M3", "m3"),
        ("minimax-m3-preview", "m3"),
    ],
)
def test_get_formatter_routes_minimax_ids(model: str, version: str) -> None:
    formatter = get_formatter(model)
    assert isinstance(formatter, MiniMaxFormatter)
    assert formatter.model_family() == "minimax"
    assert formatter.version == version


@pytest.mark.parametrize("model", ["MiniMax-Text-01", "minimax-7b", "MiniMax-M1"])
def test_other_minimax_ids_are_not_routed(model: str) -> None:
    assert not isinstance(get_formatter(model), MiniMaxFormatter)
