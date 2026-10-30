"""Shared-fixture conformance runner for the reasoning family.

Replays every case in ``fixtures/reasoning/*-cases.json`` through the
Python reasoning adapter and records each passing case for the
equal-coverage gate (``scripts/conformance/gate.py``, see
``docs/testing.md``). The test id contains ``reasoning`` so the
conformance workflow's ``-k`` filter selects it.

The fixture's expected output is the adapter's analysis of the raw model
response (extracted reasoning + clean content). The analysis is the seam
shared by ``strip``/``extract``/``parse``; ``parse`` only exposes the
clean side and a summarized tree, so the runner asserts on the analysis
directly.

``EXCLUDED`` mirrors the python rows for this family in
``fixtures/EXCLUSIONS.md``; keep the two in sync.
"""

import json
from pathlib import Path

import pytest
from conformance import record

from haikeilabs.agentware.reasoning import (
    ReasoningAdapter,
    ReasoningError,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"

# Cases the Python adapter cannot run, mirrored from fixtures/EXCLUSIONS.md
# (python rows for the reasoning family).
EXCLUDED = {
    # The fixture expects the extracted reasoning untrimmed (leading and
    # trailing newlines); all implementations trim it. Its expected clean
    # also drops the leading space the raw response carries before [THINK].
    "deepseek-cases.json#text mode with THINK tags",
    # The fixture expects the inline <thinking> tags to remain in the clean
    # content; all implementations strip them when a native reasoning field
    # is present.
    "qwen-cases.json#reasoning_content with thinking tags",
    "qwen-cases.json#text response with thinking tags",
}


def _reasoning_fixtures() -> list[tuple[str, dict]]:
    return sorted(
        (p.name, json.loads(p.read_text(encoding="utf-8")))
        for p in (FIXTURES / "reasoning").glob("*-cases.json")
    )


@pytest.mark.parametrize(
    "fixture, data",
    _reasoning_fixtures(),
    ids=[f for f, _ in _reasoning_fixtures()],
)
def test_reasoning_fixtures(fixture: str, data: dict) -> None:
    adapter = ReasoningAdapter()

    for case in data.get("examples") or []:
        if f"{fixture}#{case['name']}" in EXCLUDED:
            continue

        raw = case["raw_response"]
        expected_reasoning = case["expected_extracted_reasoning"]
        expected_clean = case["expected_clean_content"]

        if expected_reasoning is None and expected_clean is None:
            # Fail-closed case: the adapter must raise.
            with pytest.raises(ReasoningError):
                adapter.parse(raw, data["family"], "fixture")
        else:
            analysis = adapter._analyze(raw, "")
            assert analysis.reasoning == (expected_reasoning or ""), (
                f"{fixture} examples/{case['name']}: reasoning"
            )
            assert analysis.clean == (expected_clean or ""), (
                f"{fixture} examples/{case['name']}: clean"
            )

        record(f"reasoning/{fixture}", "examples", case["name"])
