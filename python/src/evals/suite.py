"""Loader for ``agentware.eval-suite.v1`` JSON table-test suites (EV-C1 §1).

A suite is data, not code: a system prompt, OpenAI function tools, and cases
of prompt -> expected outcome. Validation is strict -- unknown keys at any
level, wrong types, duplicate case ids, and expectations that cannot be
satisfied are rejected before any model is called. Loaded cases are plain
``EvalCase`` objects, so JSON suites and the Python case modules share one
runner and one validator.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from evals.expectations import ContentChecks
from evals.runner import EvalCase

SUITE_SCHEMA = "agentware.eval-suite.v1"
CASE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SUITE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)+$")


class SuiteValidationError(ValueError):
    """A suite file is missing, unreadable, or violates the v1 schema."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Content(_Strict):
    contains_all: list[str] = Field(default_factory=list)
    contains_any: list[str] = Field(default_factory=list)
    regex: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)

    @field_validator("regex")
    @classmethod
    def _regex_compiles(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return patterns


class _Expect(_Strict):
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    required_arg_keys: list[str] = Field(default_factory=list)
    forbidden_arg_keys: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    deny: bool = False
    content: _Content = Field(default_factory=_Content)

    @model_validator(mode="after")
    def _args_need_a_tool(self) -> "_Expect":
        has_arg_checks = self.args or self.required_arg_keys or self.forbidden_arg_keys
        if has_arg_checks and self.tool is None:
            raise ValueError("args/required_arg_keys/forbidden_arg_keys need expect.tool")
        if self.tool is not None and self.tool in self.forbidden_tools:
            raise ValueError(f"expect.tool {self.tool!r} is also in forbidden_tools")
        return self


class _Context(BaseModel):
    # Open by contract: harness adapters may carry extra caller context.
    model_config = ConfigDict(extra="allow", strict=True)

    role: str = ""
    groups: list[str] = Field(default_factory=list)
    allowed_tools: list[str] | None = None


class _Case(_Strict):
    id: str
    prompt: str = Field(min_length=1)
    context: _Context = Field(default_factory=_Context)
    expect: _Expect

    @field_validator("id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        if not CASE_ID_PATTERN.match(value):
            raise ValueError(f"case id {value!r} must match {CASE_ID_PATTERN.pattern}")
        return value


class _Function(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    name: str = Field(min_length=1)
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class _Tool(_Strict):
    type: Literal["function"]
    function: _Function


class _Suite(_Strict):
    schema_: Literal["agentware.eval-suite.v1"] = Field(alias="schema")
    suite: str
    kind: Literal["agent", "skill"]
    system_prompt: str | None = None
    system_prompt_file: str | None = None
    tools: list[_Tool]
    repeats: int = Field(default=1, ge=1)
    cases: list[_Case] = Field(min_length=1)

    @field_validator("suite")
    @classmethod
    def _suite_shape(cls, value: str) -> str:
        if not SUITE_NAME_PATTERN.match(value):
            raise ValueError(f"suite {value!r} must be dotted <repo>.<agent-or-workflow>")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "_Suite":
        if (self.system_prompt is None) == (self.system_prompt_file is None):
            raise ValueError("exactly one of system_prompt or system_prompt_file is required")

        tool_names = [t.function.name for t in self.tools]
        if len(set(tool_names)) != len(tool_names):
            raise ValueError("tool names must be unique")
        declared = set(tool_names)

        ids = [c.id for c in self.cases]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate case ids: {dupes}")

        for case in self.cases:
            expect = case.expect
            if expect.tool is not None and expect.tool not in declared:
                raise ValueError(f"case {case.id}: expect.tool {expect.tool!r} is not a suite tool")
            allowed = case.context.allowed_tools
            if allowed is not None:
                unknown = sorted(set(allowed) - declared)
                if unknown:
                    raise ValueError(f"case {case.id}: allowed_tools not in suite tools: {unknown}")
            if expect.deny:
                if allowed is None:
                    raise ValueError(f"case {case.id}: expect.deny needs context.allowed_tools")
                if expect.tool is not None and expect.tool in allowed:
                    raise ValueError(
                        f"case {case.id}: expect.deny but expect.tool {expect.tool!r} is allowed"
                    )
        return self


@dataclass(frozen=True)
class Suite:
    """A validated suite, with cases converted to ``EvalCase``."""

    name: str
    kind: str
    path: Path
    repeats: int
    cases: list[EvalCase]


def _missing_tool_key(raw: Any) -> set[str]:
    """Case ids whose ``expect`` omits ``tool`` (no first-tool assertion).

    pydantic cannot tell an absent key from an explicit ``null``; the
    contract gives them different meanings, so read it off the raw JSON.
    """
    absent: set[str] = set()
    if isinstance(raw, dict):
        for case in raw.get("cases") or []:
            if isinstance(case, dict) and isinstance(case.get("expect"), dict):
                if "tool" not in case["expect"]:
                    absent.add(str(case.get("id")))
    return absent


def load_suite(path: str | Path) -> Suite:
    """Load and strictly validate one suite file.

    ``system_prompt_file`` must be a relative path; it resolves against the
    suite file's directory.
    """
    suite_path = Path(path)
    try:
        raw = json.loads(suite_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SuiteValidationError(f"{suite_path}: cannot read suite: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SuiteValidationError(f"{suite_path}: not valid JSON: {exc}") from exc

    try:
        parsed = _Suite.model_validate(raw)
    except ValidationError as exc:
        raise SuiteValidationError(f"{suite_path}: invalid {SUITE_SCHEMA} suite:\n{exc}") from exc

    if parsed.system_prompt_file is not None:
        prompt_path = Path(parsed.system_prompt_file)
        if prompt_path.is_absolute():
            raise SuiteValidationError(f"{suite_path}: system_prompt_file must be relative")
        try:
            system_prompt = (suite_path.parent / prompt_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise SuiteValidationError(
                f"{suite_path}: cannot read system_prompt_file {prompt_path}: {exc}"
            ) from exc
    else:
        system_prompt = parsed.system_prompt or ""

    tools = [t.model_dump() for t in parsed.tools]
    no_tool_key = _missing_tool_key(raw)
    cases = []
    for case in parsed.cases:
        expect = case.expect
        context = case.context.model_dump(exclude={"allowed_tools"})
        cases.append(
            EvalCase(
                name=case.id,
                description=case.id,
                system_prompt=system_prompt,
                user_message=case.prompt,
                tools=tools,
                expected_tool=expect.tool,
                expect_no_tool_call=expect.tool is None and case.id not in no_tool_key,
                expected_args=dict(expect.args),
                required_arg_keys=list(expect.required_arg_keys),
                forbidden_arg_keys=list(expect.forbidden_arg_keys),
                forbidden_tools=list(expect.forbidden_tools),
                expect_deny=expect.deny,
                content=ContentChecks(
                    contains_all=tuple(expect.content.contains_all),
                    contains_any=tuple(expect.content.contains_any),
                    regex=tuple(expect.content.regex),
                    not_contains=tuple(expect.content.not_contains),
                ),
                context=context,
                allowed_tools=(
                    None
                    if case.context.allowed_tools is None
                    else list(case.context.allowed_tools)
                ),
            )
        )
    return Suite(
        name=parsed.suite,
        kind=parsed.kind,
        path=suite_path,
        repeats=parsed.repeats,
        cases=cases,
    )


def load_suites(path: str | Path) -> list[Suite]:
    """Load one suite file, or every ``*.json`` suite in a directory (sorted).

    Suite names must be unique across the set.
    """
    root = Path(path)
    if root.is_dir():
        files = sorted(root.glob("*.json"))
        if not files:
            raise SuiteValidationError(f"{root}: no *.json suites found")
    elif root.is_file():
        files = [root]
    else:
        raise SuiteValidationError(f"{root}: no such suite file or directory")

    suites = [load_suite(f) for f in files]
    names = [s.name for s in suites]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise SuiteValidationError(f"duplicate suite names: {dupes}")
    return suites
