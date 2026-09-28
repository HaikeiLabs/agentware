"""Tests for KeiProxyEvaluator.

These exercise the evaluator with no kei-proxy binary present: the proxy call
is an injected double. The claim under test is that enforcement now lives in
agentware -- vocabulary translation, fail-closed behaviour on every non-
affirmative path, and a denial that is both raised and audited.
"""

import pytest

from pedro_agentware.kei import KeiProxyAuthorizeError, KeiProxyEvaluator, resources_touched
from pedro_agentware.middleware import (
    Action,
    AuditedToolClient,
    CallerContext,
    InMemoryAuditor,
)


class FakeProxy:
    """Stands in for a kei-proxy client, returning a canned result."""

    def __init__(self, result=None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[dict] = []

    def authorize(self, user_id: str, tool: str, action: str, resource: str, **context):
        self.calls.append(
            {"user_id": user_id, "tool": tool, "action": action, "resource": resource, **context}
        )
        if self._raises is not None:
            raise self._raises
        return self._result


class LegacyProxy:
    """A pre-context client that accepts only the original four arguments."""

    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[str, str, str, str]] = []

    def authorize(self, user_id: str, tool: str, action: str, resource: str):
        self.calls.append((user_id, tool, action, resource))
        return self.result


def evaluate(result=None, raises: Exception | None = None, args: dict | None = None):
    """Run one evaluation against a fake proxy and return (decision, proxy)."""
    proxy = FakeProxy(result=result, raises=raises)
    evaluator = KeiProxyEvaluator(proxy)
    decision = evaluator.evaluate(
        "github.create_issue",
        args if args is not None else {"owner": "acme", "repo": "sales-pipeline"},
        CallerContext(user_id="U123", invoking_subject="U_HUMAN"),
    )
    return decision, proxy


# --- vocabulary translation -------------------------------------------------
#
# The decision vocabulary (allow/permit/deny/enrollment_required/unknown,
# enrollment carry, malformed output, exit codes, timeouts) is covered by the
# cross-language parity table in authorize_cases_test.py, driven from
# fixtures/kei/authorize-cases.v1.json. The cases below are Python-only: they
# exercise the injected-client seam, which the subprocess table cannot reach.


def test_reason_is_prefixed_with_its_class():
    decision, _ = evaluate({"decision": "deny", "reason": "user lacks github:write"})
    assert decision.reason == "kei-proxy deny: user lacks github:write"


@pytest.mark.parametrize(
    "value",
    ["", "PERMITTED", "maybe", "ALLOWED", "permit-with-conditions", "null", "0"],
)
def test_near_miss_decision_strings_fail_closed(value):
    decision, _ = evaluate({"decision": value})
    assert decision.action == Action.DENY, f"{value!r} must not allow"


def test_none_response_fails_closed():
    decision, _ = evaluate(None)
    assert decision.action == Action.DENY


def test_dataclass_shaped_response_is_accepted():
    """A harness client returns its own object, not a dict."""

    class Result:
        decision = "permit"
        reason = "ok"

    decision, _ = evaluate(Result())
    assert decision.action == Action.ALLOW


# --- fail closed on errors --------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("proxy unreachable"),
        TimeoutError("proxy timed out"),
        RuntimeError("malformed JSON from proxy"),
        ValueError("boom"),
    ],
)
def test_proxy_exception_denies_and_never_allows(exc):
    decision, _ = evaluate(raises=exc)
    assert decision.action == Action.DENY
    assert decision.reason


def test_exception_reason_names_the_failure():
    decision, _ = evaluate(raises=ConnectionError("proxy unreachable"))
    assert decision.reason.startswith("kei-proxy proxy_error:")
    assert "proxy unreachable" in decision.reason


def test_typed_authorize_error_keeps_its_class():
    decision, _ = evaluate(raises=KeiProxyAuthorizeError("proxy_timeout", "no answer"))
    assert decision.action == Action.DENY
    assert decision.reason == "kei-proxy proxy_timeout: no answer"


# --- policy attribution -----------------------------------------------------


def test_policy_spelling_is_accepted_for_the_policy_id():
    decision, _ = evaluate({"decision": "deny", "policy": "pol-42", "reason": "nope"})
    assert decision.rule == "pol-42"


def test_rule_falls_back_when_the_proxy_supplies_no_policy_id():
    decision, _ = evaluate({"decision": "permit"})
    assert decision.rule == KeiProxyEvaluator.RULE


# --- what is sent to the proxy ----------------------------------------------


def test_invoking_subject_is_the_authorized_identity():
    """Authorization resolves back to the human, not the agent's own identity."""
    _, proxy = evaluate({"decision": "permit"})
    assert proxy.calls[0]["user_id"] == "U_HUMAN"


def test_falls_back_to_user_id_when_no_invoking_subject():
    proxy = FakeProxy({"decision": "permit"})
    KeiProxyEvaluator(proxy).evaluate("github.read", {}, CallerContext(user_id="U123"))
    assert proxy.calls[0]["user_id"] == "U123"


def test_resource_sent_to_the_proxy_is_derived_from_args():
    _, proxy = evaluate({"decision": "permit"})
    assert proxy.calls[0]["resource"] == "github:repo:acme/sales-pipeline"


def test_full_audit_context_is_sent_to_the_proxy():
    proxy = FakeProxy({"decision": "permit"})
    caller = CallerContext(
        user_id="agent-user",
        invoking_subject="U_HUMAN",
        parent_span="parent-1",
        delegation_depth=2,
        span_id="span-1",
        agent_id="agent-1",
        agent_version="1.2.3",
        framework="agentware",
        workspace_id="workspace-1",
    )
    args = {"owner": "acme", "repo": "sales-pipeline"}
    KeiProxyEvaluator(proxy).evaluate("github.create_issue", args, caller)

    call = proxy.calls[0]
    assert call["span_id"] == "span-1"
    assert call["invoking_subject"] == "U_HUMAN"
    assert call["parent_span"] == "parent-1"
    assert call["delegation_depth"] == 2
    assert call["agent_id"] == "agent-1"
    assert call["framework"] == "agentware"
    assert call["workspace_id"] == "workspace-1"
    assert call["resources"] == ["github:repo:acme/sales-pipeline"]
    assert len(call["tool_args_digest"]) == 64


def test_legacy_proxy_receives_the_original_four_arguments():
    proxy = LegacyProxy({"decision": "permit"})
    decision = KeiProxyEvaluator(proxy).evaluate(
        "github.read",
        {"owner": "acme", "repo": "sales-pipeline"},
        CallerContext(user_id="U123", invoking_subject="U_HUMAN"),
    )

    assert decision.action == Action.ALLOW
    assert proxy.calls == [("U_HUMAN", "github.read", "execute", "github:repo:acme/sales-pipeline")]


# --- resources_touched ------------------------------------------------------


def test_resources_touched_uses_the_type_kind_id_triple():
    assert resources_touched(
        "github.create_issue", {"owner": "acme", "repo": "sales-pipeline"}
    ) == ["github:repo:acme/sales-pipeline"]


def test_resources_touched_includes_the_issue_from_args():
    touched = resources_touched(
        "github.comment", {"owner": "acme", "repo": "sales-pipeline", "issue_number": 123}
    )
    assert touched == ["github:repo:acme/sales-pipeline", "github:issue:123"]


def test_every_entry_has_exactly_three_colon_separated_parts():
    touched = resources_touched(
        "github.update",
        {"owner": "acme", "repo": "sales-pipeline", "issue_number": 7, "branch": "main"},
    )
    assert touched
    for entry in touched:
        assert len(entry.split(":")) == 3, entry


def test_underscore_tool_names_yield_the_same_resource_type():
    assert resources_touched("github_create_issue", {"issue_number": 5}) == ["github:issue:5"]


def test_args_with_no_recognisable_resources_yield_nothing():
    assert resources_touched("github.list", {"limit": 10}) == []


def test_resources_touched_does_not_invent_post_call_resources():
    """Only what the args name is knowable pre-call.

    A tool that returns a created issue number reveals it in its result, not in
    its arguments, so it cannot appear here.
    """
    touched = resources_touched("github.create_issue", {"owner": "acme", "repo": "pipe"})
    assert touched == ["github:repo:acme/pipe"]
    assert not any(part.startswith("github:issue:") for part in touched)


# --- the point: a denial is both enforced and audited -----------------------


@pytest.mark.asyncio
async def test_denial_raises_permission_error_and_is_audited_as_a_denial():
    auditor = InMemoryAuditor()
    evaluator = KeiProxyEvaluator(FakeProxy({"decision": "deny", "reason": "not enrolled"}))
    client = AuditedToolClient(evaluator=evaluator, auditor=auditor)

    def tool(**kwargs):
        raise AssertionError("a denied tool must never execute")

    with pytest.raises(PermissionError, match="not enrolled"):
        await client.Execute(
            "github.create_issue",
            {"owner": "acme", "repo": "sales-pipeline"},
            "U123",
            "C456",
            None,
            tool,
        )

    records = client.records()
    assert len(records) == 1
    assert records[0].decision.action == Action.DENY
    assert "not enrolled" in records[0].decision.reason
    assert records[0].tool_name == "github.create_issue"


@pytest.mark.asyncio
async def test_enrollment_required_also_raises_and_is_audited():
    auditor = InMemoryAuditor()
    evaluator = KeiProxyEvaluator(FakeProxy({"decision": "enrollment_required"}))
    client = AuditedToolClient(evaluator=evaluator, auditor=auditor)

    with pytest.raises(PermissionError):
        await client.Execute("github.read", {}, "U123", "C456", None, lambda **kw: "never")

    assert client.records()[0].decision.action == Action.DENY


@pytest.mark.asyncio
async def test_proxy_failure_denies_the_call_rather_than_running_it():
    evaluator = KeiProxyEvaluator(FakeProxy(raises=ConnectionError("down")))
    client = AuditedToolClient(evaluator=evaluator)

    def tool(**kwargs):
        raise AssertionError("must not execute when the proxy is unreachable")

    with pytest.raises(PermissionError):
        await client.Execute("github.read", {}, "U123", "C456", None, tool)

    assert client.records()[0].decision.action == Action.DENY


@pytest.mark.asyncio
async def test_permit_executes_the_tool_and_audits_the_allow():
    evaluator = KeiProxyEvaluator(FakeProxy({"decision": "permit"}))
    client = AuditedToolClient(evaluator=evaluator)

    result = await client.Execute(
        "github.read",
        {"owner": "acme", "repo": "sales-pipeline"},
        "U123",
        "C456",
        None,
        lambda **kwargs: "ran",
    )

    assert result == "ran"
    assert client.records()[0].decision.action == Action.ALLOW


@pytest.mark.asyncio
async def test_evaluator_does_not_inject_arguments_into_the_tool_call():
    """The Decision must not smuggle resource lineage into the tool's args."""
    seen: dict = {}

    def tool(**kwargs):
        seen.update(kwargs)
        return "ran"

    client = AuditedToolClient(evaluator=KeiProxyEvaluator(FakeProxy({"decision": "permit"})))
    await client.Execute(
        "github.read", {"owner": "acme", "repo": "pipe"}, "U123", "C456", None, tool
    )

    assert seen == {"owner": "acme", "repo": "pipe"}
