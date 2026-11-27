"""GPT-OSS Harmony prompt renderer and completion parser.

The formatter's four standard sections replay in ``toolformat_fixtures_test.py``.
This file replays the Harmony-only sections of
``fixtures/toolformat/harmony-cases.json``: ``render_prompt`` (whole
conversations, rendered for raw ``/v1/completions``) and ``parse_completion``
(raw completion text back into messages, reasoning, content and calls).
"""

import json
from pathlib import Path
from typing import Any

import pytest

from haikeilabs.agentware.toolformat import (
    HarmonyFormatter,
    OpenAIFOrmatter,
    get_formatter,
)
from haikeilabs.agentware.toolformat.harmony import (
    HARMONY_STOP_TOKENS,
    HarmonyMessage,
    HarmonySystem,
    parse_harmony_completion,
    render_harmony_message,
    render_harmony_prompt,
)

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "toolformat" / "harmony-cases.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))


class _Tool:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.name = spec["name"]
        self.description = spec["description"]
        self.input_schema = spec.get("input_schema")


def _message(spec: dict[str, Any]) -> HarmonyMessage:
    return HarmonyMessage(
        role=spec["role"],
        content=spec["content"],
        channel=spec.get("channel", ""),
        recipient=spec.get("recipient", ""),
        content_type=spec.get("content_type", ""),
        name=spec.get("name", ""),
    )


def _system(spec: dict[str, Any] | None) -> HarmonySystem | None:
    if spec is None:
        return None
    fields = dict(spec)
    if "valid_channels" in fields:
        fields["valid_channels"] = tuple(fields["valid_channels"])
    return HarmonySystem(**fields)


@pytest.mark.parametrize("case", DATA["render_prompt"], ids=lambda c: c["name"])
def test_render_prompt(case: dict[str, Any]) -> None:
    actual = render_harmony_prompt(
        [_message(m) for m in case["messages"]],
        system=_system(case.get("system", {})),
        instructions=case.get("instructions", ""),
        tools=[_Tool(t) for t in case.get("tools") or []],
        auto_drop_analysis=case.get("auto_drop_analysis", True),
        next_role=case.get("next_role", "assistant"),
    )
    assert actual == case["expected"]


@pytest.mark.parametrize("case", DATA["parse_completion"], ids=lambda c: c["name"])
def test_parse_completion(case: dict[str, Any]) -> None:
    parsed = parse_harmony_completion(case["input"])
    want = case["expected"]
    assert parsed.reasoning == want["reasoning"]
    assert parsed.content == want["content"]
    assert [{"name": c.name, "args": c.args} for c in parsed.tool_calls] == want["tool_calls"]
    assert [
        {
            "role": m.role,
            "name": m.name,
            "recipient": m.recipient,
            "channel": m.channel,
            "content_type": m.content_type,
            "content": m.content,
        }
        for m in parsed.messages
    ] == want["messages"]


def test_parsed_messages_render_back_into_history() -> None:
    # A parsed call renders in the harmony library's canonical form, so the
    # next prompt can carry the model's turn without keeping its raw text.
    case = next(c for c in DATA["parse_calls"] if c["name"].startswith("course sample"))
    analysis, call = parse_harmony_completion(case["input"]).messages
    assert render_harmony_message(analysis) == (
        "<|start|>assistant<|channel|>analysis<|message|>Need weather for Tokyo.<|end|>"
    )
    assert render_harmony_message(call) == (
        "<|start|>assistant to=functions.get_weather<|channel|>commentary <|constrain|>json"
        '<|message|>{"city":"Tokyo"}<|call|>'
    )


def test_return_token_becomes_end_in_history() -> None:
    final = parse_harmony_completion("<|channel|>final<|message|>done<|return|>").messages[0]
    assert render_harmony_message(final) == (
        "<|start|>assistant<|channel|>final<|message|>done<|end|>"
    )


def test_stop_tokens() -> None:
    assert HARMONY_STOP_TOKENS == ("<|return|>", "<|call|>")


@pytest.mark.parametrize("model", ["openai/gpt-oss-20b", "gpt-oss-120b", "ggml-org/gpt-oss-20b-GGUF"])
def test_harmony_is_opt_in(model: str) -> None:
    # Chat-completions users keep the OpenAI formatter; raw-completions
    # callers construct HarmonyFormatter themselves.
    assert isinstance(get_formatter(model), OpenAIFOrmatter)
    assert HarmonyFormatter().model_family() == "gpt-oss"


def test_no_tools_renders_nothing() -> None:
    assert HarmonyFormatter().format_tool_definitions([]) == ""


def test_tool_without_schema() -> None:
    tool = _Tool({"name": "get_location", "description": "Gets the location of the user."})
    assert "type get_location = () => any;" in HarmonyFormatter().format_tool_definitions([tool])
