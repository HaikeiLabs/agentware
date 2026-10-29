"""Shared ToolSpec contract fixture replay."""

import json
from pathlib import Path

import pytest
from conformance import record

from haikeilabs.agentware.toolformat import (
    AnthropicFormatter,
    DeepSeekFormatter,
    GenericFormatter,
    GLMFormatter,
    OpenAIFOrmatter,
    QwenFormatter,
)
from haikeilabs.agentware.toolspec import ToolSpec, serialize_tool_specs

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "toolspec" / "cases.v1.json"


def test_cases_v1_replay_wire_and_json_toolformat() -> None:
    fixture = json.loads(FIXTURE.read_text())
    cases = fixture["cases"]
    comparisons = fixture.get("toolformat_compare", {})
    formatters = {
        "generic": GenericFormatter(),
        "openai": OpenAIFOrmatter(),
        "deepseek": DeepSeekFormatter(),
        "glm": GLMFormatter(),
        "anthropic": AnthropicFormatter(),
        "qwen": QwenFormatter(),
    }
    assert len(cases) == 4
    for case in cases:
        spec = ToolSpec.from_dict(case["spec"])
        if "expected_wire" in case:
            assert serialize_tool_specs([spec])["tools"][0] == case["expected_wire"]
        if "expected_toolformat" in case:
            for flavor, expected in case["expected_toolformat"].items():
                default_comparison = (
                    "json"
                    if flavor in {"generic", "openai", "deepseek", "glm", "anthropic"}
                    else "text"
                )
                if comparisons.get(flavor, default_comparison) != "json":
                    continue
                actual = formatters[flavor].format_tool_definitions([spec])
                assert json.loads(actual) == json.loads(expected), (case["name"], flavor)
        if "expected_error" in case:
            with pytest.raises(ValueError, match=f"^{case['expected_error']}$"):
                serialize_tool_specs([spec])
        record("toolspec/cases.v1.json", "cases", case["name"])


def test_tool_spec_converts_to_legacy_definition() -> None:
    spec = ToolSpec(name="lookup", description="Lookup", parameters={"type": "object"})
    legacy = spec.to_tool_definition()
    assert legacy.name == spec.name
    assert legacy.input_schema == spec.parameters


def test_each_tool_spec_matches_its_tool_definition_for_every_flavor() -> None:
    formatters = [
        GenericFormatter(),
        OpenAIFOrmatter(),
        DeepSeekFormatter(),
        GLMFormatter(),
        AnthropicFormatter(),
        QwenFormatter(),
    ]
    cases = json.loads(FIXTURE.read_text())["cases"]
    for case in cases:
        spec = ToolSpec.from_dict(case["spec"])
        definition = spec.to_tool_definition()
        for formatter in formatters:
            assert formatter.format_tool_definitions([spec]) == formatter.format_tool_definitions(
                [definition]
            ), (case["name"], formatter.model_family())


@pytest.mark.parametrize(
    ("field_name", "message"),
    [
        ("service", "service is required"),
        ("source", "source is required"),
        ("operation_class", "operation_class is required"),
        ("route", "route is required"),
    ],
)
def test_serializer_requires_explicit_wire_metadata(field_name: str, message: str) -> None:
    values = {
        "name": "missing.metadata",
        "service": "sample",
        "source": "sample",
        "operation_class": "read",
        "route": {"harness_executor": {"executor": "test", "registration": "test"}},
    }
    del values[field_name]
    spec = ToolSpec(**values)
    with pytest.raises(ValueError, match=f"^{message}$"):
        serialize_tool_specs([spec])
