"""Contract tests for the action-tool / data-source-connector boundary.

These pin the separation between agentware and the out-of-band layers:

- agentware *models* tool-to-data-source bindings, opaque auth references,
  local policy, and delegation;
- the KEI proxy is the PEP / orchestrator / provider-connector runtime;
- ABAC is the PDP / registration / catalog.

The tests assert what agentware does NOT do: grant permission from
self-reported bindings, resolve or retain connector secret values, require an
ABAC connector to run a governed local tool loop, or own provider execution /
the proxy subprocess.

They deliberately do NOT freeze the KEI taxonomy (semantic tags,
action-vs-read classification), which is still in progress.
"""

import inspect
import json
import os
import sys
from pathlib import Path

# Point at THIS worktree's package (which contains `kei`), independent of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

import pedro_agentware.kei.config as kei_config
import pedro_agentware.kei.evaluator as kei_evaluator
import pedro_agentware.middleware as middleware
from pedro_agentware.kei import KeiProxyEvaluator
from pedro_agentware.kei.config import (
    HarnessConfig,
    SecretRef,
    ToolBinding,
    get_config,
    load_manifest,
)
from pedro_agentware.middleware import Action, AuditedToolClient, CallerContext
from pedro_agentware.middleware.policy import Policy, Rule, SimplePolicyEvaluator

VALID_MANIFEST = Path(__file__).parent / "fixtures" / "kei" / "harness-v1.json"


def _deny_all() -> SimplePolicyEvaluator:
    return SimplePolicyEvaluator(Policy(rules=[], default_deny=True))


class TestBindingsNeverGrantPermission:
    """Invariant 1: tool_bindings are routing metadata, never permission."""

    def test_binding_fields_carry_no_permission_semantics(self):
        # A binding names which connector serves a tool. It has no field that
        # could grant, scope, or classify permission.
        assert set(ToolBinding.model_fields) == {"tool_name", "connector_id", "config"}

    @pytest.mark.asyncio
    async def test_bound_tool_is_still_denied_by_a_deny_all_local_policy(self):
        # The manifest binds web_search and file_read. A deny-all local policy
        # must deny both: the binding grants nothing.
        bound = {b.tool_name for b in load_manifest(VALID_MANIFEST).tool_bindings}
        assert {"web_search", "file_read"} <= bound

        client = AuditedToolClient(evaluator=_deny_all())
        for tool in sorted(bound):
            with pytest.raises(PermissionError):
                await client.Execute(tool, {}, "U1", "C1", None, lambda **k: "ran")

        assert len(client.records()) == len(bound)
        assert all(r.decision.action == Action.DENY for r in client.records())


class TestSecretRefsAreOpaque:
    """Invariant 2: connector secret_refs are opaque, never resolved/exposed."""

    def test_secret_ref_has_only_opaque_identifier_fields(self):
        # A ref is a namespace + identifier. There is no value field to hold a
        # credential.
        assert set(SecretRef.model_fields) == {"source", "key"}

    def test_a_smuggled_secret_value_is_dropped_not_retained(self):
        # A manifest JSON that tries to embed a credential in a ref must not
        # retain it: the value is not a field, so it is dropped on parse.
        ref = SecretRef.model_validate(
            {
                "source": "secret_provider",
                "key": "kei.connector.web.credentials",
                "value": "TOP_SECRET",
            }
        )
        assert "value" not in ref.model_dump()
        assert not hasattr(ref, "value")

    def test_config_module_never_reads_the_environment(self):
        # The config layer resolves no secrets: it must not touch the
        # environment at all. Resolution happens out-of-band on the connector.
        src = Path(kei_config.__file__).read_text()
        assert "os.environ" not in src
        assert "getenv" not in src

    def test_get_config_does_not_pull_secret_values_from_env(self):
        os.environ["KEI_RUNTIME_TOKEN"] = "bootstrap-must-not-appear"
        os.environ["kei.connector.web.credentials"] = "connector-must-not-appear"
        try:
            dumped = json.dumps(get_config(VALID_MANIFEST).model_dump())
            assert "bootstrap-must-not-appear" not in dumped
            assert "connector-must-not-appear" not in dumped
        finally:
            del os.environ["KEI_RUNTIME_TOKEN"]
            del os.environ["kei.connector.web.credentials"]

    def test_harness_config_exposes_no_secret_resolver(self):
        # Nothing on HarnessConfig resolves a ref to a credential.
        for attr in ("resolve_secret", "get_secret", "resolve", "credentials", "secrets"):
            assert not hasattr(HarnessConfig, attr), attr


class TestLocalLoopHasNoABACDependency:
    """Invariant 3: local policy governs an isolated loop, no ABAC dependency."""

    def test_middleware_package_does_not_import_kei(self):
        # The local governance layer must not pull in the ABAC/proxy module.
        for py in Path(middleware.__file__).parent.rglob("*.py"):
            src = py.read_text()
            assert "pedro_agentware.kei" not in src, py
            assert "from ..kei" not in src, py
            assert "from .kei" not in src, py

    @pytest.mark.asyncio
    async def test_isolated_local_loop_enforces_and_audits(self):
        # A governed loop with a local policy and a plain function: no proxy,
        # no kei, no ABAC. It still denies and audits.
        policy = Policy(
            rules=[Rule(name="deny-mutation", tools=["github.create_issue"], action=Action.DENY)]
        )
        client = AuditedToolClient(evaluator=SimplePolicyEvaluator(policy))

        ok = await client.Execute(
            "github.read", {"owner": "a", "repo": "b"}, "U1", "C1", None, lambda **k: "read"
        )
        assert ok == "read"

        with pytest.raises(PermissionError):
            await client.Execute("github.create_issue", {}, "U1", "C1", None, lambda **k: "never")

        assert {r.decision.action for r in client.records()} == {Action.ALLOW, Action.DENY}


class TestProxyIsAnOptionalAdapterBoundary:
    """Invariant 4: proxy governance is an explicit, optional adapter."""

    def test_middleware_has_no_proxy_or_subprocess_seam(self):
        # The middleware never spawns or talks to a proxy directly; the only
        # governance seam is the PolicyEvaluator interface.
        for py in Path(middleware.__file__).parent.rglob("*.py"):
            src = py.read_text()
            assert "subprocess" not in src, py
            assert "os.environ" not in src, py
            assert "pedro_agentware.kei" not in src, py

    def test_kei_proxy_evaluator_requires_an_injected_client(self):
        # The evaluator never builds its own network client or proxy; the
        # client is injected, so the proxy stays an optional adapter.
        params = list(inspect.signature(KeiProxyEvaluator.__init__).parameters)
        assert params[1] == "client"
        assert inspect.signature(KeiProxyEvaluator.__init__).parameters["client"].default is (
            inspect.Parameter.empty
        )

    def test_evaluator_module_does_not_spawn_the_proxy(self):
        src = Path(kei_evaluator.__file__).read_text()
        assert "from .proxy" not in src
        assert "subprocess" not in src

    @pytest.mark.asyncio
    async def test_local_and_proxy_evaluators_are_interchangeable(self):
        # A local policy and the (injected) proxy evaluator sit behind the same
        # PolicyEvaluator interface and drive the same allow/deny/audit path.
        class _DenyClient:
            def authorize(self, user_id, tool, action, resource):
                return {"decision": "deny", "reason": "not enrolled"}

        local = AuditedToolClient(evaluator=_deny_all())
        with pytest.raises(PermissionError):
            await local.Execute("t", {}, "U1", "C1", None, lambda **k: "x")

        proxied = AuditedToolClient(evaluator=KeiProxyEvaluator(_DenyClient()))
        with pytest.raises(PermissionError):
            await proxied.Execute("t", {}, "U1", "C1", None, lambda **k: "x")

        assert local.records()[0].decision.action == Action.DENY
        assert proxied.records()[0].decision.action == Action.DENY


class TestDelegationPreservesSubjectAtTheBoundary:
    """Invariant 5: delegation preserves the human subject and trace."""

    @pytest.mark.asyncio
    async def test_delegated_call_authorizes_the_human_and_audits_the_chain(self):
        seen: dict = {}

        class _Client:
            def authorize(self, user_id, tool, action, resource):
                seen["user_id"] = user_id
                return {"decision": "permit"}

        client = AuditedToolClient(evaluator=KeiProxyEvaluator(_Client()))
        human = CallerContext(user_id="U_HUMAN", invoking_subject="U_HUMAN", session_id="C1")
        sub = human.delegate(span="sub-1")
        sub.user_id = "U_AGENT"  # the subagent runs under its own service id

        await client.Execute(
            "github.read",
            {"owner": "a", "repo": "b"},
            "U_AGENT",
            "C1",
            None,
            lambda **k: "ok",
            caller=sub,
        )

        assert seen["user_id"] == "U_HUMAN", "authorization must resolve to the human"
        rec = client.records()[0]
        assert rec.invoking_subject == "U_HUMAN"
        assert rec.parent_span == "sub-1"
        assert rec.delegation_depth == 1


class TestActionToolsAndReadBindingsStaySeparate:
    """Invariant 6: action tools and read connector bindings stay separate."""

    def test_bindings_do_not_classify_action_vs_read(self):
        # The manifest binding carries no action/read/permission taxonomy. That
        # classification is policy's concern, not the binding's.
        assert set(ToolBinding.model_fields) == {"tool_name", "connector_id", "config"}
        for b in load_manifest(VALID_MANIFEST).tool_bindings:
            dumped = json.dumps(b.model_dump())
            for tag in ("action", "read_only", "permission", "grant"):
                assert tag not in dumped, (b.tool_name, tag)

    @pytest.mark.asyncio
    async def test_policy_separates_action_from_read_independently_of_bindings(self):
        # Both tools are bound in the manifest. Policy alone decides: the read
        # tool is allowed, the action (mutation) tool is denied.
        policy = Policy(
            rules=[
                Rule(name="deny-mutation", tools=["web_search"], action=Action.DENY),
                Rule(name="allow-read", tools=["file_read"], action=Action.ALLOW),
            ]
        )
        client = AuditedToolClient(evaluator=SimplePolicyEvaluator(policy))

        ok = await client.Execute(
            "file_read", {"path": "/tmp/x"}, "U1", "C1", None, lambda **k: "read"
        )
        assert ok == "read"

        with pytest.raises(PermissionError):
            await client.Execute("web_search", {"q": "x"}, "U1", "C1", None, lambda **k: "never")

        actions = {r.tool_name: r.decision.action for r in client.records()}
        assert actions["file_read"] == Action.ALLOW
        assert actions["web_search"] == Action.DENY
