"""Contract tests against the pinned Forge package and actual Pydantic toolsets."""

import asyncio
import sys
from importlib.metadata import PackageNotFoundError, version

import pytest

if sys.version_info < (3, 12):
    pytest.skip("Forge 0.10.1 requires Python 3.12+", allow_module_level=True)
try:
    if version("forge-guardrails") != "0.10.1":
        pytest.skip("the pinned Forge extra is not installed", allow_module_level=True)
except PackageNotFoundError:
    pytest.skip("the pinned Forge extra is not installed", allow_module_level=True)

pytest.importorskip("forge.guardrails")
from haikeilabs.agentware.integrations.forge import ForgeSession


def test_response_rescue_and_missing_required_step():
    session = ForgeSession(["read", "done"], required_steps=["read"], terminal_tool="done")
    assert session.check_call("done", {}).action == "step_blocked"
    rescued = session.check_text('{"tool": "read", "args": {"path": "a"}}')
    assert rescued.action == "execute"
    assert rescued.calls == (("read", {"path": "a"}),)
    session.record_success("read", {"path": "a"})
    assert session.check_call("done", {}).action == "execute"


def test_prerequisites_are_per_workflow_and_reset_on_success():
    s = ForgeSession(
        ["read", "edit", "done"],
        terminal_tool="done",
        prerequisites={"edit": [{"tool": "read", "match_arg": "path"}]},
        max_prereq_violations=1,
    )
    assert s.check_call("edit", {"path": "a"}).action == "step_blocked"
    s.record_success("read", {"path": "a"})
    assert s.check_call("edit", {"path": "a"}).action == "execute"
    other = ForgeSession(
        ["read", "edit", "done"], terminal_tool="done", prerequisites={"edit": ["read"]}
    )
    assert other.check_call("edit", {}).action == "step_blocked"


def test_downstream_failures_and_denials_are_not_recorded_as_progress():
    async def case():
        s = ForgeSession(["read", "done"], required_steps=["read"], terminal_tool="done")

        async def deny():
            raise PermissionError("policy denied")

        with pytest.raises(PermissionError):
            await s.run_tool("read", {}, deny)
        assert s.check_call("done", {}).action == "step_blocked"

        async def unsuccessful():
            return {"success": False}

        await s.run_tool("read", {}, unsuccessful, succeeded=lambda value: value["success"])
        assert s.check_call("done", {}).action == "step_blocked"

    asyncio.run(case())


def test_pydantic_toolset_runs_the_actual_wrapped_tool_and_respects_rejection():
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent, FunctionToolset
    from pydantic_ai.models.test import TestModel

    from haikeilabs.agentware.integrations.pydantic_forge import ForgeToolset

    seen = []

    def read() -> str:
        seen.append("read")
        return "ok"

    session = ForgeSession(["read"], terminal_tool="")
    agent = Agent(
        TestModel(), toolsets=[ForgeToolset(FunctionToolset([read]), lambda ctx: session)]
    )
    agent.run_sync("Use the tool")
    assert seen == ["read"]


def test_concurrent_calls_cannot_overtake_prerequisite():
    async def case():
        session = ForgeSession(["read", "edit"], prerequisites={"edit": ["read"]})
        started = asyncio.Event()
        finish = asyncio.Event()

        async def read():
            started.set()
            await finish.wait()
            return True

        task = asyncio.create_task(session.run_tool("read", {}, read))
        await started.wait()

        async def edit():
            return "ok"

        pending = asyncio.create_task(session.run_tool("edit", {}, edit))
        await asyncio.sleep(0)
        assert not pending.done()
        finish.set()
        assert await task
        assert await pending == "ok"

    asyncio.run(case())


def test_adk_function_tool_preserves_schema_and_blocks_without_execution():
    pytest.importorskip("google.adk")
    from google.adk.tools import FunctionTool

    from haikeilabs.agentware.integrations.adk_forge import ForgeFunctionTool

    seen = []

    def read(path: str) -> str:
        """Read a path."""
        seen.append(path)
        return path

    async def case():
        wrapped = FunctionTool(read)
        session = ForgeSession(["read"], prerequisites={"read": ["other"]})
        tool = ForgeFunctionTool(wrapped, lambda ctx: session)
        assert tool._get_declaration() == wrapped._get_declaration()
        outcome = await tool.run_async(args={"path": "x"}, tool_context=None)
        assert "error" in outcome and seen == []

    asyncio.run(case())


def test_adk_validation_error_is_not_recorded_as_success():
    pytest.importorskip("google.adk")
    from google.adk.tools import FunctionTool

    from haikeilabs.agentware.integrations.adk_forge import ForgeFunctionTool

    def read(path: str) -> str:
        """Read a path."""
        return path

    async def case():
        session = ForgeSession(["read", "finish"], required_steps=["read"], terminal_tool="finish")
        tool = ForgeFunctionTool(FunctionTool(read), lambda ctx: session)
        outcome = await tool.run_async(args={}, tool_context=None)
        assert "error" in outcome
        assert session.check_call("finish", {}).action == "step_blocked"

    asyncio.run(case())


def test_adk_function_tool_executes_and_records_real_tool():
    pytest.importorskip("google.adk")
    from unittest.mock import MagicMock

    from google.adk.tools import FunctionTool

    from haikeilabs.agentware.integrations.adk_forge import ForgeFunctionTool

    def read(path: str) -> str:
        """Read a path."""
        return path

    async def case():
        session = ForgeSession(["read", "finish"], required_steps=["read"], terminal_tool="finish")
        tool = ForgeFunctionTool(FunctionTool(read), lambda ctx: session)
        context = MagicMock()
        assert await tool.run_async(args={"path": "x"}, tool_context=context) == "x"
        assert session.check_call("finish", {}).action == "execute"

    asyncio.run(case())


def test_forge_context_manager_is_explicit_and_preserves_headers():
    from forge.core.messages import Message, MessageMeta, MessageRole, MessageType

    from haikeilabs.agentware.integrations.forge import make_context_manager

    manager = make_context_manager(200, keep_recent=1)
    messages = [
        Message(MessageRole.SYSTEM, "system", MessageMeta(MessageType.SYSTEM_PROMPT)),
        Message(MessageRole.USER, "task", MessageMeta(MessageType.USER_INPUT)),
        Message(
            MessageRole.TOOL,
            "x" * 4000,
            MessageMeta(MessageType.TOOL_RESULT, step_index=1),
            tool_call_id="old",
        ),
        Message(
            MessageRole.TOOL,
            "recent",
            MessageMeta(MessageType.TOOL_RESULT, step_index=2),
            tool_call_id="recent",
        ),
    ]
    result = manager.maybe_compact(messages)
    assert result[0].content == "system" and result[1].content == "task"
    assert result[-1].content == "recent"
    assert len(result) < len(messages) or len(result[2].content) < len(messages[2].content)


def test_compaction_rejects_broken_tool_pairing():
    from forge.core.messages import Message, MessageMeta, MessageRole, MessageType, ToolCallInfo

    from haikeilabs.agentware.integrations.forge import compact_forge_messages, make_context_manager

    manager = make_context_manager(200, keep_recent=1)
    history = [
        Message(MessageRole.SYSTEM, "system", MessageMeta(MessageType.SYSTEM_PROMPT)),
        Message(MessageRole.USER, "task", MessageMeta(MessageType.USER_INPUT)),
        Message(
            MessageRole.ASSISTANT,
            "",
            MessageMeta(MessageType.TOOL_CALL, step_index=1),
            tool_calls=[ToolCallInfo("read", {}, "call-1")],
        ),
        Message(
            MessageRole.TOOL,
            "x" * 4000,
            MessageMeta(MessageType.TOOL_RESULT, step_index=1),
            tool_call_id="call-1",
        ),
        Message(
            MessageRole.ASSISTANT, "recent", MessageMeta(MessageType.TEXT_RESPONSE, step_index=2)
        ),
    ]
    with pytest.raises(ValueError, match="invalid tool-call history"):
        compact_forge_messages(manager, history)


def test_verified_observations_drive_progress_only_after_success():
    from haikeilabs.agentware.middleware.guardrails import ProgressTracker

    async def case():
        tracker = ProgressTracker(["verified"], max_no_progress=3)
        session = ForgeSession(
            ["read", "finish"],
            terminal_tool="finish",
            progress=tracker,
            observe_result=lambda name, args, result: [("verified", result == "verified")],
        )
        assert session.finish_nudge() is None

        async def rejected():
            raise PermissionError("denied")

        with pytest.raises(PermissionError):
            await session.run_tool("read", {}, rejected)
        assert tracker.snapshot().no_progress == 1 and not tracker.snapshot().ready

        async def verified():
            return "verified"

        assert await session.run_tool("read", {}, verified) == "verified"
        assert tracker.snapshot().ready and tracker.snapshot().no_progress == 0
        assert "finish" in session.finish_nudge().nudge

    asyncio.run(case())


def test_pydantic_quality_rejection_uses_native_retry():
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import FunctionToolset, ModelRetry

    from haikeilabs.agentware.integrations.pydantic_forge import ForgeToolset

    def write(path: str) -> str:
        return path

    async def case():
        session = ForgeSession(["read", "write"], prerequisites={"write": ["read"]})
        toolset = ForgeToolset(FunctionToolset([write]), lambda ctx: session)
        with pytest.raises(ModelRetry):
            await toolset.call_tool("write", {"path": "x"}, None, None)

    asyncio.run(case())


def test_rejects_an_unpinned_forge_install(monkeypatch):
    from haikeilabs.agentware.integrations import forge as integration

    monkeypatch.setattr(integration, "version", lambda name: "0.8.1")
    with pytest.raises(ImportError, match="forge-guardrails==0.10.1"):
        integration.ForgeSession(["read"])
    with pytest.raises(ImportError):
        integration.make_context_manager(1024)


def test_argument_matching_uses_post_policy_arguments():
    async def case():
        rules = {"write": [{"tool": "read", "match_arg": "path"}]}
        no_evidence = ForgeSession(["read", "write"], prerequisites=rules)

        async def actual():
            return {"executed_path": "filtered"}

        await no_evidence.run_tool("read", {"path": "requested"}, actual)
        assert no_evidence.check_call("write", {"path": "requested"}).action == "step_blocked"
        assert no_evidence.check_call("write", {}).action == "step_blocked"
        session = ForgeSession(
            ["read", "write"],
            prerequisites=rules,
            executed_args=lambda name, requested, result: {"path": result["executed_path"]},
        )
        await session.run_tool("read", {"path": "requested"}, actual)
        assert session.check_call("write", {"path": "requested"}).action == "step_blocked"
        assert session.check_call("write", {"path": "filtered"}).action == "execute"

    asyncio.run(case())


def test_finish_nudge_never_overrides_exhausted_progress_budget():
    from haikeilabs.agentware.middleware.guardrails import ProgressTracker

    progress = ProgressTracker(["verified"], max_no_progress=0)
    progress.observe("verified")
    session = ForgeSession(["done"], terminal_tool="done", progress=progress)
    assert session.finish_nudge() is not None
    progress.failure()
    assert session.finish_nudge() is None
    assert session.check_call("done", {}).action == "fatal"


def test_progress_callback_never_observes_unverified_proposed_arguments():
    from haikeilabs.agentware.middleware.guardrails import ProgressTracker

    async def case():
        seen = []

        async def execute():
            return {"path": "filtered"}

        def observe(name, args, result):
            seen.append(args)
            return []

        first = ForgeSession(["read"], progress=ProgressTracker(["fact"]), observe_result=observe)
        await first.run_tool("read", {"path": "requested"}, execute)
        assert seen == [{}]
        second = ForgeSession(
            ["read"],
            progress=ProgressTracker(["fact"]),
            observe_result=observe,
            executed_args=lambda name, proposed, result: {"path": result["path"]},
        )
        await second.run_tool("read", {"path": "requested"}, execute)
        assert seen[-1] == {"path": "filtered"}

    asyncio.run(case())


def test_duplicate_observations_use_no_progress_budget():
    from haikeilabs.agentware.middleware.guardrails import ProgressTracker

    async def case():
        tracker = ProgressTracker(["read", "done"], max_no_progress=1)
        session = ForgeSession(
            ["read", "done"],
            progress=tracker,
            observe_result=lambda name, args, result: [(name, True)],
        )

        async def execute():
            return "ok"

        await session.run_tool("read", {}, execute)
        assert tracker.snapshot().no_progress == 0
        await session.run_tool("read", {}, execute)
        assert tracker.snapshot().no_progress == 1
        await session.run_tool("read", {}, execute)
        assert tracker.snapshot().exhausted
        assert session.check_call("done", {}).action == "fatal"

    asyncio.run(case())
