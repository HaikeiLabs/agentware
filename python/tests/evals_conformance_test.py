"""Conformance fixtures for the ``agentware.eval-suite.v1`` loader and grader.

Every fixture in ``fixtures/evals/suite-v1/`` is a self-contained JSON bundle:
a full suite, the recorded model turns each case replays, and the expected
per-case outcome (pass, or fail with the failing check's message fragment).
This test replays every fixture through the real Python loader and
``EvalRunner.run_table_case`` with a scripted client (no model, no network),
so a port of the grader can be checked against the same expectations.

The fixture format is specified in ``fixtures/evals/suite-v1/README.md`` and
summarised in ``docs/evals/README.md``.
"""

import json
from pathlib import Path

import pytest

from haikeilabs.agentware.evals.models import BaseModelClient, ModelResult
from haikeilabs.agentware.evals.runner import EvalRunner
from haikeilabs.agentware.evals.suite import SuiteValidationError, load_suite

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "evals" / "suite-v1"


def _fixture_files(pattern: str) -> list[Path]:
    return sorted(FIXTURES_DIR.glob(pattern))


def test_conformance_fixture_set_is_populated() -> None:
    valid = _fixture_files("*.fixture.json")
    invalid = _fixture_files("*.invalid.json")
    assert valid, f"no *.fixture.json files in {FIXTURES_DIR}"
    assert invalid, f"no *.invalid.json files in {FIXTURES_DIR}"


def _write_suite(bundle: dict, tmp_path: Path) -> Path:
    """The bundle's embedded suite, as a standalone file ``load_suite`` reads."""
    suite_file = tmp_path / "suite.json"
    suite_file.write_text(json.dumps(bundle["suite"]), encoding="utf-8")
    return suite_file


class _RecordedClient(BaseModelClient):
    """Serves one case's recorded model turns, in order.

    A turn is ``{"reply": "..."}`` or ``{"tool_calls": [{"name", "arguments"}]}``;
    ``arguments`` may be a JSON object (serialized here, as on the wire) or a
    pre-serialized string. When the runner asks for more turns than recorded,
    it gets a plain ``"done"`` reply: a grader that over-runs a fixture's
    script changes the case outcome and fails the fixture, which is how the
    stop-rule fixtures catch a loop that should have ended earlier.
    """

    def __init__(self, turns: list[dict]) -> None:
        self._turns = list(turns)

    def complete(self, messages, tools=None, **kwargs) -> ModelResult:  # noqa: ANN001, ANN003
        turn = self._turns.pop(0) if self._turns else {"reply": "done"}
        calls = []
        for i, tc in enumerate(turn.get("tool_calls") or []):
            args = tc.get("arguments", {})
            if not isinstance(args, str):
                args = json.dumps(args)
            calls.append({"id": f"call_{i}", "name": tc["name"], "arguments": args})
        return ModelResult(
            content=turn.get("reply", ""),
            tool_calls=calls,
            finish_reason=turn.get("finish_reason", "stop"),
            usage={},
        )


def _tool_executor(name: str, args: dict) -> str:
    return json.dumps({"ok": True, "tool": name, "args": args})


@pytest.mark.parametrize("path", _fixture_files("*.fixture.json"), ids=lambda p: p.stem)
def test_suite_v1_fixture(path: Path, tmp_path: Path) -> None:
    bundle = json.loads(path.read_text(encoding="utf-8"))
    suite = load_suite(_write_suite(bundle, tmp_path))
    expected = {entry["id"]: entry for entry in bundle["expected"]}
    assert {case.name for case in suite.cases} == set(expected), (
        f"{path.name}: expected entries must cover the suite cases exactly"
    )
    max_turns = bundle.get("runner", {}).get("max_turns", 10)

    for case in suite.cases:
        turns = bundle["turns"][case.name]
        runner = EvalRunner(
            base_url="stub",
            max_turns=max_turns,
            client_factory=lambda _model, turns=turns: _RecordedClient(turns),
        )
        result = runner.run_table_case(case, model="stub", tool_executor=_tool_executor)
        want = expected[case.name]
        assert result.errored is False, f"{path.name}#{case.name}: {result.error}"
        assert result.success == want["passed"], (
            f"{path.name}#{case.name}: passed={result.success}, "
            f"error={result.error!r}, turns={result.turns}"
        )
        if not want["passed"]:
            assert want["error_contains"] in result.error, (
                f"{path.name}#{case.name}: {want['error_contains']!r} not in {result.error!r}"
            )
        if "turns" in want:
            assert result.turns == want["turns"], (
                f"{path.name}#{case.name}: {result.turns} turns, expected {want['turns']}"
            )


@pytest.mark.parametrize("path", _fixture_files("*.invalid.json"), ids=lambda p: p.stem)
def test_suite_v1_invalid_fixture(path: Path, tmp_path: Path) -> None:
    bundle = json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(SuiteValidationError) as excinfo:
        load_suite(_write_suite(bundle, tmp_path))
    assert bundle["error_contains"] in str(excinfo.value), (
        f"{path.name}: {bundle['error_contains']!r} not in {excinfo.value}"
    )
