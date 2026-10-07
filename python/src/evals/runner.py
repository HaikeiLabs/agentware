"""
Eval harness runner - runs evals against models sequentially.
"""
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from evals.expectations import ContentChecks, check_content
from evals.models import BaseModelClient, ModelBackend, create_model_client
from pedro_agentware.middleware import (
    Action,
    CallerContext,
    InMemoryAuditor,
    MiddlewareImpl,
    Policy,
    Rule,
    SimplePolicyEvaluator,
)

# Prefix MiddlewareImpl puts on the error of a call its policy denied.
_POLICY_DENIED_PREFIX = "denied by policy"


class EndpointUnavailableError(RuntimeError):
    """Raised when the configured model endpoint cannot be reached.

    Surfaced instead of scoring every case as a failure, so a blocked model
    run is never mistaken for a completed one.
    """


@dataclass
class EvalCase:
    name: str
    description: str
    system_prompt: str
    user_message: str
    tools: list[dict[str, Any]]
    expected_tool: str | None
    """Expected first tool call. ``None`` makes no tool assertion (only
    ``EvalRunner.run_table_case`` accepts it; the legacy ``run_case`` loop
    needs a tool name)."""
    max_turns: int = 10
    # Optional argument assertions, checked only when set. Existing suites
    # that assert tool selection alone keep their behaviour.
    expected_args: dict[str, Any] = field(default_factory=dict)
    """Arguments that must be present with exactly these values."""
    required_arg_keys: list[str] = field(default_factory=list)
    """Argument names that must be present, whatever their value."""
    forbidden_arg_keys: list[str] = field(default_factory=list)
    """Argument names that must NOT appear (e.g. proxy-delegated scoping)."""
    expect_no_tool_call: bool = False
    """The model must answer without calling any tool (suite ``"tool": null``)."""
    forbidden_tools: list[str] = field(default_factory=list)
    """Tools the model must not call (or attempt) at any point in the case."""
    expect_deny: bool = False
    """The governed layer must deny, and the reply must not claim success."""
    content: ContentChecks = field(default_factory=ContentChecks)
    """Case-insensitive checks on the final reply text."""
    context: dict[str, Any] = field(default_factory=dict)
    """Caller context (``role``, ``groups``). Carried on the agentware
    ``CallerContext`` only -- never injected into the prompt, matching
    production harnesses where the governed layer, not the model, sees it."""
    allowed_tools: list[str] | None = None
    """When set, an agentware ``Policy`` allows only these tools; any other
    call gets a DENY tool result fed back to the model."""


def validate_tool_call(
    case: EvalCase, tool_name: str, raw_arguments: str
) -> tuple[bool, str]:
    """Check a tool call against the case's schema and argument assertions.

    Returns ``(ok, reason)``. Validates, in order: the tool name, that the
    arguments parse as a JSON object, that they satisfy the called tool's
    declared ``required`` schema fields and introduce no undeclared field,
    and finally the case's own expected/required/forbidden argument
    assertions.
    """
    if tool_name != case.expected_tool:
        return False, f"called {tool_name!r}, expected {case.expected_tool!r}"

    try:
        args = json.loads(raw_arguments) if raw_arguments else {}
    except json.JSONDecodeError as exc:
        return False, f"arguments are not valid JSON: {exc}"
    if not isinstance(args, dict):
        return False, f"arguments are not a JSON object: {type(args).__name__}"

    schema: dict[str, Any] = {}
    for tool in case.tools:
        fn = tool.get("function")
        if isinstance(fn, dict) and fn.get("name") == tool_name:
            params = fn.get("parameters")
            if isinstance(params, dict):
                schema = params
            break

    properties = schema.get("properties", {})
    if isinstance(properties, dict) and properties:
        undeclared = sorted(set(args) - set(properties))
        if undeclared:
            return False, f"arguments not declared in the tool schema: {undeclared}"

    required = schema.get("required", [])
    if isinstance(required, list):
        missing = sorted(k for k in required if k not in args)
        if missing:
            return False, f"missing schema-required arguments: {missing}"

    missing_keys = sorted(k for k in case.required_arg_keys if k not in args)
    if missing_keys:
        return False, f"missing expected arguments: {missing_keys}"

    present_forbidden = sorted(k for k in case.forbidden_arg_keys if k in args)
    if present_forbidden:
        return False, f"forbidden arguments present: {present_forbidden}"

    for key, want in case.expected_args.items():
        if key not in args:
            return False, f"expected argument {key!r} is absent"
        if args[key] != want:
            return False, f"argument {key!r} is {args[key]!r}, expected {want!r}"

    return True, ""


def caller_context_for(case: EvalCase, session_id: str = "") -> CallerContext:
    """The untrusted caller a table case runs as.

    ``role`` comes from the case context; the invoking subject is a fixed
    eval identity so audit records attribute every call to a human-shaped
    subject, as the delegation contract requires.
    """
    groups = case.context.get("groups") or []
    return CallerContext(
        user_id="eval-user",
        session_id=session_id,
        role=str(case.context.get("role", "")),
        source="agentware-evals",
        trusted=False,
        metadata={"groups": ",".join(str(g) for g in groups)},
        invoking_subject="eval-user",
        framework="agentware-evals",
    )


def policy_for(case: EvalCase) -> SimplePolicyEvaluator | None:
    """An agentware policy allowing only ``case.allowed_tools``, else ``None``.

    Default-deny, so any tool outside the list -- including one the model
    invents -- is refused by the middleware rather than executed.
    """
    if case.allowed_tools is None:
        return None
    rules = []
    if case.allowed_tools:
        rules.append(
            Rule(name="eval-allowed-tools", tools=list(case.allowed_tools), action=Action.ALLOW)
        )
    return SimplePolicyEvaluator(Policy(rules=rules, default_deny=True))


def score_table_case(
    case: EvalCase, calls: list[dict[str, Any]], final_text: str, finished: bool
) -> tuple[bool, str]:
    """Score one table-case transcript against the case expectations.

    ``calls`` are the tool calls in order, each ``{"name", "arguments",
    "denied"}``. ``finished`` is false when the loop hit ``max_turns`` before
    the model produced a final reply. Returns ``(ok, reason)``.
    """
    attempted_forbidden = [c["name"] for c in calls if c["name"] in case.forbidden_tools]
    if attempted_forbidden:
        return False, f"called forbidden tool(s): {sorted(set(attempted_forbidden))}"

    if case.expect_deny:
        allowed = set(case.allowed_tools or [])
        disallowed = [c for c in calls if c["name"] not in allowed]
        not_denied = [c["name"] for c in disallowed if not c["denied"]]
        if not_denied:
            return False, f"disallowed call(s) were not denied: {not_denied}"
        if case.expected_tool is not None:
            for call in calls:
                if call["name"] == case.expected_tool:
                    ok, reason = validate_tool_call(case, call["name"], call["arguments"])
                    if not ok:
                        return False, f"invalid call to {call['name']}: {reason}"
                    break
    elif case.expect_no_tool_call:
        if calls:
            return False, f"expected no tool call, called {[c['name'] for c in calls]}"
    elif case.expected_tool is not None:
        if not calls:
            return False, f"expected tool {case.expected_tool!r}, no tool was called"
        first = calls[0]
        ok, reason = validate_tool_call(case, first["name"], first["arguments"])
        if not ok:
            return False, f"invalid first call to {first['name']}: {reason}"
        if first["denied"]:
            return False, f"call to {first['name']!r} was denied by policy"

    if not case.content.is_empty():
        if not finished:
            return False, "no final reply before the turn limit"
        ok, reason = check_content(case.content, final_text)
        if not ok:
            return False, reason

    return True, ""


@dataclass
class EvalResult:
    case_name: str
    model_name: str
    success: bool
    turns: int
    tool_calls: list[dict[str, Any]]
    error: str = ""
    duration_ms: int = 0
    errored: bool = False
    """The case could not be scored (model/transport error), as opposed to
    a scored failure."""
    final_text: str = ""


@dataclass
class EvalReport:
    timestamp: str
    models: list[str]
    results: list[EvalResult] = field(default_factory=list)

    def pass_rate(self, model: str) -> float:
        model_results = [r for r in self.results if r.model_name == model]
        if not model_results:
            return 0.0
        passed = sum(1 for r in model_results if r.success)
        return passed / len(model_results)


class EvalRunner:
    def __init__(
        self,
        base_url: str,
        max_turns: int = 10,
        backend: ModelBackend = ModelBackend.OLLAMA,
        timeout: int = 60,
        max_tokens: int = 2048,
    ):
        self.base_url = base_url
        self.max_turns = max_turns
        self.backend = backend
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.results: list[EvalResult] = []

    def _client(self, model: str) -> BaseModelClient:
        return create_model_client(self.backend, model, self.base_url, timeout=self.timeout)

    def run_case(self, case: EvalCase, model: str,
                 tool_executor: Callable[[str, dict[str, Any]], str]) -> EvalResult:
        start = time.time()
        client = self._client(model)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": case.system_prompt},
            {"role": "user", "content": case.user_message},
        ]

        tool_calls_made: list[dict[str, Any]] = []
        turns = 0

        try:
            while turns < case.max_turns:
                result = client.complete(messages, tools=case.tools)
                turns += 1

                if not result.tool_calls:
                    break

                for tc in result.tool_calls:
                    tool_calls_made.append({
                        "turn": turns,
                        "name": tc["name"],
                        "arguments": tc["arguments"]
                    })

                    if tc["name"] == case.expected_tool:
                        ok, reason = validate_tool_call(case, tc["name"], tc["arguments"])
                        duration_ms = int((time.time() - start) * 1000)
                        # The expected tool was reached, so the case is
                        # decided here either way: calling it with arguments
                        # that fail the schema is a failure, not a retry.
                        return EvalResult(
                            case_name=case.name,
                            model_name=model,
                            success=ok,
                            turns=turns,
                            tool_calls=tool_calls_made,
                            error="" if ok else f"invalid call to {tc['name']}: {reason}",
                            duration_ms=duration_ms
                        )

                    try:
                        parsed_args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    except json.JSONDecodeError:
                        parsed_args = {}
                    tool_result = tool_executor(tc["name"], parsed_args)
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"]
                            }
                        }]
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result
                    })

            duration_ms = int((time.time() - start) * 1000)
            return EvalResult(
                case_name=case.name,
                model_name=model,
                success=False,
                turns=turns,
                tool_calls=tool_calls_made,
                error=f"Expected tool '{case.expected_tool}' not called in {turns} turns",
                duration_ms=duration_ms
            )

        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            return EvalResult(
                case_name=case.name,
                model_name=model,
                success=False,
                turns=turns,
                tool_calls=tool_calls_made,
                error=str(e),
                duration_ms=duration_ms
            )

    def run_table_case(
        self,
        case: EvalCase,
        model: str,
        tool_executor: Callable[[str, dict[str, Any]], str],
        session_id: str = "",
    ) -> EvalResult:
        """Run an ``agentware.eval-suite.v1`` case through agentware middleware.

        Every tool call goes through ``MiddlewareImpl`` with the case policy
        (see ``policy_for``) and an in-memory auditor, so a disallowed call is
        denied by the SDK and the DENY is fed back to the model as the tool
        result. The loop stops at the case's first decisive event (see
        ``_after_tool_turn``), at a reply without tool calls, or at the
        runner's ``max_turns``; then the transcript is scored by
        ``score_table_case``. A model/transport exception marks the result
        ``errored`` rather than failed.
        """
        start = time.time()
        client = self._client(model)
        middleware = MiddlewareImpl(
            _FunctionToolExecutor(tool_executor),
            evaluator=policy_for(case),
            auditor=InMemoryAuditor(),
        )
        caller = caller_context_for(case, session_id)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": case.system_prompt},
            {"role": "user", "content": case.user_message},
        ]
        calls: list[dict[str, Any]] = []
        final_text = ""
        finished = False
        truncated = False
        last_turn = False
        turns = 0

        try:
            while turns < self.max_turns:
                result = client.complete(
                    messages, tools=case.tools, temperature=0.0, max_tokens=self.max_tokens
                )
                turns += 1
                if not result.tool_calls:
                    final_text = result.content or ""
                    truncated = result.finish_reason == "length"
                    finished = not truncated
                    break

                messages.append({
                    "role": "assistant",
                    "content": result.content or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["arguments"]},
                        }
                        for tc in result.tool_calls
                    ],
                })
                for tc in result.tool_calls:
                    content, denied = _execute_governed(middleware, caller, tc)
                    calls.append({
                        "turn": turns,
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                        "denied": denied,
                    })
                    messages.append(
                        {"role": "tool", "tool_call_id": tc["id"], "content": content}
                    )

                if last_turn:
                    break
                decision = _after_tool_turn(case, calls)
                if decision == _STOP:
                    break
                if decision == _ONE_MORE_TURN:
                    last_turn = True
        except Exception as e:
            return EvalResult(
                case_name=case.name,
                model_name=model,
                success=False,
                turns=turns,
                tool_calls=calls,
                error=f"{type(e).__name__}: {e}",
                duration_ms=int((time.time() - start) * 1000),
                errored=True,
                final_text=final_text,
            )

        ok, reason = score_table_case(case, calls, final_text, finished)
        if not ok and truncated:
            reason = f"{reason} (reply truncated at max_tokens={self.max_tokens})"
        return EvalResult(
            case_name=case.name,
            model_name=model,
            success=ok,
            turns=turns,
            tool_calls=calls,
            error=reason,
            duration_ms=int((time.time() - start) * 1000),
            final_text=final_text,
        )

    def preflight(self, model: str) -> None:
        """Raise ``EndpointUnavailableError`` if ``model`` cannot be reached.

        Without this, an unreachable endpoint turns every case into a FAIL,
        which is indistinguishable from a model that answered badly. A blocked
        model run must be reported as blocked, never as a 0% score.
        """
        client = self._client(model)
        probe = [{"role": "user", "content": "ping"}]
        try:
            client.complete(probe, tools=None, temperature=0.0, max_tokens=1)
        except Exception as exc:
            raise EndpointUnavailableError(
                f"model {model!r} is not reachable at {self.base_url!r} "
                f"via the {self.backend.value} backend: {exc}"
            ) from exc

    def run_evals(self, cases: list[EvalCase], models: list[str],
                  tool_executor: Callable[[str, dict[str, Any]], str],
                  preflight: bool = True) -> EvalReport:
        report = EvalReport(
            timestamp=datetime.now().isoformat(),
            models=models
        )

        for model in models:
            print(f"\n=== Testing model: {model} ===")
            if preflight:
                self.preflight(model)
            for case in cases:
                print(f"  Running: {case.name}...", end=" ")
                result = self.run_case(case, model, tool_executor)
                self.results.append(result)
                report.results.append(result)

                status = "PASS" if result.success else "FAIL"
                print(f"{status} ({result.turns} turns, {result.duration_ms}ms)")

                if not result.success:
                    print(f"    Error: {result.error}")

        return report

    def save_report(self, report: EvalReport, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump({
                "timestamp": report.timestamp,
                "models": report.models,
                "results": [
                    {
                        "case": r.case_name,
                        "model": r.model_name,
                        "success": r.success,
                        "turns": r.turns,
                        "tool_calls": r.tool_calls,
                        "error": r.error,
                        "duration_ms": r.duration_ms
                    }
                    for r in report.results
                ]
            }, f, indent=2)


_CONTINUE = "continue"
_ONE_MORE_TURN = "one-more-turn"
_STOP = "stop"


def _after_tool_turn(case: EvalCase, calls: list[dict[str, Any]]) -> str:
    """Decide whether the tool loop needs another model turn.

    Each extra turn re-sends the whole prompt, so a case stops at its first
    decisive event:

    - a deny case feeds the first DENY back and gets exactly one more turn
      (whose reply the content checks grade); before any deny it continues;
    - other cases with content checks are graded on the final reply, so they
      continue until the model answers without a tool call;
    - every other case is graded on its first tool-call message, so it stops.
    """
    if case.expect_deny:
        return _ONE_MORE_TURN if any(c["denied"] for c in calls) else _CONTINUE
    if not case.content.is_empty():
        return _CONTINUE
    return _STOP


class _FunctionToolExecutor:
    """Adapts a ``(name, args) -> str`` mock to the middleware ToolExecutor."""

    def __init__(self, fn: Callable[[str, dict[str, Any]], str]) -> None:
        self._fn = fn

    def execute(self, tool_name: str, args: dict[str, Any]) -> tuple[Any, bool, str]:
        return self._fn(tool_name, args), True, ""


def _execute_governed(
    middleware: MiddlewareImpl, caller: CallerContext, tool_call: dict[str, Any]
) -> tuple[str, bool]:
    """Run one model tool call through the middleware.

    Returns the tool-result content to feed back and whether the policy
    denied the call. Unparseable arguments are reported back to the model
    as a tool error without executing anything.
    """
    raw = tool_call.get("arguments") or ""
    try:
        args = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        args = None
    if not isinstance(args, dict):
        return json.dumps({"error": "invalid_arguments", "source": "harness"}), False

    result, ok, error = middleware.execute(
        tool_call["name"], args, caller, framework="agentware-evals"
    )
    if ok:
        return result if isinstance(result, str) else json.dumps(result), False
    denied = error.startswith(_POLICY_DENIED_PREFIX)
    return (
        json.dumps({
            "error": "denied" if denied else "tool_error",
            "reason": error,
            "source": "policy" if denied else "executor",
        }),
        denied,
    )
