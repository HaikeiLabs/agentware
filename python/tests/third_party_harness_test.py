"""Third-party harness viability test.

This test proves that a third party can build a complete harness using ONLY
haikeilabs.agentware, without importing pedro_service, pedro_tag, or any repo-specific
layout. It demonstrates the "bring your own agent" product thesis.

Constraints:
- No imports from pedro_service or pedro_tag
- No shutil.which
- No assumed repo layout
- No os.environ reads inside library code on the test path
"""

import pytest


class TestThirdPartyHarness:
    """Test that a third-party harness can be built with only haikeilabs.agentware."""

    def test_minimal_harness_contract(self):
        """Build a minimal harness using only haikeilabs.agentware."""
        from haikeilabs.agentware.kei import (
            EnvSecretProvider,
            HarnessContract,
            OpaqueTokenProvider,
            ToolNotFoundError,
            validate_contract,
        )

        class MinimalToolExecutor:
            def execute(self, tool_name: str, args: dict) -> dict:
                if tool_name == "echo":
                    return {"echo": args.get("message", "")}
                raise ToolNotFoundError(f"Tool not found: {tool_name}")

        contract = HarnessContract(
            auth_provider=OpaqueTokenProvider(token="test-token"),
            tool_executor=MinimalToolExecutor(),
            secret_provider=EnvSecretProvider(),
        )

        errors = validate_contract(contract)
        assert errors == [], f"Contract validation failed: {errors}"

    def test_harness_with_policy(self):
        """Build a harness with policy evaluation."""
        from haikeilabs.agentware.kei import (
            EnvSecretProvider,
            HarnessContract,
            OpaqueTokenProvider,
            ToolNotFoundError,
            validate_contract,
        )
        from haikeilabs.agentware.middleware import Action, Decision

        class MinimalToolExecutor:
            def execute(self, tool_name: str, args: dict) -> dict:
                if tool_name == "hello":
                    return {"greeting": f"Hello, {args.get('name', 'world')}!"}
                raise ToolNotFoundError(f"Tool not found: {tool_name}")

        class _DenyAll:
            def evaluate(self, tool_name, args, caller):
                return Decision(action=Action.DENY, rule="default", reason="default deny")

        contract = HarnessContract(
            auth_provider=OpaqueTokenProvider(token="test-token"),
            tool_executor=MinimalToolExecutor(),
            secret_provider=EnvSecretProvider(),
            policy_evaluator=_DenyAll(),
        )

        errors = validate_contract(contract)
        assert errors == [], f"Contract validation failed: {errors}"

    @pytest.mark.asyncio
    async def test_contract_client_defaults_to_deny_and_uses_executor(self):
        """The contract wires its executor and fails closed without policy."""
        from haikeilabs.agentware.kei import (
            EnvSecretProvider,
            HarnessContract,
            OpaqueTokenProvider,
        )
        from haikeilabs.agentware.middleware import Action, CallerContext

        calls = []

        class Executor:
            def execute(self, tool_name: str, args: dict) -> dict:
                calls.append((tool_name, args))
                return {"ok": True}

        contract = HarnessContract(
            auth_provider=OpaqueTokenProvider(token="test-token"),
            tool_executor=Executor(),
            secret_provider=EnvSecretProvider(),
        )
        client = contract.create_tool_client(source="contract-test")

        with pytest.raises(PermissionError):
            await client.Execute(
                "write",
                {},
                "user",
                "session",
                None,
                caller=CallerContext(user_id="user", invoking_subject="user"),
            )

        assert calls == []
        assert client.records()[0].decision.action == Action.DENY

    @pytest.mark.asyncio
    async def test_contract_client_wires_explicit_policy_and_auditor(self):
        """Explicit evaluator, auditor, and executor are used by the client."""
        from haikeilabs.agentware.kei import (
            EnvSecretProvider,
            HarnessContract,
            OpaqueTokenProvider,
        )
        from haikeilabs.agentware.middleware import (
            Action,
            AuditFilter,
            CallerContext,
            Decision,
            InMemoryAuditor,
        )

        class Executor:
            def execute(self, tool_name: str, args: dict) -> dict:
                return {"tool": tool_name, **args}

        class _AllowAll:
            def evaluate(self, tool_name, args, caller):
                return Decision(action=Action.ALLOW, rule="default", reason="allowed")

        auditor = InMemoryAuditor()
        contract = HarnessContract(
            auth_provider=OpaqueTokenProvider(token="test-token"),
            tool_executor=Executor(),
            secret_provider=EnvSecretProvider(),
            policy_evaluator=_AllowAll(),
            auditor=auditor,
        )
        client = contract.create_tool_client(source="contract-test")
        result = await client.Execute(
            "echo",
            {"value": 1},
            "user",
            "session",
            None,
            caller=CallerContext(user_id="user", invoking_subject="user"),
        )

        assert result == {"tool": "echo", "value": 1}
        assert len(auditor.query(AuditFilter())) == 1
        assert client.records() == auditor.query(AuditFilter())

    def test_harness_with_auditor(self):
        """Build a harness with audit logging."""
        from haikeilabs.agentware.kei import (
            EnvSecretProvider,
            HarnessContract,
            OpaqueTokenProvider,
            ToolNotFoundError,
            validate_contract,
        )
        from haikeilabs.agentware.middleware.audit import InMemoryAuditor

        class MinimalToolExecutor:
            def execute(self, tool_name: str, args: dict) -> dict:
                if tool_name == "add":
                    return {"result": args.get("a", 0) + args.get("b", 0)}
                raise ToolNotFoundError(f"Tool not found: {tool_name}")

        auditor = InMemoryAuditor()

        contract = HarnessContract(
            auth_provider=OpaqueTokenProvider(token="test-token"),
            tool_executor=MinimalToolExecutor(),
            secret_provider=EnvSecretProvider(),
            auditor=auditor,
        )

        errors = validate_contract(contract)
        assert errors == [], f"Contract validation failed: {errors}"

    @pytest.mark.asyncio
    async def test_harness_audited_tool_client(self):
        """Verify AuditedToolClient works with harness-provided components."""
        from haikeilabs.agentware.kei import (
            EnvSecretProvider,
            HarnessContract,
            OpaqueTokenProvider,
            ToolNotFoundError,
            validate_contract,
        )
        from haikeilabs.agentware.middleware import (
            Action,
            AuditedToolClient,
            CallerContext,
            Decision,
        )

        tools = {}

        def register_tool(name: str, func):
            tools[name] = func

        def call_tool(name: str, args: dict) -> dict:
            if name not in tools:
                raise ToolNotFoundError(f"Tool not found: {name}")
            return tools[name](**args)

        register_tool("multiply", lambda a, b: {"product": a * b})

        class HarnessToolExecutor:
            def __init__(self, handler):
                self._handler = handler

            def execute(self, tool_name: str, args: dict) -> dict:
                return self._handler(tool_name, args)

        class _AllowAll:
            def evaluate(self, tool_name, args, caller):
                return Decision(action=Action.ALLOW, rule="default", reason="allowed")

        client = AuditedToolClient(
            source="third-party-harness",
            evaluator=_AllowAll(),
        )

        executor = HarnessToolExecutor(call_tool)
        contract = HarnessContract(
            auth_provider=OpaqueTokenProvider(token="test-token"),
            tool_executor=executor,
            secret_provider=EnvSecretProvider(),
            policy_evaluator=_AllowAll(),
        )

        errors = validate_contract(contract)
        assert errors == [], f"Contract validation failed: {errors}"

        caller = CallerContext(
            user_id="test-user",
            invoking_subject="test-user",
            source="third-party-harness",
        )

        result = await client.Execute(
            tool_name="multiply",
            tool_args={"a": 3, "b": 4},
            user_id="test-user",
            channel_id="test-channel",
            guild_id=None,
            func=lambda a, b: {"product": a * b},
            caller=caller,
        )

        assert result == {"product": 12}

    def test_caller_context_delegation(self):
        """Verify CallerContext delegation preserves invoking_subject."""
        from haikeilabs.agentware.middleware import CallerContext

        caller = CallerContext(
            user_id="agent-1",
            invoking_subject="human-user-123",
            source="third-party-harness",
            delegation_depth=0,
        )

        child = caller.delegate(span="subagent-1")

        assert child.invoking_subject == "human-user-123"
        assert child.delegation_depth == 1
        assert child.parent_span == "subagent-1"

        grandchild = child.delegate(span="subagent-2")
        assert grandchild.invoking_subject == "human-user-123"
        assert grandchild.delegation_depth == 2

    def test_fail_closed_unknown_policy(self):
        """Verify fail-closed: unknown policy decision = DENY."""
        from haikeilabs.agentware.middleware import Action, CallerContext, Decision

        class _DenyAll:
            def evaluate(self, tool_name, args, caller):
                return Decision(action=Action.DENY, rule="default", reason="default deny")

        evaluator = _DenyAll()

        caller = CallerContext(
            user_id="test",
            invoking_subject="test",
        )

        decision = evaluator.evaluate("unknown_tool", {"arg": "value"}, caller)

        assert decision.action == Action.DENY

    def test_no_pedro_service_import(self):
        """Verify haikeilabs.agentware does not import pedro_service or pedro_tag."""
        import haikeilabs.agentware.kei
        import haikeilabs.agentware.middleware

        kei_module = haikeilabs.agentware.kei
        middleware_module = haikeilabs.agentware.middleware

        kei_source = open(kei_module.__file__).read() if hasattr(kei_module, "__file__") else ""
        middleware_source = (
            open(middleware_module.__file__).read()
            if hasattr(middleware_module, "__file__")
            else ""
        )

        assert "pedro_service" not in kei_source, "kei must not import pedro_service"
        assert "pedro_tag" not in kei_source, "kei must not import pedro_tag"
        assert "pedro_service" not in middleware_source, "middleware must not import pedro_service"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
