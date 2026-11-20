"""Tests for the OperationRunner framework adapters (LangChain, Pydantic AI).

The adapters expose a generated operation as a framework tool: every tool
call goes through the runner (authorize, release, execute), the model-facing
name and description always come from the caller, and a policy deny surfaces
as a readable tool error whose message includes the reason code. The tests
use a fake runner only — no socket, no provider — and skip cleanly when the
optional framework packages are not installed.
"""

import os

import pytest

pytest.importorskip("langchain_core", reason="langchain-core is not installed")
pytest.importorskip("pydantic_ai", reason="pydantic-ai is not installed")

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from kei.connectors.github.v1 import issues_pb2, ops  # noqa: E402
from langchain_core.tools import ToolException  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402

from haikeilabs.agentware.middleware.types import CallerContext  # noqa: E402
from haikeilabs.agentware.operations import (  # noqa: E402
    ConnectRequiredError,
    EnrollmentRequiredError,
    OperationDeniedError,
)
from haikeilabs.agentware.operations.adapters import (  # noqa: E402
    langchain_tool,
    register_pydantic_ai_tool,
)

CALLER = CallerContext(trusted=False, parent_span="p", delegation_depth=1)


def caller_fn() -> CallerContext:
    return CALLER


def to_request(**kwargs: object) -> issues_pb2.GetIssueRequest:
    return issues_pb2.GetIssueRequest(**{k: v for k, v in kwargs.items() if v is not None})


def from_response(response: object) -> object:
    return response


class FakeRunner:
    """A runner stub with the OperationRunner.run shape; no socket involved."""

    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.response = response
        self.error = error

    async def run(
        self,
        caller: CallerContext,
        op: object,
        request: object,
        *,
        tool: str = "",
        resource_template_override: str | None = None,
    ) -> object:
        self.calls.append(
            {
                "caller": caller,
                "op": op,
                "request": request,
                "tool": tool,
                "resource_template_override": resource_template_override,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


async def test_langchain_tool_calls_runner_with_op() -> None:
    runner = FakeRunner(response={"number": 7})
    tool = langchain_tool(
        runner,
        caller_fn,
        ops.OP_GET_ISSUE,
        issues_pb2.GetIssueRequest,
        name="get_issue",
        description="Get one repository issue",
        to_request=to_request,
        from_response=from_response,
    )
    # The model-facing name and description come from the caller, verbatim.
    assert tool.name == "get_issue"
    assert tool.description == "Get one repository issue"
    out = await tool.ainvoke({"owner": "octocat", "repository": "hello-world"})
    assert out == {"number": 7}
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["op"].id == "github.issues.get"
    request = call["request"]
    assert request.owner == "octocat"
    assert request.repository == "hello-world"
    assert call["tool"] == "get_issue"


async def test_pydantic_tool_calls_runner_with_op() -> None:
    runner = FakeRunner(response={"number": 7})
    agent = Agent(TestModel(), tools=[])
    register_pydantic_ai_tool(
        agent,
        ops.OP_GET_ISSUE,
        issues_pb2.GetIssueRequest,
        name="get_issue",
        description="Get one repository issue",
        to_request=to_request,
        from_response=from_response,
        runner=runner,
        caller_fn=caller_fn,
    )
    result = await agent.run("list the issue")
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["op"].id == "github.issues.get"
    assert call["tool"] == "get_issue"
    assert "get_issue" in result.output


@pytest.mark.parametrize("adapter", ["langchain", "pydantic_ai"])
async def test_deny_is_readable_tool_error(adapter: str) -> None:
    error = OperationDeniedError("policy_denied")
    runner = FakeRunner(error=error)
    if adapter == "langchain":
        tool = langchain_tool(
            runner,
            caller_fn,
            ops.OP_GET_ISSUE,
            issues_pb2.GetIssueRequest,
            name="get_issue",
            description="Get one repository issue",
            to_request=to_request,
            from_response=from_response,
        )
        with pytest.raises(ToolException) as excinfo:
            await tool.ainvoke({"owner": "octocat", "repository": "hello-world"})
    else:
        agent = Agent(TestModel(), tools=[])
        register_pydantic_ai_tool(
            agent,
            ops.OP_GET_ISSUE,
            issues_pb2.GetIssueRequest,
            name="get_issue",
            description="Get one repository issue",
            to_request=to_request,
            from_response=from_response,
            runner=runner,
            caller_fn=caller_fn,
        )
        with pytest.raises(Exception) as excinfo:
            await agent.run("list the issue")
    assert "policy_denied" in str(excinfo.value)
    assert len(runner.calls) == 1


@pytest.mark.parametrize("adapter", ["langchain", "pydantic_ai"])
async def test_enrollment_and_connect_surface_as_tool_errors(adapter: str) -> None:
    runner = FakeRunner(error=EnrollmentRequiredError({"url": "https://connect.test"}))
    if adapter == "langchain":
        tool = langchain_tool(
            runner,
            caller_fn,
            ops.OP_GET_ISSUE,
            issues_pb2.GetIssueRequest,
            name="get_issue",
            description="Get one repository issue",
            to_request=to_request,
            from_response=from_response,
        )
        with pytest.raises(ToolException) as excinfo:
            await tool.ainvoke({"owner": "octocat", "repository": "hello-world"})
    else:
        agent = Agent(TestModel(), tools=[])
        register_pydantic_ai_tool(
            agent,
            ops.OP_GET_ISSUE,
            issues_pb2.GetIssueRequest,
            name="get_issue",
            description="Get one repository issue",
            to_request=to_request,
            from_response=from_response,
            runner=runner,
            caller_fn=caller_fn,
        )
        with pytest.raises(Exception) as excinfo:
            await agent.run("list the issue")
    assert "Enrollment required" in str(excinfo.value)
    runner2 = FakeRunner(error=ConnectRequiredError({"url": "https://connect.test"}))
    if adapter == "langchain":
        tool2 = langchain_tool(
            runner2,
            caller_fn,
            ops.OP_GET_ISSUE,
            issues_pb2.GetIssueRequest,
            name="get_issue",
            description="Get one repository issue",
            to_request=to_request,
            from_response=from_response,
        )
        with pytest.raises(ToolException) as excinfo2:
            await tool2.ainvoke({"owner": "octocat", "repository": "hello-world"})
    else:
        agent2 = Agent(TestModel(), tools=[])
        register_pydantic_ai_tool(
            agent2,
            ops.OP_GET_ISSUE,
            issues_pb2.GetIssueRequest,
            name="get_issue",
            description="Get one repository issue",
            to_request=to_request,
            from_response=from_response,
            runner=runner2,
            caller_fn=caller_fn,
        )
        with pytest.raises(Exception) as excinfo2:
            await agent2.run("list the issue")
    assert "Connection required" in str(excinfo2.value)
