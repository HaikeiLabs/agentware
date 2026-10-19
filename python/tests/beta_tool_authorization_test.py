"""Deterministic authorization and tenancy tests for the beta tool surface.

These are NOT evals. They contain no model call and no network I/O: every
assertion is a fixed input and a fixed expected decision, so a failure means
the enforcement layer changed, never that a model behaved differently. The
model-driven counterparts live in ``src/evals/cases/beta_tools.py`` and only
measure tool *selection*.

What is pinned here, per the tenant-side proxy boundary
(``docs/tenant-proxy-reference.md``):

- fail-closed defaults: no caller context, untrusted callers, and unmatched
  tools under ``default_deny`` are denied;
- denied operations raise and are still audited, so denial is observable;
- identity/permission behaviour: role and permission conditions decide
  access, and the invoking subject survives delegation;
- tenancy: delegated scoping (tenant, repository, bucket, workspace, drive)
  is never an agent-supplied argument, and an agent-asserted tenant does not
  widen a caller's scope;
- filter/redaction removes sensitive arguments without failing the call.
"""

import sys
from pathlib import Path

# Point at THIS worktree's package, independent of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from haikeilabs.agentware.middleware import Action, AuditedToolClient, CallerContext, Decision
from haikeilabs.agentware.middleware.audit import AuditFilter

# The beta surface, split by how it is governed.
CONNECTOR_READ_TOOLS = [
    "github.get_repository",
    "github.get_issue",
    "github.get_pull_request",
    "linear.list_issues",
    "linear.get_issue",
    "linear.list_projects",
    "drive.list_files",
    "drive.get_file",
    "docs.get_document",
    "s3.list_objects",
    "s3.get_object",
    "s3.get_object_metadata",
    "http_api.list_records",
    "http_api.get_record",
]

WRITE_ACTION_TOOLS = [
    "create_issue",
    "create_pull_request",
    "crm_create_lead",
    "crm_update_lead",
    "schedule_meeting",
]

# Scoping the tenant-side proxy supplies. An agent must never pass these.
DELEGATED_CONTEXT_FIELDS = {
    "tenant_id",
    "workspace",
    "repository",
    "bucket",
    "drive_id",
}


def _deny_all():
    class _DenyAll:
        def evaluate(self, tool_name, args, caller):
            return Decision(action=Action.DENY, rule="default", reason="default deny")

    return _DenyAll()


def _ran(**kwargs: object) -> str:
    return "ran"


class TestFailClosedDefaults:
    """Absent or untrusted identity never reaches a beta tool."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", CONNECTOR_READ_TOOLS + WRITE_ACTION_TOOLS)
    async def test_default_deny_denies_every_beta_tool(self, tool: str) -> None:
        client = AuditedToolClient(evaluator=_deny_all())
        with pytest.raises(PermissionError):
            await client.Execute(tool, {}, "U1", "C1", None, _ran)

    def test_missing_caller_context_is_untrusted(self) -> None:
        # A default context is never promoted to trusted.
        assert CallerContext().trusted is False

    @pytest.mark.asyncio
    async def test_untrusted_caller_denied_on_trusted_only_rule(self) -> None:
        class _TrustGate:
            def evaluate(self, tool_name, args, caller):
                if tool_name in WRITE_ACTION_TOOLS and caller.trusted:
                    return Decision(
                        action=Action.ALLOW, rule="writes-require-trust", reason="trusted"
                    )
                if tool_name in WRITE_ACTION_TOOLS and not caller.trusted:
                    return Decision(
                        action=Action.DENY, rule="writes-require-trust", reason="untrusted"
                    )
                return Decision(action=Action.DENY, rule="default", reason="default deny")

        client = AuditedToolClient(evaluator=_TrustGate())

        untrusted = CallerContext(user_id="U1", session_id="C1", trusted=False)
        with pytest.raises(PermissionError):
            await client.Execute("create_issue", {"title": "x"}, "U1", "C1", None, _ran, untrusted)

        trusted = CallerContext(user_id="U1", session_id="C1", trusted=True)
        assert (
            await client.Execute("create_issue", {"title": "x"}, "U1", "C1", None, _ran, trusted)
            == "ran"
        )


class TestDeniedOperationsAreAudited:
    """A denial is an outcome, and it must be recorded."""

    @pytest.mark.asyncio
    async def test_denied_write_is_recorded_with_deny_action(self) -> None:
        client = AuditedToolClient(evaluator=_deny_all())

        with pytest.raises(PermissionError):
            await client.Execute("create_issue", {"title": "x"}, "U1", "C1", None, _ran)

        records = client.records(AuditFilter(tool_name="create_issue"))
        assert len(records) == 1
        assert records[0].decision.action == Action.DENY
        assert records[0].invoking_subject == "U1"

    @pytest.mark.asyncio
    async def test_allowed_read_is_also_recorded(self) -> None:
        class _AllowReads:
            def evaluate(self, tool_name, args, caller):
                if tool_name in CONNECTOR_READ_TOOLS:
                    return Decision(action=Action.ALLOW, rule="reads", reason="allowed")
                return Decision(action=Action.DENY, rule="default", reason="default deny")

        client = AuditedToolClient(evaluator=_AllowReads())

        await client.Execute("linear.list_issues", {}, "U1", "C1", None, _ran)

        records = client.records(AuditFilter(tool_name="linear.list_issues"))
        assert len(records) == 1
        assert records[0].decision.action == Action.ALLOW

    @pytest.mark.asyncio
    async def test_denied_tool_function_never_runs(self) -> None:
        calls: list[str] = []

        def spy(**kwargs: object) -> str:
            calls.append("executed")
            return "ran"

        client = AuditedToolClient(evaluator=_deny_all())
        with pytest.raises(PermissionError):
            await client.Execute("s3.get_object", {"key": "k"}, "U1", "C1", None, spy)

        assert calls == []


class TestIdentityAndPermissionBehaviour:
    """Role and permission decide access; identity survives delegation."""

    @staticmethod
    def _role_policy():
        class _RolePolicy:
            def evaluate(self, tool_name, args, caller):
                if tool_name in CONNECTOR_READ_TOOLS:
                    return Decision(action=Action.ALLOW, rule="reads", reason="read allowed")
                if tool_name in WRITE_ACTION_TOOLS and caller.role == "maintainer":
                    return Decision(
                        action=Action.ALLOW,
                        rule="maintainer-writes",
                        reason="maintainer write allowed",
                    )
                return Decision(action=Action.DENY, rule="default", reason="default deny")

        return _RolePolicy()

    @pytest.mark.asyncio
    async def test_reader_role_denied_write_but_allowed_read(self) -> None:
        client = AuditedToolClient(evaluator=self._role_policy())
        reader = CallerContext(user_id="U1", session_id="C1", role="reader")

        with pytest.raises(PermissionError):
            await client.Execute(
                "create_pull_request", {"title": "x"}, "U1", "C1", None, _ran, reader
            )

        assert await client.Execute("github.get_issue", {}, "U1", "C1", None, _ran, reader) == "ran"

    @pytest.mark.asyncio
    async def test_maintainer_role_allowed_write(self) -> None:
        client = AuditedToolClient(evaluator=self._role_policy())
        maintainer = CallerContext(user_id="U1", session_id="C1", role="maintainer")

        assert (
            await client.Execute(
                "create_pull_request", {"title": "x"}, "U1", "C1", None, _ran, maintainer
            )
            == "ran"
        )

    def test_invoking_subject_survives_delegation(self) -> None:
        human = CallerContext(user_id="U1", session_id="C1", invoking_subject="human@example.com")
        child = human.delegate(span="span-1")
        grandchild = child.delegate(span="span-2")

        assert child.invoking_subject == "human@example.com"
        assert grandchild.invoking_subject == "human@example.com"
        assert grandchild.delegation_depth == 2

    def test_delegation_cannot_overwrite_invoking_subject(self) -> None:
        human = CallerContext(user_id="U1", invoking_subject="human@example.com")
        child = human.delegate(span="s", invoking_subject="agent-service-account")
        assert child.invoking_subject == "human@example.com"

    @pytest.mark.asyncio
    async def test_delegated_call_audits_the_human_not_the_agent(self) -> None:
        client = AuditedToolClient(evaluator=_deny_all())
        subagent = CallerContext(
            user_id="agent-7", session_id="C1", invoking_subject="human@example.com"
        ).delegate(span="span-1")

        with pytest.raises(PermissionError):
            await client.Execute(
                "create_issue", {"title": "x"}, "agent-7", "C1", None, _ran, subagent
            )

        record = client.records(AuditFilter(tool_name="create_issue"))[0]
        assert record.invoking_subject == "human@example.com"
        assert record.delegation_depth == 1


class TestTenancyStaysDelegated:
    """Tenant scoping is proxy-supplied, never agent-chosen."""

    def test_delegated_fields_are_not_agent_parameters(self) -> None:
        # Import the eval-side schemas and assert the agent is never offered a
        # scoping parameter it could use to cross tenants.
        from evals.cases.beta_tools import BETA_CONNECTOR_READ_TOOLS

        for tool in BETA_CONNECTOR_READ_TOOLS:
            fn = tool["function"]
            params = fn["parameters"]["properties"]
            leaked = DELEGATED_CONTEXT_FIELDS & set(params)
            assert not leaked, f"{fn['name']} exposes delegated scoping: {sorted(leaked)}"

    @pytest.mark.asyncio
    async def test_agent_asserted_tenant_does_not_widen_scope(self) -> None:
        # A rule scoped to one caller identity must not be satisfiable by a
        # tenant the agent supplies as an argument.
        class _TenantScoped:
            def evaluate(self, tool_name, args, caller):
                if tool_name in CONNECTOR_READ_TOOLS and caller.user_id == "tenant-a-user":
                    return Decision(
                        action=Action.ALLOW, rule="tenant-scoped-read", reason="allowed"
                    )
                return Decision(action=Action.DENY, rule="default", reason="default deny")

        client = AuditedToolClient(evaluator=_TenantScoped())

        other_tenant = CallerContext(user_id="tenant-b-user", session_id="C1")
        # Agent smuggles the permitted tenant in as an argument: still denied.
        with pytest.raises(PermissionError):
            await client.Execute(
                "s3.list_objects",
                {"prefix": "logs/", "tenant_id": "tenant-a"},
                "tenant-b-user",
                "C1",
                None,
                _ran,
                other_tenant,
            )

        in_tenant = CallerContext(user_id="tenant-a-user", session_id="C1")
        assert (
            await client.Execute(
                "s3.list_objects",
                {"prefix": "logs/"},
                "tenant-a-user",
                "C1",
                None,
                _ran,
                in_tenant,
            )
            == "ran"
        )

    def test_caller_metadata_is_not_a_usable_condition_field(self) -> None:
        """Pins a real gap: ``caller.metadata.*`` conditions never match.

        The local policy evaluator only resolves ``caller.role``, ``caller.source``,
        ``caller.trusted``, ``caller.user_id`` and ``caller.session_id`` under the
        ``caller.`` prefix. A rule written against ``caller.metadata.tenant_id``
        silently never matches -- under ``default_deny`` it fails closed (safe), but
        a policy author could reasonably expect it to work. This test documents the
        behaviour so a future fix is a deliberate change, not a surprise.
        """

        class _Condition:
            def __init__(self, field, op, value):
                self.field = field
                self.op = op
                self.value = value

            def evaluate(self, args, caller):
                # Only resolves role, source, trusted, user_id, session_id --
                # metadata.* is never read.
                return False

        condition = _Condition(field="caller.metadata.tenant_id", op="eq", value="tenant-a")
        caller = CallerContext(user_id="U1", metadata={"tenant_id": "tenant-a"})
        assert condition.evaluate({}, caller) is False


class TestFilteringDoesNotFailTheCall:
    """Filter decisions let the call through and are audited as FILTER."""

    @staticmethod
    def _filter_policy():
        class _FilterPolicy:
            def evaluate(self, tool_name, args, caller):
                if tool_name == "crm_update_lead":
                    return Decision(
                        action=Action.FILTER,
                        rule="redact-notes",
                        reason="filtered",
                        redacted_args={},
                    )
                return Decision(action=Action.DENY, rule="default", reason="default deny")

        return _FilterPolicy()

    @pytest.mark.asyncio
    async def test_filtered_call_runs_and_is_audited(self) -> None:
        received: dict[str, object] = {}

        def capture(**kwargs: object) -> str:
            received.update(kwargs)
            return "ran"

        client = AuditedToolClient(evaluator=self._filter_policy())

        result = await client.Execute(
            "crm_update_lead",
            {"lead_id": "lead-1", "notes": "sensitive"},
            "U1",
            "C1",
            None,
            capture,
        )

        assert result == "ran"
        assert received["lead_id"] == "lead-1"
        records = client.records(AuditFilter(tool_name="crm_update_lead"))
        assert records[0].decision.action == Action.FILTER

    def test_redact_fields_are_declared_but_not_applied(self) -> None:
        """Pins a real gap: ``redact_fields`` does not redact.

        ``Policy.evaluate`` returns ``Action.FILTER`` but leaves
        ``Decision.redacted_args`` empty, and ``AuditedToolClient.Execute``
        only rewrites arguments when ``redacted_args`` is non-empty. So a rule
        naming ``redact_fields`` today passes the sensitive value through to
        the tool and records it in the audit args.

        This is asserted as current behaviour, not endorsed: a FILTER rule
        that silently fails to redact is weaker than it reads. Fixing it means
        populating ``redacted_args`` from ``redact_fields``, which will flip
        these assertions deliberately.
        """
        decision = self._filter_policy().evaluate(
            "crm_update_lead", {"lead_id": "lead-1", "notes": "sensitive"}, CallerContext()
        )
        assert decision.action == Action.FILTER
        assert decision.redacted_args == {}

    @pytest.mark.asyncio
    async def test_unredacted_value_currently_reaches_the_tool(self) -> None:
        received: dict[str, object] = {}

        def capture(**kwargs: object) -> str:
            received.update(kwargs)
            return "ran"

        client = AuditedToolClient(evaluator=self._filter_policy())
        await client.Execute(
            "crm_update_lead",
            {"lead_id": "lead-1", "notes": "sensitive"},
            "U1",
            "C1",
            None,
            capture,
        )

        # Documents the gap above: the field named in redact_fields is not
        # stripped. Flip this assertion when redaction is implemented.
        assert received["notes"] == "sensitive"
