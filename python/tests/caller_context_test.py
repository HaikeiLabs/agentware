"""Tests for CallerContext delegation fields (Go parity)."""

import sys

sys.path.insert(0, "src")

import pytest

from haikeilabs.agentware.middleware import (
    AuditedToolClient,
    AuditFilter,
    CallerContext,
    InMemoryAuditor,
)


def test_defaults_are_fail_closed():
    """An unpopulated context must not be trusted."""
    assert CallerContext().trusted is False


def test_has_go_parity_delegation_fields():
    ctx = CallerContext(invoking_subject="U_HUMAN", parent_span="span-0", delegation_depth=2)

    assert ctx.invoking_subject == "U_HUMAN"
    assert ctx.parent_span == "span-0"
    assert ctx.delegation_depth == 2


def test_delegate_preserves_invoking_subject_and_increments_depth():
    parent = CallerContext(user_id="pedro-agent", invoking_subject="U_HUMAN", session_id="C123")

    child = parent.delegate("span-parent")
    grandchild = child.delegate("span-child")

    assert child.invoking_subject == "U_HUMAN"
    assert grandchild.invoking_subject == "U_HUMAN"
    assert (parent.delegation_depth, child.delegation_depth, grandchild.delegation_depth) == (
        0,
        1,
        2,
    )
    assert child.parent_span == "span-parent"
    assert grandchild.parent_span == "span-child"
    assert parent.delegation_depth == 0, "delegating must not mutate the parent"


def test_delegate_refuses_to_overwrite_invoking_subject():
    """The whole point: a subagent cannot claim to be the human."""
    parent = CallerContext(invoking_subject="U_HUMAN")

    child = parent.delegate("span-a", invoking_subject="pedro-agent", role="subagent")

    assert child.invoking_subject == "U_HUMAN"
    assert child.role == "subagent"


def test_delegate_copies_metadata():
    parent = CallerContext(invoking_subject="U_HUMAN", metadata={"channel_id": "C1"})

    child = parent.delegate("span-a")
    child.metadata["channel_id"] = "C2"

    assert parent.metadata["channel_id"] == "C1"


def test_delegate_accepts_a_metadata_override():
    parent = CallerContext(invoking_subject="U_HUMAN", metadata={"channel_id": "C1"})

    child = parent.delegate("span-a", metadata={**parent.metadata, "subtask_id": "t1"})

    assert child.metadata == {"channel_id": "C1", "subtask_id": "t1"}
    assert parent.metadata == {"channel_id": "C1"}
    assert child.invoking_subject == "U_HUMAN"


async def test_audited_tool_client_records_and_executes():
    client = AuditedToolClient()

    async def tool(value: str) -> str:
        return f"got {value}"

    result = await client.Execute(
        tool_name="echo",
        tool_args={"value": "hi"},
        user_id="U_HUMAN",
        channel_id="C123",
        guild_id=None,
        func=tool,
    )

    assert result == "got hi"
    records = client.records()
    assert len(records) == 1
    assert records[0].tool_name == "echo"
    assert records[0].session_id == "C123"


async def test_audited_tool_client_denies_and_still_audits():
    class DenyAll:
        def evaluate(self, tool_name, args, caller):
            from haikeilabs.agentware.middleware import Action, Decision

            return Decision(action=Action.DENY, rule="deny-all", reason="nope")

    client = AuditedToolClient().with_policy(DenyAll())

    async def tool(**kwargs):  # pragma: no cover - must never run
        raise AssertionError("denied tool must not execute")

    with pytest.raises(PermissionError):
        await client.Execute("echo", {}, "U_HUMAN", "C123", None, tool)

    assert len(client.records()) == 1


async def test_audit_record_links_delegation_fields():
    """The audit record is the linkage point: caller delegation fields appear verbatim."""
    client = AuditedToolClient(source="test-harness")

    async def tool(**kwargs):
        return "ok"

    caller = CallerContext(
        user_id="U_AGENT",
        session_id="C123",
        invoking_subject="alice@example.com",
        parent_span="span-7",
        delegation_depth=2,
    )

    await client.Execute("github_create_issue", {}, "U_AGENT", "C123", None, tool, caller=caller)

    records = client.records()
    assert len(records) == 1
    record = records[0]
    assert record.invoking_subject == "alice@example.com"
    assert record.parent_span == "span-7"
    assert record.delegation_depth == 2
    assert record.framework == "test-harness"
    assert record.session_id == "C123"


async def test_audit_record_stamps_framework_from_source():
    client = AuditedToolClient(source="third-party-harness")

    async def tool(**kwargs):
        return "ok"

    await client.Execute("echo", {}, "U_AGENT", "C123", None, tool)

    assert client.records()[0].framework == "third-party-harness"


async def test_missing_caller_is_fail_closed_and_linked_to_user():
    """No caller supplied: the client must build an untrusted one, not a trusted one."""
    client = AuditedToolClient()

    async def tool(**kwargs):
        return "ok"

    await client.Execute("echo", {"value": "hi"}, "U_HUMAN", "C123", None, tool)

    assert len(client.records()) == 1
    record = client.records()[0]
    # At a human entry point the user is the invoking subject, but the context
    # is still fail-closed: trusted stays False unless someone sets it.
    assert record.invoking_subject == "U_HUMAN"
    assert record.session_id == "C123"
    assert record.delegation_depth == 0
    assert record.framework == "pedro-agentware"


def test_audit_filter_by_delegation_linkage():
    """Records can be joined back to a human across a chain via delegation fields."""
    auditor = InMemoryAuditor()

    def record_span(span, subject, depth):
        from haikeilabs.agentware.middleware import Action, AuditRecord, Decision

        auditor.record(
            AuditRecord(
                session_id="C123",
                tool_name="t",
                args={},
                decision=Decision(action=Action.ALLOW),
                invoking_subject=subject,
                parent_span=span,
                delegation_depth=depth,
            )
        )

    record_span("", "alice@example.com", 0)
    record_span("span-1", "alice@example.com", 1)
    record_span("span-1", "bob@example.com", 1)

    by_span = auditor.query(AuditFilter(parent_span="span-1"))
    assert len(by_span) == 2

    by_subject = auditor.query(AuditFilter(invoking_subject="alice@example.com"))
    assert len(by_subject) == 2
    assert {r.delegation_depth for r in by_subject} == {0, 1}


def test_middleware_impl_links_delegation_fields():
    from haikeilabs.agentware.middleware import InMemoryAuditor, MiddlewareImpl

    auditor = InMemoryAuditor()

    class EchoExecutor:
        def execute(self, tool_name, args):
            return ({"echo": args.get("value")}, True, "")

    mw = MiddlewareImpl(EchoExecutor(), auditor=auditor)

    caller = CallerContext(
        user_id="U_AGENT",
        session_id="C123",
        invoking_subject="alice@example.com",
        parent_span="span-7",
        delegation_depth=1,
    )

    mw.execute("echo", {"value": "hi"}, caller, framework="adk")

    assert len(auditor.query(AuditFilter())) == 1
    record = auditor.query(AuditFilter())[0]
    assert record.invoking_subject == "alice@example.com"
    assert record.parent_span == "span-7"
    assert record.delegation_depth == 1
    assert record.framework == "adk"


def test_middleware_impl_propagates_caller_to_policy():
    """The caller must reach the policy evaluator unchanged and unmutated."""
    from haikeilabs.agentware.middleware import Action, Decision, MiddlewareImpl

    captured = {}

    class CapturingEvaluator:
        def evaluate(self, tool_name, args, caller):
            captured["policy"] = caller
            return Decision(action=Action.ALLOW, reason="capture")

    class EchoExecutor:
        def execute(self, tool_name, args):
            return ({"echo": args.get("value")}, True, "")

    mw = MiddlewareImpl(EchoExecutor(), evaluator=CapturingEvaluator())

    caller = CallerContext(
        user_id="u-1", invoking_subject="alice@example.com", parent_span="span-7"
    )
    mw.execute("echo", {"value": "hi"}, caller)

    assert captured["policy"] is caller, "evaluator must receive the exact caller object"
    assert caller.delegation_depth == 0, "middleware must not mutate the caller"
    assert caller.trusted is False, "caller trust must be preserved, not upgraded"
