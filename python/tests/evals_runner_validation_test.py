"""Unit tests for eval tool-call validation and endpoint preflight.

These cover the *harness*, not a model: every model response here comes from a
local stub client, so the tests are deterministic and need no endpoint. They
are separate from both the model-driven evals (which need a real endpoint) and
the authorization/tenancy tests in ``beta_tool_authorization_test.py``.
"""

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from evals.cases.beta_tools import BETA_TOOL_CASES, BETA_TOOLS, DELEGATED_SCOPING
from evals.models import ModelResult
from evals.runner import (
    EndpointUnavailableError,
    EvalCase,
    EvalRunner,
    validate_tool_call,
)


def _case(**overrides: Any) -> EvalCase:
    base: dict[str, Any] = dict(
        name="c",
        description="d",
        system_prompt="s",
        user_message="u",
        tools=BETA_TOOLS,
        expected_tool="github.get_issue",
    )
    base.update(overrides)
    return EvalCase(**base)


class TestValidateToolCall:
    def test_accepts_correct_call(self) -> None:
        ok, reason = validate_tool_call(
            _case(expected_args={"issue_number": 412}),
            "github.get_issue",
            '{"issue_number": 412}',
        )
        assert ok, reason

    def test_rejects_wrong_tool(self) -> None:
        ok, reason = validate_tool_call(_case(), "s3.list_objects", "{}")
        assert not ok
        assert "expected 'github.get_issue'" in reason

    def test_rejects_malformed_json(self) -> None:
        ok, reason = validate_tool_call(_case(), "github.get_issue", "{not json")
        assert not ok
        assert "not valid JSON" in reason

    def test_rejects_non_object_arguments(self) -> None:
        ok, reason = validate_tool_call(_case(), "github.get_issue", "[1, 2]")
        assert not ok
        assert "not a JSON object" in reason

    def test_rejects_missing_schema_required_argument(self) -> None:
        ok, reason = validate_tool_call(_case(), "github.get_issue", "{}")
        assert not ok
        assert "missing schema-required arguments" in reason
        assert "issue_number" in reason

    def test_rejects_argument_not_in_schema(self) -> None:
        # A delegated scoping field the schema never declared.
        ok, reason = validate_tool_call(
            _case(),
            "github.get_issue",
            '{"issue_number": 1, "repository": "competitor/private"}',
        )
        assert not ok
        assert "not declared in the tool schema" in reason

    def test_rejects_wrong_argument_value(self) -> None:
        ok, reason = validate_tool_call(
            _case(expected_args={"issue_number": 412}),
            "github.get_issue",
            '{"issue_number": 999}',
        )
        assert not ok
        assert "expected 412" in reason

    def test_rejects_missing_required_key(self) -> None:
        ok, reason = validate_tool_call(
            _case(expected_tool="s3.list_objects", required_arg_keys=["prefix"]),
            "s3.list_objects",
            "{}",
        )
        assert not ok
        assert "missing expected arguments" in reason

    def test_rejects_forbidden_delegated_scoping(self) -> None:
        # Schema-undeclared fields are caught first; this pins the explicit
        # forbidden-key check using a field the schema does declare.
        ok, reason = validate_tool_call(
            _case(expected_tool="s3.list_objects", forbidden_arg_keys=["prefix"]),
            "s3.list_objects",
            '{"prefix": "other-tenant/"}',
        )
        assert not ok
        assert "forbidden arguments present" in reason

    def test_empty_arguments_string_is_an_empty_object(self) -> None:
        ok, reason = validate_tool_call(
            _case(expected_tool="linear.list_issues"), "linear.list_issues", ""
        )
        assert ok, reason


class TestBetaCasesCarryAssertions:
    def test_every_beta_case_asserts_arguments(self) -> None:
        for case in BETA_TOOL_CASES:
            assert (
                case.expected_args or case.required_arg_keys or case.forbidden_arg_keys
            ), f"{case.name} asserts tool selection only"

    def test_connector_cases_forbid_delegated_scoping(self) -> None:
        connector_cases = [c for c in BETA_TOOL_CASES if "." in c.expected_tool]
        assert connector_cases
        for case in connector_cases:
            assert set(DELEGATED_SCOPING) <= set(case.forbidden_arg_keys), case.name


class _StubClient:
    """Minimal model client: replays queued responses, records calls."""

    def __init__(self, responses: list[ModelResult], fail: Exception | None = None):
        self.responses = list(responses)
        self.fail = fail
        self.calls = 0

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> ModelResult:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        if self.responses:
            return self.responses.pop(0)
        return ModelResult(content="done", tool_calls=[], finish_reason="stop", usage={})


def _tool_call(name: str, arguments: str) -> ModelResult:
    return ModelResult(
        content="",
        tool_calls=[{"id": "1", "name": name, "arguments": arguments}],
        finish_reason="tool_calls",
        usage={},
    )


def _install(monkeypatch: pytest.MonkeyPatch, client: _StubClient) -> None:
    monkeypatch.setattr(
        "evals.runner.create_model_client", lambda *args, **kwargs: client
    )


class TestRunnerScoring:
    def test_correct_arguments_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _StubClient([_tool_call("github.get_issue", '{"issue_number": 412}')]))
        runner = EvalRunner(base_url="stub")
        result = runner.run_case(
            _case(expected_args={"issue_number": 412}), "stub-model", lambda n, a: "{}"
        )
        assert result.success
        assert result.error == ""

    def test_right_tool_wrong_arguments_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _StubClient([_tool_call("github.get_issue", '{"issue_number": 1}')]))
        runner = EvalRunner(base_url="stub")
        result = runner.run_case(
            _case(expected_args={"issue_number": 412}), "stub-model", lambda n, a: "{}"
        )
        assert not result.success
        assert "invalid call" in result.error

    def test_smuggled_scoping_argument_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            _StubClient(
                [_tool_call("github.get_issue", '{"issue_number": 7, "repository": "other/repo"}')]
            ),
        )
        runner = EvalRunner(base_url="stub")
        result = runner.run_case(
            _case(forbidden_arg_keys=DELEGATED_SCOPING), "stub-model", lambda n, a: "{}"
        )
        assert not result.success

    def test_malformed_arguments_do_not_crash_the_runner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A bad payload for a non-expected tool must not raise out of run_case.
        _install(
            monkeypatch,
            _StubClient(
                [_tool_call("s3.list_objects", "{broken"), _tool_call("github.get_issue", "{}")]
            ),
        )
        runner = EvalRunner(base_url="stub")
        result = runner.run_case(_case(), "stub-model", lambda n, a: "{}")
        assert not result.success


class TestPreflight:
    def test_unreachable_endpoint_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _StubClient([], fail=OSError("connection refused")))
        runner = EvalRunner(base_url="http://127.0.0.1:9/v1")
        with pytest.raises(EndpointUnavailableError) as exc:
            runner.preflight("some-model")
        assert "not reachable" in str(exc.value)

    def test_run_evals_surfaces_blocked_run_instead_of_zero_score(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _StubClient([], fail=OSError("connection refused")))
        runner = EvalRunner(base_url="http://127.0.0.1:9/v1")
        with pytest.raises(EndpointUnavailableError):
            runner.run_evals(BETA_TOOL_CASES[:2], ["some-model"], lambda n, a: "{}")

    def test_reachable_endpoint_passes_preflight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _StubClient([]))
        EvalRunner(base_url="stub").preflight("stub-model")
