"""Replays the shared eval-expectations fixture (``fixtures/evals/expectations``).

Every model turn comes from a scripted client, so these are deterministic and
need no endpoint. Go, TypeScript and Rust replay the same cases.
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from evals.models import ModelResult
from evals.runner import EvalCase, EvalReport, EvalRunner, case_from_contract

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/evals/expectations/cases.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))


class _ScriptedClient:
    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self.turns = turns
        self.consumed = 0

    def complete(self, messages: list[dict[str, Any]], tools: Any = None, **_: Any) -> ModelResult:
        if self.consumed >= len(self.turns):
            raise RuntimeError("script exhausted")
        turn = self.turns[self.consumed]
        self.consumed += 1
        return ModelResult(
            content=turn.get("content", ""),
            tool_calls=[
                {
                    "id": f"call-{self.consumed}-{i}",
                    "name": tc["name"],
                    "arguments": json.dumps(tc["arguments"]),
                }
                for i, tc in enumerate(turn.get("tool_calls", []))
            ],
            finish_reason="",
            usage={},
        )


def _runner(client: _ScriptedClient) -> EvalRunner:
    return EvalRunner("http://unused", client_factory=lambda model: client)  # type: ignore[arg-type,return-value]


@pytest.mark.parametrize("entry", DATA["cases"], ids=[c["case"]["name"] for c in DATA["cases"]])
def test_fixture_case(entry: dict[str, Any]) -> None:
    turns = [t for repeat in entry["script"] for t in repeat]
    client = _ScriptedClient(turns)
    case = case_from_contract({**entry["case"], "tools": DATA["tools"]})
    result = _runner(client).run_case(case, "scripted", lambda name, args: "ok")

    want = entry["expected"]
    assert result.success is want["success"], result.error
    assert result.passes == want["passes"]
    assert result.repeats == want["repeats"]
    assert result.turns == want["turns"]
    assert [tc["name"] for tc in result.tool_calls] == want["toolCalls"]
    if "error" in want:
        assert result.error == want["error"]
    if "errorContains" in want:
        assert want["errorContains"] in result.error
    assert client.consumed == len(turns)


def test_contract_defaults() -> None:
    case = case_from_contract(
        {
            "name": "n",
            "description": "d",
            "systemPrompt": "s",
            "userMessage": "u",
            "tools": [],
            "expectedTool": "t",
            "maxTurns": 2,
        }
    )
    assert case.expect == "call"
    assert case.expected_args == {}
    assert case.repeats == 1
    assert case.min_pass_rate == 1.0
    assert case.max_turns == 2


def _base_case(**overrides: Any) -> EvalCase:
    fields: dict[str, Any] = dict(
        name="n",
        description="d",
        system_prompt="s",
        user_message="u",
        tools=DATA["tools"],
        expected_tool="get_weather",
        max_turns=1,
    )
    fields.update(overrides)
    return EvalCase(**fields)


def _saved_row(tmp_path: Path, result: Any) -> dict[str, Any]:
    out = tmp_path / "report.json"
    runner = _runner(_ScriptedClient([]))
    runner.save_report(EvalReport(timestamp="t", models=["m"], results=[result]), out)
    return json.loads(out.read_text())["results"][0]  # type: ignore[no-any-return]


def test_single_run_report_shape_unchanged(tmp_path: Path) -> None:
    client = _ScriptedClient([{"content": "hi"}])
    result = _runner(client).run_case(_base_case(), "scripted", lambda n, a: "ok")
    assert list(_saved_row(tmp_path, result)) == [
        "case",
        "model",
        "success",
        "turns",
        "tool_calls",
        "error",
        "duration_ms",
    ]


def test_repeated_report_adds_passes_and_repeats(tmp_path: Path) -> None:
    client = _ScriptedClient([{"content": "a"}, {"content": "b"}])
    result = _runner(client).run_case(
        _base_case(expect="no_call", repeats=2), "scripted", lambda n, a: "ok"
    )
    row = _saved_row(tmp_path, result)
    assert row["passes"] == 2
    assert row["repeats"] == 2
