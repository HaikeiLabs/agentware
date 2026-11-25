"""Shared reasoning-tag constants (``haikeilabs.agentware.reasoning.tags``).

The expected tag names are spelled without their delimiters on purpose: the
tags themselves are never typed literally (see ``reasoning/tags.py``).
"""

import json
from pathlib import Path

import pytest

from haikeilabs.agentware.middleware.guardrails.response_validator import (
    ResponseValidator,
    ToolCall,
)
from haikeilabs.agentware.reasoning import (
    BRACKET_THINK_CLOSE,
    BRACKET_THINK_OPEN,
    MMTHINK_CLOSE,
    MMTHINK_OPEN,
    REASONING_TAG_PAIRS,
    THINK_CLOSE,
    THINK_OPEN,
    THINKING_CLOSE,
    THINKING_OPEN,
    ReasoningAdapter,
    ReasoningOptions,
    UnbalancedThinkingError,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def test_tag_pairs_cover_think_mmthink_thinking_and_bracket_think() -> None:
    want = [
        ("<", "think", ">"),
        ("<", "mm:think", ">"),
        ("<", "thinking", ">"),
        ("[", "THINK", "]"),
    ]
    assert len(REASONING_TAG_PAIRS) == 4
    for (open_tag, close_tag), (lead, name, trail) in zip(REASONING_TAG_PAIRS, want):
        assert open_tag == lead + name + trail
        assert close_tag == lead + "/" + name + trail


def test_tag_constants_match_pairs() -> None:
    assert REASONING_TAG_PAIRS == (
        (THINK_OPEN, THINK_CLOSE),
        (MMTHINK_OPEN, MMTHINK_CLOSE),
        (THINKING_OPEN, THINKING_CLOSE),
        (BRACKET_THINK_OPEN, BRACKET_THINK_CLOSE),
    )


@pytest.mark.parametrize("pair", REASONING_TAG_PAIRS)
@pytest.mark.parametrize("upper", [False, True])
def test_adapter_strips_every_tag_pair(pair: tuple[str, str], upper: bool) -> None:
    open_tag, close_tag = (t.upper() for t in pair) if upper else pair
    clean = ReasoningAdapter().strip(f"{open_tag}\nplan the call\n{close_tag}answer")
    assert clean == "answer"


def test_adapter_fails_closed_on_unclosed_think() -> None:
    with pytest.raises(UnbalancedThinkingError):
        ReasoningAdapter().strip(THINK_OPEN + "never closed")


def test_adapter_fails_closed_on_stray_mmthink_close_by_default() -> None:
    # MiniMax M2 emits the close tag without the open tag; without the opt-in
    # the adapter stays fail-closed.
    with pytest.raises(UnbalancedThinkingError):
        ReasoningAdapter().strip("reasoning" + MMTHINK_CLOSE + "answer")


def test_adapter_implicit_open_treats_leading_think_close_as_reasoning() -> None:
    adapter = ReasoningAdapter(ReasoningOptions(implicit_open=True))
    clean = adapter.strip("reasoning" + THINK_CLOSE + "answer")
    assert clean == "answer"


def test_adapter_implicit_open_counts_mmthink_close() -> None:
    adapter = ReasoningAdapter(ReasoningOptions(implicit_open=True))
    analysis = adapter._analyze("reasoning" + MMTHINK_CLOSE + "answer", "")
    assert analysis.clean == "answer"
    assert analysis.reasoning == "reasoning"


def test_adapter_implicit_open_still_fails_closed_on_other_close_tags() -> None:
    # Close-only mode counts only the plain think and mm:think closes.
    adapter = ReasoningAdapter(ReasoningOptions(implicit_open=True))
    with pytest.raises(UnbalancedThinkingError):
        adapter.strip("reasoning" + THINKING_CLOSE + "answer")


def _validator_cases() -> tuple[list[str], list[dict]]:
    data = json.loads((FIXTURES / "reasoning" / "validator-rescue.v1.json").read_text("utf-8"))
    return data["tool_names"], data["cases"]


_TOOL_NAMES, _CASES = _validator_cases()


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_response_validator_shared_reasoning_fixture(case: dict) -> None:
    rv = ResponseValidator(_TOOL_NAMES, rescue_enabled=True)
    result = rv.validate_text_response(case["response"])
    want = [ToolCall(tool=c["tool"], args=c["args"]) for c in case["expected_tool_calls"]]
    if not want:
        assert result.needs_retry is True
        assert result.tool_calls == []
    else:
        assert result.needs_retry is False
        assert result.tool_calls == want
