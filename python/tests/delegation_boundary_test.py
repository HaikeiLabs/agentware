"""Delegation-boundary contract tests: the proxy delegation envelope.

These pin the agentware side of the delegation protocol documented in
``docs/tenant-proxy-reference.md``:

> a proxy protocol carrying delegation context: ``invoking_subject``,
> ``agent_id``, tenant/workspace, ``trace_id``, idempotency key.

The original human subject is first-class on :class:`CallerContext`
(``invoking_subject``); the extended envelope (organization, workspace,
``agent_id``, ``connector_id``, capability, action, resource, and
``trace_id``) rides in ``CallerContext.metadata`` so it survives every
delegation hop and reaches the decision point and executor unchanged.

The tests prove:

- **preservation** — every envelope field survives every ``delegate()`` hop
  verbatim, and a child cannot mutate the parent's envelope;
- **attribution** — authorization and audit resolve to the original human
  subject, never to the subagent's ``agent_id`` or a forged identity;
- **fail closed** — a missing caller, a missing envelope, or a forged context
  is never promoted to an allow; every envelope field is required.

No Kei invocation envelope, connector contract, migration, deployment, or
credential is modified: these tests assert the boundary that exists.
"""

import json
import sys
from pathlib import Path

# Point at THIS worktree's package (which contains `kei`), independent of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from pedro_agentware.kei import KeiProxyEvaluator
from pedro_agentware.middleware import (
    Action,
    AuditedToolClient,
    AuditFilter,
    CallerContext,
    Decision,
    InMemoryAuditor,
    MiddlewareImpl,
)
from pedro_agentware.middleware.policy import Policy, SimplePolicyEvaluator

ENVELOPE_PATH = Path(__file__).resolve().parent / "fixtures" / "kei" / "delegation-envelope-v1.json"
ENVELOPE = json.loads(ENVELOPE_PATH.read_text())

# Fields that ride in CallerContext.metadata. invoking_subject is first-class.
METADATA_ENVELOPE_FIELDS = {
    "organization",
    "workspace",
    "agent_id",
    "connector_id",
    "capability",
    "action",
    "resource",
    "trace_id",
}


def _deny_all() -> SimplePolicyEvaluator:
    return SimplePolicyEvaluator(Policy(rules=[], default_deny=True))


def _human_context(**overrides):
    """A caller carrying the full delegation envelope at the human entry point."""
    metadata = {k: v for k, v in ENVELOPE.items() if k != "invoking_subject"}
    base = dict(
        user_id=ENVELOPE["invoking_subject"],
        invoking_subject=ENVELOPE["invoking_subject"],
        session_id="C123",
        metadata=metadata,
    )
    base.update(overrides)
    return CallerContext(**base)


class TestDelegationEnvelopePreserved:
    """The full envelope survives every delegation hop, verbatim."""

    def test_envelope_survives_every_delegation_hop(self):
        human = _human_context()
        child = human.delegate("span-1")
        grandchild = child.delegate("span-2")

        assert (human.delegation_depth, child.delegation_depth, grandchild.delegation_depth) == (
            0,
            1,
            2,
        )
        for depth, ctx in ((0, human), (1, child), (2, grandchild)):
            for field in METADATA_ENVELOPE_FIELDS:
                assert ctx.metadata[field] == ENVELOPE[field], (depth, field)
            assert ctx.invoking_subject == ENVELOPE["invoking_subject"]

    def test_delegate_copies_envelope_so_children_cannot_mutate_the_parent(self):
        human = _human_context()
        child = human.delegate("span-1")

        child.metadata["trace_id"] = "forged-trace"

        assert human.metadata["trace_id"] == ENVELOPE["trace_id"]

    def test_envelope_survives_a_metadata_override_on_delegate(self):
        human = _human_context()
        child = human.delegate("span-1", metadata={**human.metadata, "subtask_id": "t1"})

        for field in METADATA_ENVELOPE_FIELDS:
            assert child.metadata[field] == ENVELOPE[field]
        assert child.metadata["subtask_id"] == "t1"
        assert child.invoking_subject == ENVELOPE["invoking_subject"]


class TestOriginalHumanSubjectPreserved:
    """Authorization and audit resolve to the human, never the subagent."""

    def test_agent_runs_under_its_own_id_but_the_human_survives(self):
        human = _human_context()
        sub = human.delegate(span="sub-1")
        sub.user_id = ENVELOPE["agent_id"]  # the subagent runs under its service id

        assert sub.invoking_subject == ENVELOPE["invoking_subject"]
        assert sub.metadata["agent_id"] == ENVELOPE["agent_id"]
        assert sub.user_id == ENVELOPE["agent_id"]

    def test_delegate_refuses_to_overwrite_the_human_subject(self):
        human = _human_context()

        forged = human.delegate("span-1", invoking_subject="attacker@example.com")

        assert forged.invoking_subject == ENVELOPE["invoking_subject"]

    def test_metadata_cannot_smuggle_an_authoritative_subject(self):
        forged = _human_context()
        forged.metadata["invoking_subject"] = "attacker@example.com"

        # metadata is descriptive; the authoritative subject is first-class.
        assert forged.invoking_subject == ENVELOPE["invoking_subject"]


class TestMissingContextFailsClosed:
    """No caller: untrusted, empty envelope, no fabricated attribution."""

    def test_default_context_is_untrusted_with_an_empty_envelope(self):
        ctx = CallerContext()

        assert ctx.trusted is False
        assert ctx.invoking_subject == ""
        assert ctx.metadata == {}
        assert ctx.delegation_depth == 0

    @pytest.mark.asyncio
    async def test_client_without_a_caller_denied_by_a_deny_all_policy(self):
        client = AuditedToolClient(evaluator=_deny_all())

        with pytest.raises(PermissionError):
            await client.Execute("github.create_issue", {}, "U1", "C1", None, lambda **k: "never")

        assert client.records()[0].decision.action == Action.DENY

    @pytest.mark.asyncio
    async def test_client_without_a_caller_still_audits_as_the_entry_user(self):
        client = AuditedToolClient(source="test-harness")

        async def tool(**kwargs):
            return "ok"

        await client.Execute("echo", {"value": "hi"}, "U_HUMAN", "C123", None, tool)

        rec = client.records()[0]
        assert rec.invoking_subject == "U_HUMAN"  # human entry point
        assert rec.delegation_depth == 0
        assert rec.framework == "test-harness"


class TestForgedAndIncompleteContextFailsClosed:
    """A forged or incomplete envelope is never promoted to an allow."""

    def test_envelope_without_a_human_subject_is_not_authoritative(self):
        forged = CallerContext(
            user_id=ENVELOPE["agent_id"],
            metadata={
                "organization": ENVELOPE["organization"],
                "workspace": ENVELOPE["workspace"],
                "agent_id": ENVELOPE["agent_id"],
                "connector_id": ENVELOPE["connector_id"],
                "capability": ENVELOPE["capability"],
                "action": ENVELOPE["action"],
                "resource": ENVELOPE["resource"],
                "trace_id": "forged-trace",
            },
        )

        assert forged.invoking_subject == ""
        assert forged.trusted is False

    @pytest.mark.asyncio
    async def test_forged_context_without_a_subject_is_denied_at_the_boundary(self):
        class RequireHuman:
            def evaluate(self, tool_name, args, caller):
                if not caller.invoking_subject:
                    return Decision(
                        action=Action.DENY, rule="require-human", reason="no human subject"
                    )
                return Decision(action=Action.ALLOW, rule="human-ok")

        forged = CallerContext(
            user_id=ENVELOPE["agent_id"],
            metadata={"agent_id": ENVELOPE["agent_id"], "trace_id": "forged-trace"},
        )

        client = AuditedToolClient(evaluator=RequireHuman())
        with pytest.raises(PermissionError, match="no human subject"):
            await client.Execute(
                "github.create_issue",
                {},
                forged.user_id,
                "C1",
                None,
                lambda **k: "never",
                caller=forged,
            )

        assert client.records()[0].decision.action == Action.DENY

    @pytest.mark.asyncio
    async def test_missing_trace_id_fails_closed(self):
        class RequireTrace:
            def evaluate(self, tool_name, args, caller):
                if not caller.metadata.get("trace_id"):
                    return Decision(action=Action.DENY, rule="require-trace", reason="no trace_id")
                return Decision(action=Action.ALLOW, rule="trace-ok")

        client = AuditedToolClient(evaluator=RequireTrace())

        missing_trace = _human_context()
        missing_trace.metadata.pop("trace_id")
        with pytest.raises(PermissionError, match="no trace_id"):
            await client.Execute(
                "t",
                {},
                missing_trace.user_id,
                "C1",
                None,
                lambda **k: "never",
                caller=missing_trace,
            )

    @pytest.mark.asyncio
    async def test_forged_metadata_cannot_stand_in_for_a_missing_required_field(self):
        """A forged metadata key cannot fabricate a required field."""
        missing_trace = _human_context()
        missing_trace.metadata.pop("trace_id")

        assert missing_trace.metadata.get("trace_id") is None
        assert missing_trace.invoking_subject == ENVELOPE["invoking_subject"]


class TestProxyBoundaryCarriesEnvelopeAndAuthorizesTheHuman:
    """The envelope reaches the decision point; the proxy authorizes the human."""

    class RecordingProxyEvaluator(KeiProxyEvaluator):
        def __init__(self, client):
            super().__init__(client)
            self.seen_caller: CallerContext | None = None

        def evaluate(self, tool_name, args, caller):
            self.seen_caller = caller
            return super().evaluate(tool_name, args, caller)

    @pytest.mark.asyncio
    async def test_envelope_reaches_the_decision_point_unchanged(self):
        class FakeProxy:
            def __init__(self):
                self.calls: list[dict] = []

            def authorize(self, user_id, tool, action, resource):
                self.calls.append(
                    {"user_id": user_id, "tool": tool, "action": action, "resource": resource}
                )
                return {"decision": "permit"}

        proxy = FakeProxy()
        evaluator = self.RecordingProxyEvaluator(proxy)

        human = _human_context()
        sub = human.delegate("sub-1")
        sub.user_id = ENVELOPE["agent_id"]

        client = AuditedToolClient(evaluator=evaluator)
        await client.Execute(
            ENVELOPE["capability"],
            {
                "owner": "acme-corp",
                "repo": "sales-pipeline",
                "action": ENVELOPE["action"],
            },
            sub.user_id,
            "C1",
            None,
            lambda **k: "ok",
            caller=sub,
        )

        seen = evaluator.seen_caller
        assert seen is not None
        assert seen.invoking_subject == ENVELOPE["invoking_subject"]
        for field in METADATA_ENVELOPE_FIELDS:
            assert seen.metadata[field] == ENVELOPE[field], field

        # The proxy boundary authorizes the human, not the subagent's id.
        assert proxy.calls[0]["user_id"] == ENVELOPE["invoking_subject"]
        assert proxy.calls[0]["tool"] == ENVELOPE["capability"]
        assert proxy.calls[0]["action"] == ENVELOPE["action"]
        assert proxy.calls[0]["resource"] == ENVELOPE["resource"]

    @pytest.mark.asyncio
    async def test_non_affirmative_decision_denies_even_with_the_full_envelope(self):
        class _Client:
            def authorize(self, user_id, tool, action, resource):
                return {"decision": "deny"}

        client = AuditedToolClient(evaluator=KeiProxyEvaluator(_Client()))
        human = _human_context()

        with pytest.raises(PermissionError):
            await client.Execute(
                "github.read", {}, human.user_id, "C1", None, lambda **k: "never", caller=human
            )

        rec = client.records()[0]
        assert rec.decision.action == Action.DENY
        # A denial still audits as the human.
        assert rec.invoking_subject == ENVELOPE["invoking_subject"]

    @pytest.mark.asyncio
    async def test_unreachable_proxy_denies_even_with_the_full_envelope(self):
        class _Broken:
            def authorize(self, user_id, tool, action, resource):
                raise ConnectionError("proxy unreachable")

        client = AuditedToolClient(evaluator=KeiProxyEvaluator(_Broken()))
        human = _human_context()

        with pytest.raises(PermissionError, match="proxy unreachable"):
            await client.Execute(
                "github.read", {}, human.user_id, "C1", None, lambda **k: "never", caller=human
            )

        assert client.records()[0].decision.action == Action.DENY


class TestAuditRecordCarriesDelegationLinkage:
    """The audit record is the linkage point: human subject + chain + envelope."""

    @pytest.mark.asyncio
    async def test_audit_record_links_the_human_through_the_chain(self):
        client = AuditedToolClient(source="test-harness")
        human = _human_context()
        sub = human.delegate("sub-1")
        sub.user_id = ENVELOPE["agent_id"]

        async def tool(**kwargs):
            return "ok"

        await client.Execute("echo", {}, sub.user_id, "C1", None, tool, caller=sub)

        rec = client.records()[0]
        assert rec.invoking_subject == ENVELOPE["invoking_subject"]
        assert rec.parent_span == "sub-1"
        assert rec.delegation_depth == 1
        assert rec.framework == "test-harness"

    def test_middleware_impl_preserves_the_envelope_to_the_policy(self):
        captured: dict = {}

        class CapturingEvaluator:
            def evaluate(self, tool_name, args, caller):
                captured["caller"] = caller
                return Decision(action=Action.ALLOW, reason="capture")

        class EchoExecutor:
            def execute(self, tool_name, args):
                return ({"echo": args.get("value")}, True, "")

        auditor = InMemoryAuditor()
        mw = MiddlewareImpl(EchoExecutor(), auditor=auditor, evaluator=CapturingEvaluator())

        sub = _human_context().delegate("sub-1")
        sub.user_id = ENVELOPE["agent_id"]
        mw.execute("echo", {"value": "hi"}, sub)

        seen = captured["caller"]
        assert seen.invoking_subject == ENVELOPE["invoking_subject"]
        for field in METADATA_ENVELOPE_FIELDS:
            assert seen.metadata[field] == ENVELOPE[field], field

        record = auditor.query(AuditFilter())[0]
        assert record.invoking_subject == ENVELOPE["invoking_subject"]
        assert record.delegation_depth == 1
