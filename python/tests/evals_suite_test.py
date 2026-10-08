"""Unit tests for the EV-C1 table-test runner.

Covers the ``agentware.eval-suite.v1`` loader, every expectation type, the
deny path through a real agentware ``Policy``, repeats, model-profile
resolution, CLI exit codes, and the ``haikei.eval-benchmark.v1`` output.
Every model response comes from a scripted stub client; nothing here needs
an endpoint.
"""

import copy
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from evals import main as eval_main
from evals.benchmark import (
    BENCHMARK_SCHEMA,
    CaseOutcome,
    SuiteResult,
    build_benchmark,
    render_markdown,
    scrub,
)
from evals.cases.beta_tools import BETA_TOOL_CASES, BETA_TOOLS
from evals.expectations import ContentChecks, check_content
from evals.models import ModelBackend, ModelResult
from evals.profiles import ProfileError, load_profiles, resolve_profile
from evals.runner import EvalResult, EvalRunner
from evals.suite import SuiteValidationError, load_suite, load_suites
from evals.table import combine_repeats, exit_code, run_suite

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_SUITE = REPO_ROOT / "evals" / "suites" / "agentware.beta-tools.json"
CANONICAL_PROFILES = REPO_ROOT / "evals" / "model-profiles.yaml"

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "file_bug",
            "description": "File a bug.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "team_key": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crm_get_lead",
            "description": "Read a CRM lead.",
            "parameters": {
                "type": "object",
                "properties": {"lead_id": {"type": "string"}},
                "required": ["lead_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_wiki",
            "description": "Search the wiki.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
]


def _suite(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": "agentware.eval-suite.v1",
        "suite": "test.agent",
        "kind": "agent",
        "system_prompt": "You are a test agent.",
        "tools": copy.deepcopy(_TOOLS),
        "cases": [
            {
                "id": "file-bug",
                "prompt": "File a bug titled crash",
                "expect": {"tool": "file_bug", "args": {"team_key": "KEI"}},
            }
        ],
    }
    base.update(overrides)
    return base


def _write(tmp_path: Path, data: Any, name: str = "suite.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return path


def _load(tmp_path: Path, **overrides: Any):  # type: ignore[no-untyped-def]
    return load_suite(_write(tmp_path, _suite(**overrides)))


def _case(tmp_path: Path, case: dict[str, Any]):  # type: ignore[no-untyped-def]
    return _load(tmp_path, cases=[case]).cases[0]


# --- scripted model --------------------------------------------------------


def _call(name: str, args: dict[str, Any] | str, call_id: str = "c1") -> ModelResult:
    raw = args if isinstance(args, str) else json.dumps(args)
    return ModelResult(
        content="",
        tool_calls=[{"id": call_id, "name": name, "arguments": raw}],
        finish_reason="tool_calls",
        usage={},
    )


def _reply(text: str) -> ModelResult:
    return ModelResult(content=text, tool_calls=[], finish_reason="stop", usage={})


class _Script:
    """A model client replaying scripted turns; records what it was sent."""

    def __init__(self, turns: list[ModelResult], fail: Exception | None = None):
        self.turns = list(turns)
        self.fail = fail
        self.seen: list[list[dict[str, Any]]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> ModelResult:
        self.seen.append(copy.deepcopy(messages))
        if self.fail is not None:
            raise self.fail
        return self.turns.pop(0) if self.turns else _reply("done")


def _install(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    monkeypatch.setattr("evals.runner.create_model_client", lambda *a, **k: client)


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        return json.dumps({"status": "ok", "tool": name})


def _run(monkeypatch: pytest.MonkeyPatch, case: Any, turns: list[ModelResult]) -> EvalResult:
    _install(monkeypatch, _Script(turns))
    return EvalRunner(base_url="stub").run_table_case(case, "m", _RecordingExecutor())


# --- suite schema ------------------------------------------------------------


class TestSuiteSchemaGood:
    def test_reference_suite_loads(self) -> None:
        suite = load_suite(REFERENCE_SUITE)
        assert suite.name == "agentware.beta-tools"
        assert suite.kind == "agent"
        assert suite.repeats == 1
        assert len(suite.cases) == 24

    def test_cases_map_onto_eval_case(self, tmp_path: Path) -> None:
        case = _case(
            tmp_path,
            {
                "id": "x",
                "prompt": "p",
                "context": {"role": "member", "groups": ["default"]},
                "expect": {
                    "tool": "file_bug",
                    "args": {"team_key": "KEI"},
                    "required_arg_keys": ["title"],
                    "forbidden_arg_keys": ["tenant_id"],
                    "forbidden_tools": ["crm_get_lead"],
                    "content": {"contains_any": ["filed"]},
                },
            },
        )
        assert case.name == "x"
        assert case.user_message == "p"
        assert case.system_prompt == "You are a test agent."
        assert case.expected_tool == "file_bug"
        assert case.expected_args == {"team_key": "KEI"}
        assert case.required_arg_keys == ["title"]
        assert case.forbidden_arg_keys == ["tenant_id"]
        assert case.forbidden_tools == ["crm_get_lead"]
        assert case.content == ContentChecks(contains_any=("filed",))
        assert case.context["role"] == "member"
        assert case.allowed_tools is None
        assert not case.expect_no_tool_call

    def test_tool_null_vs_absent(self, tmp_path: Path) -> None:
        suite = _load(
            tmp_path,
            cases=[
                {"id": "null", "prompt": "hi", "expect": {"tool": None}},
                {"id": "absent", "prompt": "hi", "expect": {}},
            ],
        )
        null, absent = suite.cases
        assert null.expected_tool is None and null.expect_no_tool_call
        assert absent.expected_tool is None and not absent.expect_no_tool_call

    def test_system_prompt_file_is_relative_to_suite(self, tmp_path: Path) -> None:
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "agent.md").write_text("Prompt from file.")
        nested = tmp_path / "suites"
        nested.mkdir()
        data = _suite(system_prompt_file="../prompts/agent.md")
        del data["system_prompt"]
        suite = load_suite(_write(nested, data))
        assert suite.cases[0].system_prompt == "Prompt from file."

    def test_directory_loads_every_suite_sorted(self, tmp_path: Path) -> None:
        _write(tmp_path, _suite(suite="test.b"), "b.json")
        _write(tmp_path, _suite(suite="test.a"), "a.json")
        assert [s.name for s in load_suites(tmp_path)] == ["test.a", "test.b"]

    def test_context_allows_extra_keys(self, tmp_path: Path) -> None:
        case = _case(
            tmp_path,
            {"id": "x", "prompt": "p", "context": {"tenant": "t"}, "expect": {}},
        )
        assert case.context["tenant"] == "t"


class TestSuiteSchemaBad:
    @pytest.mark.parametrize(
        "overrides, message",
        [
            ({"schema": "agentware.eval-suite.v2"}, "schema"),
            ({"suite": "nodots"}, "dotted"),
            ({"suite": "Bad.Name"}, "dotted"),
            ({"kind": "workflow"}, "kind"),
            ({"repeats": 0}, "repeats"),
            ({"repeats": "2"}, "repeats"),
            ({"cases": []}, "cases"),
            ({"extra": 1}, "extra"),
            ({"system_prompt_file": "p.md"}, "exactly one"),
            ({"tools": [{"type": "function", "function": {"name": ""}}]}, "name"),
            ({"tools": _TOOLS + [_TOOLS[0]]}, "unique"),
        ],
    )
    def test_rejects_bad_suite(
        self, tmp_path: Path, overrides: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(SuiteValidationError, match=message):
            _load(tmp_path, **overrides)

    def test_rejects_missing_system_prompt(self, tmp_path: Path) -> None:
        data = _suite()
        del data["system_prompt"]
        with pytest.raises(SuiteValidationError, match="exactly one"):
            load_suite(_write(tmp_path, data))

    @pytest.mark.parametrize(
        "case, message",
        [
            ({"id": "Bad_ID", "prompt": "p", "expect": {}}, "case id"),
            ({"id": "-x", "prompt": "p", "expect": {}}, "case id"),
            ({"id": "x" * 65, "prompt": "p", "expect": {}}, "case id"),
            ({"id": "x", "prompt": "", "expect": {}}, "prompt"),
            ({"id": "x", "prompt": "p"}, "expect"),
            ({"id": "x", "prompt": "p", "expect": {}, "notes": "?"}, "notes"),
            ({"id": "x", "prompt": "p", "expect": {"tool": "nope"}}, "not a suite tool"),
            ({"id": "x", "prompt": "p", "expect": {"tool_name": "file_bug"}}, "tool_name"),
            ({"id": "x", "prompt": "p", "expect": {"args": {"a": 1}}}, "need expect.tool"),
            (
                {"id": "x", "prompt": "p", "expect": {"required_arg_keys": ["a"]}},
                "need expect.tool",
            ),
            (
                {"id": "x", "prompt": "p", "expect": {"content": {"contains": ["a"]}}},
                "contains",
            ),
            (
                {"id": "x", "prompt": "p", "expect": {"content": {"regex": ["("]}}},
                "invalid regex",
            ),
            ({"id": "x", "prompt": "p", "expect": {"deny": "yes"}}, "deny"),
            ({"id": "x", "prompt": "p", "expect": {"deny": True}}, "allowed_tools"),
            (
                {
                    "id": "x",
                    "prompt": "p",
                    "context": {"allowed_tools": ["file_bug"]},
                    "expect": {"tool": "file_bug", "deny": True},
                },
                "is allowed",
            ),
            (
                {
                    "id": "x",
                    "prompt": "p",
                    "context": {"allowed_tools": ["ghost"]},
                    "expect": {},
                },
                "allowed_tools not in suite tools",
            ),
            (
                {
                    "id": "x",
                    "prompt": "p",
                    "expect": {"tool": "file_bug", "forbidden_tools": ["file_bug"]},
                },
                "also in forbidden_tools",
            ),
        ],
    )
    def test_rejects_bad_case(
        self, tmp_path: Path, case: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(SuiteValidationError, match=message):
            _load(tmp_path, cases=[case])

    def test_rejects_duplicate_case_ids(self, tmp_path: Path) -> None:
        case = {"id": "x", "prompt": "p", "expect": {}}
        with pytest.raises(SuiteValidationError, match="duplicate case ids"):
            _load(tmp_path, cases=[case, case])

    def test_rejects_absolute_or_missing_prompt_file(self, tmp_path: Path) -> None:
        data = _suite(system_prompt_file=str(tmp_path / "abs.md"))
        del data["system_prompt"]
        (tmp_path / "abs.md").write_text("x")
        with pytest.raises(SuiteValidationError, match="must be relative"):
            load_suite(_write(tmp_path, data))
        data["system_prompt_file"] = "missing.md"
        with pytest.raises(SuiteValidationError, match="cannot read system_prompt_file"):
            load_suite(_write(tmp_path, data))

    def test_rejects_non_json_and_missing_paths(self, tmp_path: Path) -> None:
        with pytest.raises(SuiteValidationError, match="not valid JSON"):
            load_suite(_write(tmp_path, "{nope"))
        with pytest.raises(SuiteValidationError, match="no such suite"):
            load_suites(tmp_path / "absent.json")
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(SuiteValidationError, match="no \\*.json suites"):
            load_suites(empty)

    def test_rejects_duplicate_suite_names_across_files(self, tmp_path: Path) -> None:
        _write(tmp_path, _suite(), "a.json")
        _write(tmp_path, _suite(), "b.json")
        with pytest.raises(SuiteValidationError, match="duplicate suite names"):
            load_suites(tmp_path)


class TestReferenceSuiteParity:
    """The JSON reference suite stays in step with the Python beta cases."""

    def test_every_python_case_is_in_the_json_suite(self) -> None:
        by_id = {c.name: c for c in load_suite(REFERENCE_SUITE).cases}
        for py in BETA_TOOL_CASES:
            js = by_id[py.name]
            assert js.user_message == py.user_message
            assert js.system_prompt == py.system_prompt
            assert js.expected_tool == py.expected_tool
            assert js.expected_args == py.expected_args
            assert js.required_arg_keys == py.required_arg_keys
            assert js.forbidden_arg_keys == py.forbidden_arg_keys

    def test_tools_match_the_python_catalog(self) -> None:
        assert load_suite(REFERENCE_SUITE).cases[0].tools == BETA_TOOLS

    def test_denied_write_is_a_real_policy_deny(self) -> None:
        by_id = {c.name: c for c in load_suite(REFERENCE_SUITE).cases}
        case = by_id["beta_denied_write_without_permission"]
        assert case.expect_deny
        assert case.allowed_tools is not None
        assert "create_issue" not in case.allowed_tools
        assert case.content.not_contains

    def test_decline_prompts_are_cases(self) -> None:
        by_id = {c.name: c for c in load_suite(REFERENCE_SUITE).cases}
        assert by_id["beta_decline_missing_document_id"].forbidden_tools == [
            "docs.get_document"
        ]
        assert by_id["beta_decline_malformed_lead_email"].forbidden_tools == [
            "crm_create_lead"
        ]


# --- expectations ------------------------------------------------------------


class TestContentChecks:
    def test_empty_checks_pass(self) -> None:
        assert check_content(ContentChecks(), "") == (True, "")

    def test_contains_all_is_case_insensitive(self) -> None:
        checks = ContentChecks(contains_all=("Filed", "KEI-1"))
        assert check_content(checks, "filed as kei-1")[0]
        ok, reason = check_content(checks, "filed")
        assert not ok and "missing required phrases" in reason

    def test_contains_any(self) -> None:
        checks = ContentChecks(contains_any=("denied", "not allowed"))
        assert check_content(checks, "That is NOT ALLOWED.")[0]
        ok, reason = check_content(checks, "Sure!")
        assert not ok and "none of" in reason

    def test_regex(self) -> None:
        checks = ContentChecks(regex=(r"kei-\d+",))
        assert check_content(checks, "Created KEI-42")[0]
        ok, reason = check_content(checks, "Created it")
        assert not ok and "does not match regex" in reason

    def test_not_contains(self) -> None:
        checks = ContentChecks(not_contains=("Successfully created",))
        assert check_content(checks, "I could not create it")[0]
        ok, reason = check_content(checks, "successfully CREATED the issue")
        assert not ok and "forbidden phrases" in reason


class TestToolExpectations:
    def test_expected_tool_and_args_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(tmp_path, {"id": "x", "prompt": "p", "expect": {"tool": "file_bug"}})
        result = _run(monkeypatch, case, [_call("file_bug", {"title": "t"}), _reply("ok")])
        assert result.success, result.error
        assert result.turns == 1  # graded on the first tool-call message

    def test_first_call_must_be_the_expected_tool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(tmp_path, {"id": "x", "prompt": "p", "expect": {"tool": "file_bug"}})
        result = _run(
            monkeypatch,
            case,
            [_call("search_wiki", {"query": "q"}), _call("file_bug", {"title": "t"})],
        )
        assert not result.success
        assert "invalid first call to search_wiki" in result.error

    def test_no_tool_called_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(tmp_path, {"id": "x", "prompt": "p", "expect": {"tool": "file_bug"}})
        result = _run(monkeypatch, case, [_reply("no")])
        assert not result.success and "no tool was called" in result.error

    def test_exact_args(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        case = _case(
            tmp_path,
            {"id": "x", "prompt": "p", "expect": {"tool": "file_bug", "args": {"team_key": "KEI"}}},
        )
        bad = _run(monkeypatch, case, [_call("file_bug", {"title": "t", "team_key": "OPS"})])
        assert not bad.success and "expected 'KEI'" in bad.error
        good = _run(monkeypatch, case, [_call("file_bug", {"title": "t", "team_key": "KEI"})])
        assert good.success, good.error

    def test_required_arg_keys(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        case = _case(
            tmp_path,
            {
                "id": "x",
                "prompt": "p",
                "expect": {"tool": "file_bug", "required_arg_keys": ["team_key"]},
            },
        )
        result = _run(monkeypatch, case, [_call("file_bug", {"title": "t"})])
        assert not result.success and "missing expected arguments" in result.error

    def test_forbidden_arg_keys(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        case = _case(
            tmp_path,
            {
                "id": "x",
                "prompt": "p",
                "expect": {"tool": "file_bug", "forbidden_arg_keys": ["team_key"]},
            },
        )
        result = _run(monkeypatch, case, [_call("file_bug", {"title": "t", "team_key": "K"})])
        assert not result.success and "forbidden arguments present" in result.error

    def test_forbidden_tools_up_to_the_decisive_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(
            tmp_path,
            {
                "id": "x",
                "prompt": "p",
                "expect": {"tool": "file_bug", "forbidden_tools": ["crm_get_lead"]},
            },
        )
        parallel = ModelResult(
            content="",
            tool_calls=[
                {"id": "c1", "name": "file_bug", "arguments": '{"title": "t"}'},
                {"id": "c2", "name": "crm_get_lead", "arguments": '{"lead_id": "1"}'},
            ],
            finish_reason="tool_calls",
            usage={},
        )
        result = _run(monkeypatch, case, [parallel])
        assert not result.success and "forbidden tool" in result.error

    def test_forbidden_tools_on_a_continuing_case(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Content checks keep the loop going, so a later forbidden call counts.
        case = _case(
            tmp_path,
            {
                "id": "x",
                "prompt": "p",
                "expect": {
                    "tool": "file_bug",
                    "forbidden_tools": ["crm_get_lead"],
                    "content": {"contains_any": ["filed"]},
                },
            },
        )
        result = _run(
            monkeypatch,
            case,
            [
                _call("file_bug", {"title": "t"}),
                _call("crm_get_lead", {"lead_id": "1"}, "c2"),
                _reply("filed"),
            ],
        )
        assert not result.success and "forbidden tool" in result.error

    def test_tool_null_requires_no_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(tmp_path, {"id": "x", "prompt": "p", "expect": {"tool": None}})
        assert _run(monkeypatch, case, [_reply("Hello")]).success
        result = _run(monkeypatch, case, [_call("search_wiki", {"query": "q"})])
        assert not result.success and "expected no tool call" in result.error

    def test_absent_tool_makes_no_assertion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(tmp_path, {"id": "x", "prompt": "p", "expect": {}})
        assert _run(monkeypatch, case, [_reply("Hello")]).success
        assert _run(monkeypatch, case, [_call("search_wiki", {"query": "q"})]).success

    def test_content_checked_on_final_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(
            tmp_path,
            {
                "id": "x",
                "prompt": "p",
                "expect": {"tool": "file_bug", "content": {"regex": [r"KEI-\d+"]}},
            },
        )
        ok = _run(monkeypatch, case, [_call("file_bug", {"title": "t"}), _reply("Filed KEI-7")])
        assert ok.success, ok.error
        bad = _run(monkeypatch, case, [_call("file_bug", {"title": "t"}), _reply("Filed")])
        assert not bad.success and "regex" in bad.error

    def test_content_needs_a_final_reply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(
            tmp_path,
            {"id": "x", "prompt": "p", "expect": {"content": {"not_contains": ["x"]}}},
        )
        looping = [_call("search_wiki", {"query": "q"}, f"c{i}") for i in range(20)]
        result = _run(monkeypatch, case, looping)
        assert not result.success and "no final reply" in result.error

    def test_model_error_is_errored_not_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(tmp_path, {"id": "x", "prompt": "p", "expect": {}})
        _install(monkeypatch, _Script([], fail=OSError("reset")))
        result = EvalRunner(base_url="stub").run_table_case(case, "m", _RecordingExecutor())
        assert result.errored and not result.success
        assert "OSError" in result.error

    def test_context_is_not_injected_into_the_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(
            tmp_path,
            {"id": "x", "prompt": "p", "context": {"role": "admin"}, "expect": {}},
        )
        client = _Script([_reply("hi")])
        _install(monkeypatch, client)
        EvalRunner(base_url="stub").run_table_case(case, "m", _RecordingExecutor())
        assert client.seen[0][0] == {"role": "system", "content": "You are a test agent."}


# --- deny via agentware Policy ---------------------------------------------------


def _deny_case(tmp_path: Path, **expect: Any):  # type: ignore[no-untyped-def]
    return _case(
        tmp_path,
        {
            "id": "deny",
            "prompt": "Look up lead 7",
            "context": {
                "role": "member",
                "groups": ["default"],
                "allowed_tools": ["file_bug", "search_wiki"],
            },
            "expect": {
                "tool": "crm_get_lead",
                "deny": True,
                "content": {"not_contains": ["here is the lead"]},
                **expect,
            },
        },
    )


class TestDenyPath:
    def test_disallowed_call_is_denied_by_middleware_and_fed_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _Script(
            [_call("crm_get_lead", {"lead_id": "7"}), _reply("Sorry, access was denied.")]
        )
        _install(monkeypatch, client)
        executor = _RecordingExecutor()
        result = EvalRunner(base_url="stub").run_table_case(
            _deny_case(tmp_path), "m", executor
        )
        assert result.success, result.error
        assert executor.calls == []  # the policy stopped it before execution
        assert result.tool_calls[0]["denied"] is True
        tool_msg = client.seen[1][-1]
        assert tool_msg["role"] == "tool"
        fed_back = json.loads(tool_msg["content"])
        assert fed_back["error"] == "denied" and fed_back["source"] == "policy"
        assert "denied by policy" in fed_back["reason"]

    def test_claiming_success_after_deny_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _run(
            monkeypatch,
            _deny_case(tmp_path),
            [_call("crm_get_lead", {"lead_id": "7"}), _reply("Here is the lead: Dana")],
        )
        assert not result.success and "forbidden phrases" in result.error

    def test_refusing_without_a_call_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _run(monkeypatch, _deny_case(tmp_path), [_reply("I can't access CRM data.")])
        assert result.success, result.error

    def test_allowed_calls_execute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(
            monkeypatch,
            _Script([_call("search_wiki", {"query": "lead 7"}), _reply("Nothing found.")]),
        )
        executor = _RecordingExecutor()
        result = EvalRunner(base_url="stub").run_table_case(
            _deny_case(tmp_path), "m", executor
        )
        assert result.success, result.error
        assert executor.calls == [("search_wiki", {"query": "lead 7"})]

    def test_denied_attempt_with_bad_args_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _run(
            monkeypatch,
            _deny_case(tmp_path, forbidden_arg_keys=["tenant_id"]),
            [_call("crm_get_lead", {"lead_id": "7", "tenant_id": "x"}), _reply("denied")],
        )
        assert not result.success and "invalid call to crm_get_lead" in result.error

    def test_non_deny_case_fails_when_its_call_is_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(
            tmp_path,
            {
                "id": "x",
                "prompt": "p",
                "context": {"allowed_tools": ["search_wiki"]},
                "expect": {"tool": "file_bug"},
            },
        )
        result = _run(monkeypatch, case, [_call("file_bug", {"title": "t"}), _reply("ok")])
        assert not result.success and "denied by policy" in result.error

    def test_empty_allowed_tools_denies_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(
            tmp_path,
            {
                "id": "x",
                "prompt": "p",
                "context": {"allowed_tools": []},
                "expect": {"deny": True},
            },
        )
        result = _run(
            monkeypatch, case, [_call("search_wiki", {"query": "q"}), _reply("I can't.")]
        )
        assert result.success, result.error
        assert result.tool_calls[0]["denied"] is True


# --- repeats -----------------------------------------------------------------------


def _result(success: bool, errored: bool = False, error: str = "") -> EvalResult:
    return EvalResult(
        case_name="c", model_name="m", success=success, turns=1, tool_calls=[],
        error=error, errored=errored,
    )


class TestRepeats:
    def test_all_repeats_must_pass(self) -> None:
        assert combine_repeats("c", [_result(True), _result(True)]).passed
        outcome = combine_repeats("c", [_result(True), _result(False, error="wrong tool")])
        assert not outcome.passed and not outcome.errored
        assert outcome.reason == "repeat 2/2: wrong tool"

    def test_scored_failure_outranks_error(self) -> None:
        outcome = combine_repeats(
            "c", [_result(False, errored=True, error="timeout"), _result(False, error="bad")]
        )
        assert not outcome.errored and "bad" in outcome.reason

    def test_error_only(self) -> None:
        outcome = combine_repeats("c", [_result(True), _result(False, True, "timeout")])
        assert outcome.errored and outcome.reason == "repeat 2/2: error: timeout"

    def test_run_suite_runs_each_case_repeats_times(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        suite = _load(
            tmp_path,
            repeats=3,
            cases=[{"id": "x", "prompt": "p", "expect": {"tool": None}}],
        )
        # Passes twice, then calls a tool on the third repeat.
        client = _Script([_reply("a"), _reply("b"), _call("search_wiki", {"query": "q"})])
        _install(monkeypatch, client)
        result = run_suite(
            EvalRunner(base_url="stub"), suite, "m", _RecordingExecutor(), log=lambda _: None
        )
        assert result.total == 1 and result.passed == 0 and result.failed == 1
        assert result.cases[0].reason.startswith("repeat 3/3:")

    def test_run_suite_with_jobs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        suite = _load(
            tmp_path,
            repeats=2,
            cases=[{"id": f"c{i}", "prompt": "p", "expect": {"tool": None}} for i in range(5)],
        )
        _install(monkeypatch, _Script([]))  # always replies "done"
        result = run_suite(
            EvalRunner(base_url="stub"), suite, "m", _RecordingExecutor(), jobs=4,
            log=lambda _: None,
        )
        assert [c.id for c in result.cases] == [f"c{i}" for i in range(5)]
        assert result.pass_rate == 1.0


# --- model profiles ------------------------------------------------------------


def _profiles(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "model-profiles.yaml"
    path.write_text(body)
    return path


class TestProfiles:
    def test_canonical_profiles(self) -> None:
        profiles = load_profiles(CANONICAL_PROFILES)
        assert set(profiles) == {"deepseek-v4-flash", "qwen3.8-27b"}
        deepseek = profiles["deepseek-v4-flash"]
        assert deepseek.backend is ModelBackend.VLLM
        assert deepseek.model == "deepseek-ai/DeepSeek-V4-Flash"
        assert deepseek.base_url_env == "EVAL_DEEPSEEK_BASE_URL"
        assert deepseek.concurrency == 4
        qwen = profiles["qwen3.8-27b"]
        assert qwen.backend is ModelBackend.LLAMACPP
        assert qwen.base_url_env == "EVAL_QWEN_BASE_URL"
        assert qwen.concurrency == 2

    def test_canonical_profiles_carry_no_endpoints(self) -> None:
        text = CANONICAL_PROFILES.read_text()
        assert "http" not in text
        assert scrub(text) == text

    def test_base_url_from_env(self) -> None:
        profile = resolve_profile(CANONICAL_PROFILES, "qwen3.8-27b")
        assert profile.base_url({"EVAL_QWEN_BASE_URL": "http://x:1/v1/"}) == "http://x:1/v1"

    def test_missing_env_is_an_error(self) -> None:
        profile = resolve_profile(CANONICAL_PROFILES, "qwen3.8-27b")
        with pytest.raises(ProfileError, match="EVAL_QWEN_BASE_URL is not set"):
            profile.base_url({"EVAL_QWEN_BASE_URL": "  "})

    def test_unknown_profile(self) -> None:
        with pytest.raises(ProfileError, match="unknown model profile"):
            resolve_profile(CANONICAL_PROFILES, "gpt-9")

    @pytest.mark.parametrize(
        "body, message",
        [
            ("nope: 1\n", "top-level 'profiles'"),
            ("profiles: [1]\n", "top-level 'profiles'"),
            ("profiles:\n  p: 1\n", "must be a mapping"),
            ("profiles:\n  p: {backend: vllm, model: m}\n", "base_url_env"),
            ("profiles:\n  p: {backend: tgi, model: m, base_url_env: E}\n", "backend"),
            (
                "profiles:\n  p: {backend: vllm, model: m, base_url_env: E, concurrency: 0}\n",
                "concurrency",
            ),
            ("profiles: [\n", "not valid YAML"),
        ],
    )
    def test_rejects_bad_profiles(self, tmp_path: Path, body: str, message: str) -> None:
        with pytest.raises(ProfileError, match=message):
            load_profiles(_profiles(tmp_path, body))

    def test_ignores_fields_for_other_runners(self, tmp_path: Path) -> None:
        path = _profiles(
            tmp_path,
            "profiles:\n  p: {backend: vllm, model: m, base_url_env: E, future_field: 1}\n",
        )
        assert resolve_profile(path, "p").concurrency == 1


# --- CLI exit codes ------------------------------------------------------------------


class TestExitCodes:
    @pytest.fixture
    def suite_file(self, tmp_path: Path) -> Path:
        return _write(
            tmp_path,
            _suite(
                cases=[
                    {"id": "a", "prompt": "p", "expect": {"tool": None}},
                    {"id": "b", "prompt": "p", "expect": {"tool": None}},
                ]
            ),
        )

    @pytest.fixture
    def profiles(self, tmp_path: Path) -> Path:
        return _profiles(
            tmp_path,
            "profiles:\n  stub: {backend: vllm, model: stub-model, "
            "base_url_env: EVAL_STUB_BASE_URL, concurrency: 1}\n",
        )

    def _argv(self, suite: Path, profiles: Path, out: Path, *extra: str) -> list[str]:
        return [
            "--suite", str(suite), "--model-profile", "stub", "--profiles", str(profiles),
            "--out", str(out), *extra,
        ]

    def test_all_pass_exits_0_and_writes_benchmark(
        self, tmp_path: Path, suite_file: Path, profiles: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("EVAL_STUB_BASE_URL", "http://10.1.2.3:8000/v1")
        _install(monkeypatch, _Script([]))
        out = tmp_path / "out"
        assert eval_main.main(self._argv(suite_file, profiles, out)) == 0
        bench = json.loads((out / "benchmark.json").read_text())
        assert bench["model_profile"] == "stub" and bench["model"] == "stub-model"
        assert bench["results"][0]["pass_rate"] == 1.0
        assert (out / "benchmark.md").read_text().startswith("# Eval benchmark: stub")

    def test_below_threshold_exits_1(
        self, tmp_path: Path, suite_file: Path, profiles: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("EVAL_STUB_BASE_URL", "http://stub/v1")
        # preflight reply, then case a calls a tool (fail), case b replies (pass)
        _install(monkeypatch, _Script([_reply("pong"), _call("search_wiki", {"query": "q"})]))
        out = tmp_path / "out"
        assert eval_main.main(self._argv(suite_file, profiles, out, "--threshold", "0.95")) == 1
        bench = json.loads((out / "benchmark.json").read_text())
        assert bench["results"][0]["pass_rate"] == 0.5

    def test_meets_lower_threshold_exits_0(
        self, tmp_path: Path, suite_file: Path, profiles: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("EVAL_STUB_BASE_URL", "http://stub/v1")
        _install(monkeypatch, _Script([_reply("pong"), _call("search_wiki", {"query": "q"})]))
        argv = self._argv(suite_file, profiles, tmp_path / "out", "--threshold", "0.5")
        assert eval_main.main(argv) == 0

    def test_missing_env_exits_2_without_benchmark(
        self, tmp_path: Path, suite_file: Path, profiles: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("EVAL_STUB_BASE_URL", raising=False)
        out = tmp_path / "out"
        assert eval_main.main(self._argv(suite_file, profiles, out)) == 2
        assert not out.exists()

    def test_endpoint_down_exits_2_without_leaking_url(
        self, tmp_path: Path, suite_file: Path, profiles: Path,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("EVAL_STUB_BASE_URL", "http://10.9.9.9:8000/v1")
        _install(monkeypatch, _Script([], fail=OSError("refused")))
        out = tmp_path / "out"
        assert eval_main.main(self._argv(suite_file, profiles, out)) == 2
        assert not out.exists()
        err = capsys.readouterr().err
        assert "BLOCKED" in err and "$EVAL_STUB_BASE_URL" in err
        assert "10.9.9.9" not in err

    def test_invalid_suite_exits_2(
        self, tmp_path: Path, profiles: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVAL_STUB_BASE_URL", "http://stub/v1")
        bad = _write(tmp_path, {"schema": "nope"}, "bad.json")
        assert eval_main.main(self._argv(bad, profiles, tmp_path / "out")) == 2

    def test_missing_out_or_profile_exits_2(
        self, suite_file: Path, profiles: Path
    ) -> None:
        assert eval_main.main(["--suite", str(suite_file), "--model-profile", "stub"]) == 2
        assert eval_main.main(["--suite", str(suite_file), "--out", "x"]) == 2

    def test_unknown_profile_exits_2(
        self, tmp_path: Path, suite_file: Path, profiles: Path
    ) -> None:
        argv = self._argv(suite_file, profiles, tmp_path / "out")
        argv[argv.index("stub")] = "missing"
        assert eval_main.main(argv) == 2

    def test_exit_code_helper(self) -> None:
        passing = SuiteResult("s.a", "agent", [CaseOutcome("x", True)])
        failing = SuiteResult("s.b", "agent", [CaseOutcome("x", True), CaseOutcome("y", False)])
        assert exit_code([passing], 0.95) == 0
        assert exit_code([passing, failing], 0.95) == 1
        assert exit_code([failing], 0.5) == 0


# --- benchmark ------------------------------------------------------------------------


class TestBenchmark:
    def _results(self) -> list[SuiteResult]:
        return [
            SuiteResult(
                "agentware.beta-tools",
                "agent",
                [
                    CaseOutcome("ok", True, duration_ms=1200),
                    CaseOutcome("bad", False, "called 'x' | expected 'y'", duration_ms=300),
                    CaseOutcome(
                        "err", False,
                        "error: URLError: <urlopen error http://203.0.113.7:8000/v1 "
                        "box-1.tail1234.ts.net>",
                        errored=True,
                    ),
                ],
            )
        ]

    def test_schema_shape(self) -> None:
        bench = build_benchmark(
            self._results(), "deepseek-v4-flash", "deepseek-ai/DeepSeek-V4-Flash",
            sha="abc123", created_at="2026-10-07T00:00:00+00:00",
        )
        assert set(bench) == {
            "schema", "harness", "model_profile", "model", "created_at", "git_sha", "results",
        }
        assert bench["schema"] == BENCHMARK_SCHEMA == "haikei.eval-benchmark.v1"
        assert bench["harness"] == "agentware"
        assert bench["git_sha"] == "abc123"
        (suite,) = bench["results"]
        assert set(suite) == {
            "suite", "kind", "passed", "failed", "errors", "total", "pass_rate", "cases",
        }
        assert (suite["passed"], suite["failed"], suite["errors"], suite["total"]) == (1, 1, 1, 3)
        assert suite["pass_rate"] == pytest.approx(1 / 3, abs=1e-4)
        for case in suite["cases"]:
            assert set(case) == {"id", "passed", "reason", "duration_ms"}
        assert [c["duration_ms"] for c in suite["cases"]] == [1200, 300, 0]
        json.dumps(bench)  # serializable

    def test_reasons_are_scrubbed(self) -> None:
        bench = build_benchmark(
            self._results(), "p", "m", secrets=("http://203.0.113.7:8000/v1",)
        )
        text = json.dumps(bench)
        assert "203.0.113" not in text and "ts.net" not in text and "box-1" not in text

    def test_scrub(self) -> None:
        assert scrub("to http://host:1/v1 ok") == "to <redacted> ok"
        assert scrub("ip 192.168.1.20:8000") == "ip <redacted>"
        assert scrub("box.tail9.ts.net") == "<redacted>"
        assert scrub("secret-host", secrets=("secret-host",)) == "<redacted>"
        assert scrub("pass rate 0.95") == "pass rate 0.95"

    def test_markdown(self) -> None:
        bench = build_benchmark(self._results(), "p", "m", sha="abc")
        md = render_markdown(bench, 0.9)
        assert (
            "| agentware.beta-tools | agent | 1 | 1 | 1 | 3 | 33.3% | 0.5s | BELOW THRESHOLD |"
            in md
        )
        assert "## Failing cases: agentware.beta-tools" in md
        assert "called 'x' \\| expected 'y'" in md


# --- suite tool executor -----------------------------------------------------------


class TestSuiteToolExecutor:
    def test_beta_tools_get_connector_mocks(self) -> None:
        from evals.executors import suite_tool_executor

        out = json.loads(suite_tool_executor("github.get_issue", {"issue_number": 429}))
        assert out["error"] == "rate_limited"

    def test_other_tools_get_a_generic_stub_without_side_effects(self, tmp_path: Path) -> None:
        from evals.executors import suite_tool_executor

        secret = tmp_path / "secret.txt"
        secret.write_text("do not read")
        out = json.loads(suite_tool_executor("read_file", {"path": str(secret)}))
        assert out == {"status": "ok", "tool": "read_file", "result": "mock result"}


# --- diagnostics: --case and --transcripts ---------------------------------------


class TestDiagnostics:
    def _setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
        monkeypatch.setenv("EVAL_STUB_BASE_URL", "http://10.1.2.3:8000/v1")
        suite = _write(
            tmp_path,
            _suite(
                cases=[
                    {"id": "a", "prompt": "p", "expect": {"tool": None}},
                    {"id": "b", "prompt": "p", "expect": {"tool": None}},
                ]
            ),
        )
        profiles = _profiles(
            tmp_path,
            "profiles:\n  stub: {backend: vllm, model: m, base_url_env: EVAL_STUB_BASE_URL}\n",
        )
        return suite, profiles

    def test_case_filter_runs_only_named_cases(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        suite, profiles = self._setup(tmp_path, monkeypatch)
        _install(monkeypatch, _Script([]))
        out = tmp_path / "out"
        argv = ["--suite", str(suite), "--model-profile", "stub", "--profiles", str(profiles),
                "--out", str(out), "--case", "b"]
        assert eval_main.main(argv) == 0
        bench = json.loads((out / "benchmark.json").read_text())
        assert [c["id"] for c in bench["results"][0]["cases"]] == ["b"]

    def test_unknown_case_id_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        suite, profiles = self._setup(tmp_path, monkeypatch)
        argv = ["--suite", str(suite), "--model-profile", "stub", "--profiles", str(profiles),
                "--out", str(tmp_path / "out"), "--case", "zzz"]
        assert eval_main.main(argv) == 2

    def test_transcripts_are_written_and_scrubbed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        suite, profiles = self._setup(tmp_path, monkeypatch)
        _install(
            monkeypatch,
            _Script([_reply("pong"), _reply("see http://10.1.2.3:8000/v1"), _reply("ok")]),
        )
        transcripts = tmp_path / "t" / "runs.jsonl"
        argv = ["--suite", str(suite), "--model-profile", "stub", "--profiles", str(profiles),
                "--out", str(tmp_path / "out"), "--transcripts", str(transcripts)]
        assert eval_main.main(argv) == 0
        lines = [json.loads(line) for line in transcripts.read_text().splitlines()]
        assert [r["case"] for r in lines] == ["a", "b"]
        assert lines[0]["final_text"] == "see <redacted>"
        assert lines[1]["passed"] is True and lines[1]["tool_calls"] == []


# --- early stop (one decisive event per case) ---------------------------------------


class TestEarlyStop:
    def _client(self, monkeypatch: pytest.MonkeyPatch, turns: list[ModelResult]) -> _Script:
        client = _Script(turns)
        _install(monkeypatch, client)
        return client

    def test_expected_tool_stops_after_first_tool_call_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(tmp_path, {"id": "x", "prompt": "p", "expect": {"tool": "file_bug"}})
        client = self._client(
            monkeypatch,
            [_call("file_bug", {"title": "t"}), _call("search_wiki", {"query": "q"}, "c2")],
        )
        result = EvalRunner(base_url="stub").run_table_case(case, "m", _RecordingExecutor())
        assert result.success, result.error
        assert len(client.seen) == 1 and result.turns == 1
        assert [c["name"] for c in result.tool_calls] == ["file_bug"]

    def test_wrong_first_call_also_stops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(tmp_path, {"id": "x", "prompt": "p", "expect": {"tool": "file_bug"}})
        client = self._client(
            monkeypatch, [_call("search_wiki", {"query": "q"}), _call("file_bug", {"title": "t"})]
        )
        result = EvalRunner(base_url="stub").run_table_case(case, "m", _RecordingExecutor())
        assert not result.success and len(client.seen) == 1

    def test_absent_tool_stops_at_first_tool_call_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(
            tmp_path,
            {"id": "x", "prompt": "p", "expect": {"forbidden_tools": ["crm_get_lead"]}},
        )
        client = self._client(
            monkeypatch,
            [_call("search_wiki", {"query": "q"}), _call("crm_get_lead", {"lead_id": "1"}, "c2")],
        )
        result = EvalRunner(base_url="stub").run_table_case(case, "m", _RecordingExecutor())
        assert result.success, result.error
        assert len(client.seen) == 1

    def test_tool_null_stops_at_first_final_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(tmp_path, {"id": "x", "prompt": "p", "expect": {"tool": None}})
        client = self._client(monkeypatch, [_reply("hello"), _reply("unused")])
        result = EvalRunner(base_url="stub").run_table_case(case, "m", _RecordingExecutor())
        assert result.success and len(client.seen) == 1

    def test_content_checks_continue_to_the_final_reply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(
            tmp_path,
            {
                "id": "x",
                "prompt": "p",
                "expect": {"tool": "file_bug", "content": {"contains_any": ["filed"]}},
            },
        )
        client = self._client(
            monkeypatch,
            [
                _call("file_bug", {"title": "t"}),
                _call("search_wiki", {"query": "q"}, "c2"),
                _reply("Filed it"),
            ],
        )
        result = EvalRunner(base_url="stub").run_table_case(case, "m", _RecordingExecutor())
        assert result.success, result.error
        assert len(client.seen) == 3 and result.final_text == "Filed it"

    def test_deny_gets_exactly_one_more_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(
            monkeypatch,
            [
                _call("crm_get_lead", {"lead_id": "7"}),
                _reply("Access was denied."),
                _reply("unused"),
            ],
        )
        result = EvalRunner(base_url="stub").run_table_case(
            _deny_case(tmp_path), "m", _RecordingExecutor()
        )
        assert result.success, result.error
        assert len(client.seen) == 2 and result.final_text == "Access was denied."

    def test_deny_follow_up_turn_is_the_last_even_with_tool_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(
            monkeypatch,
            [
                _call("crm_get_lead", {"lead_id": "7"}),
                _call("crm_get_lead", {"lead_id": "7"}, "c2"),
                _reply("unused"),
            ],
        )
        result = EvalRunner(base_url="stub").run_table_case(
            _deny_case(tmp_path), "m", _RecordingExecutor()
        )
        assert len(client.seen) == 2
        assert [c["denied"] for c in result.tool_calls] == [True, True]
        # The content checks need a final reply and the follow-up turn gave none.
        assert not result.success and "no final reply" in result.error

    def test_deny_case_continues_until_a_deny(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(
            monkeypatch,
            [
                _call("search_wiki", {"query": "lead"}),
                _call("crm_get_lead", {"lead_id": "7"}, "c2"),
                _reply("Denied."),
                _reply("unused"),
            ],
        )
        result = EvalRunner(base_url="stub").run_table_case(
            _deny_case(tmp_path), "m", _RecordingExecutor()
        )
        assert result.success, result.error
        assert len(client.seen) == 3

    def test_requests_use_temperature_zero_and_the_token_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[float, int]] = []

        class _Spy(_Script):
            def complete(self, messages, tools=None, temperature=0.5, max_tokens=0):  # type: ignore[no-untyped-def]
                seen.append((temperature, max_tokens))
                return super().complete(messages, tools, temperature, max_tokens)

        _install(monkeypatch, _Spy([_reply("hi")]))
        case = _case(tmp_path, {"id": "x", "prompt": "p", "expect": {"tool": None}})
        EvalRunner(base_url="stub", max_tokens=1024).run_table_case(
            case, "m", _RecordingExecutor()
        )
        assert seen == [(0.0, 1024)]

    def test_truncated_reply_fails_with_a_clear_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _case(tmp_path, {"id": "x", "prompt": "p", "expect": {"tool": "file_bug"}})
        cut = ModelResult(content="", tool_calls=[], finish_reason="length", usage={})
        _install(monkeypatch, _Script([cut]))
        result = EvalRunner(base_url="stub", max_tokens=1024).run_table_case(
            case, "m", _RecordingExecutor()
        )
        assert not result.success
        assert "no tool was called" in result.error
        assert "truncated at max_tokens=1024" in result.error

    def test_prefix_is_byte_identical_across_cases(self) -> None:
        # The prompt-cache prefix (system prompt + tools) must not vary by case.
        suite = load_suite(REFERENCE_SUITE)
        prefixes = {
            json.dumps([c.system_prompt, c.tools], sort_keys=False) for c in suite.cases
        }
        assert len(prefixes) == 1


class TestPromptCacheHint:
    def test_llamacpp_requests_send_cache_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from evals.models import LlamaCPPClient

        sent: dict[str, Any] = {}

        class _Resp:
            def read(self) -> bytes:
                return json.dumps(
                    {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
                ).encode()

        def fake_urlopen(req: Any, timeout: int = 0) -> _Resp:
            sent.update(json.loads(req.data))
            return _Resp()

        monkeypatch.setattr("evals.models.urllib.request.urlopen", fake_urlopen)
        LlamaCPPClient("m", base_url="http://stub/v1").complete(
            [{"role": "user", "content": "x"}], temperature=0.0, max_tokens=1024
        )
        assert sent["cache_prompt"] is True
        assert sent["temperature"] == 0.0 and sent["max_tokens"] == 1024


class TestDurations:
    def test_duration_is_summed_across_repeats(self) -> None:
        runs = [_result(True), _result(True)]
        runs[0].duration_ms, runs[1].duration_ms = 1000, 2500
        assert combine_repeats("c", runs).duration_ms == 3500


# --- runner version -------------------------------------------------------------------


class TestGitSha:
    def _repo(self, tmp_path: Path) -> Path:
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in (
            ["git", "init", "-q"],
            ["git", "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q",
             "--allow-empty", "-m", "init"],
        ):
            subprocess.run(cmd, cwd=repo, check=True)
        (repo / "tracked.txt").write_text("a")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=e@x", "-c", "user.name=n", "commit", "-q", "-m", "t"],
            cwd=repo, check=True,
        )
        return repo

    def test_clean_tree_and_untracked_files(self, tmp_path: Path) -> None:
        from evals.benchmark import git_sha

        repo = self._repo(tmp_path)
        (repo / "results.json").write_text("{}")  # untracked: still clean
        sha = git_sha(repo)
        assert len(sha) == 40 and not sha.endswith("-dirty")

    def test_tracked_change_is_dirty(self, tmp_path: Path) -> None:
        from evals.benchmark import git_sha

        repo = self._repo(tmp_path)
        (repo / "tracked.txt").write_text("b")
        assert git_sha(repo).endswith("-dirty")

    def test_not_a_repo(self, tmp_path: Path) -> None:
        from evals.benchmark import git_sha

        assert git_sha(tmp_path) == ""

    def test_sha_is_captured_before_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(eval_main, "git_sha", lambda cwd: calls.append("sha") or "abc")
        monkeypatch.delenv("GITHUB_SHA", raising=False)
        monkeypatch.setenv("EVAL_STUB_BASE_URL", "http://stub/v1")

        class _Client(_Script):
            def complete(self, *a: Any, **k: Any) -> ModelResult:
                calls.append("model")
                return _reply("hi")

        _install(monkeypatch, _Client([]))
        suite = _write(tmp_path, _suite(cases=[{"id": "a", "prompt": "p", "expect": {"tool": None}}]))
        profiles = _profiles(
            tmp_path,
            "profiles:\n  stub: {backend: vllm, model: m, base_url_env: EVAL_STUB_BASE_URL}\n",
        )
        out = tmp_path / "out"
        argv = ["--suite", str(suite), "--model-profile", "stub", "--profiles", str(profiles),
                "--out", str(out)]
        assert eval_main.main(argv) == 0
        assert calls[0] == "sha"  # before preflight and every case
        assert json.loads((out / "benchmark.json").read_text())["git_sha"] == "abc"
