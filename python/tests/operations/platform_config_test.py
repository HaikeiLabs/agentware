"""Tests for ``connector_config``: the runner's ConnectorConfig from the platform.

The harness names no connector ID: ``connector_config`` reads the agent's
bound connectors from kei-proxy (``GET /v1/agent-config``), joins them with the
harness's generated operation bindings by tool name, and takes the connection
kind and header from the contracts ``Connection`` the installation names. The
runner then releases the credential without a connector, and kei-proxy derives
it from the tool's binding. Mirrored in Go (``go/kei/operations``) and
TypeScript (``typescript/tests/operations``).
"""

from collections.abc import Mapping
from typing import Any

import httpx
import pytest
from kei.connectors.github.v1 import issues_pb2, ops, pulls_pb2

from haikeilabs.agentware.kei import KeiProxySocketError, KeiProxySocketSession
from haikeilabs.agentware.middleware.types import CallerContext
from haikeilabs.agentware.operations import (
    ConnectorsUnavailableError,
    OperationBinding,
    OperationDeniedError,
    OperationRunner,
    connector_config,
)

SECRET = "credential-DO-NOT-LEAK"
SESSION = KeiProxySocketSession("test", "user", "s")
CALLER = CallerContext(trusted=False, parent_span="p", delegation_depth=1)
TOOL = "github.get_issue"
TOOL_ARGS = {"owner": "acme", "repository": "repo", "issue_number": 2}


def agent_config(**connector: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "connector_id": "c-1",
        "name": "acme-github",
        "kind": "native",
        "connection": "pat",
        "base_url": "https://ghe.example",
        "tools": [
            {
                "tool": TOOL,
                "operations": ["get"],
                "binding_version": "v3",
                "resource_template": "repos/{owner}/{repository}",
            }
        ],
    }
    entry.update(connector)
    return {"schema": "kei.agent-config/v1", "connectors": [entry]}


class FakeAgentConfigSource:
    def __init__(self, result: Mapping[str, Any] | None = None, error: Exception | None = None):
        self.result = result if result is not None else agent_config()
        self.error = error
        self.calls = 0

    async def agent_config(self) -> Mapping[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def bindings() -> dict[str, dict[str, OperationBinding]]:
    return {
        TOOL: {ops.OP_GET_ISSUE.id: OperationBinding(issues_pb2.GetIssueRequest, ops.GithubQueries)}
    }


async def test_builds_the_entry_for_a_bound_tool_from_the_platform() -> None:
    config = await connector_config(FakeAgentConfigSource(), bindings())

    assert list(config) == [TOOL]
    entry = config[TOOL]
    # The platform resolves the connector from the tool's binding: no ID here.
    assert entry.connector_id == ""
    assert entry.connection_name == "pat"
    # kind comes from the contracts Connection named "pat" (HTTP bearer).
    assert entry.kind == "http_bearer"
    assert entry.header_name == ""
    assert entry.base_url == "https://ghe.example"
    binding = entry.operations[ops.OP_GET_ISSUE.id]
    assert binding.request_type is issues_pb2.GetIssueRequest
    assert binding.binding_version == "v3"


async def test_base_url_falls_back_to_the_contract_connector() -> None:
    source = FakeAgentConfigSource(agent_config())
    del source.result["connectors"][0]["base_url"]

    config = await connector_config(source, bindings())

    assert config[TOOL].base_url == "https://api.github.com"


@pytest.mark.parametrize(
    "change",
    [
        {"connection": ""},  # the descriptor does not carry the connection yet
        {"connection": "user_token"},  # not a connection of the github contract
        {"tools": []},  # the tool is not bound to this agent
        {"tools": [{"tool": "other.tool", "operations": []}]},
    ],
)
async def test_a_tool_the_platform_cannot_configure_is_left_out(change: dict[str, Any]) -> None:
    config = await connector_config(FakeAgentConfigSource(agent_config(**change)), bindings())

    assert config == {}


async def test_bindings_must_name_one_contract_connector_per_tool() -> None:
    mixed = bindings()
    mixed[TOOL]["slack.chat.post"] = OperationBinding(pulls_pb2.ListPullsRequest, ops.GithubQueries)

    with pytest.raises(ValueError):
        await connector_config(FakeAgentConfigSource(), mixed)


@pytest.mark.parametrize(
    "error",
    [
        KeiProxySocketError("proxy_error", "the daemon answered 503", "agent_config_unavailable"),
        KeiProxySocketError("proxy_error", "the daemon answered 404"),
        KeiProxySocketError("proxy_unavailable", "no daemon"),
        KeiProxySocketError("malformed_response", "bad body"),
    ],
)
async def test_an_unavailable_agent_config_fails_closed(error: KeiProxySocketError) -> None:
    with pytest.raises(ConnectorsUnavailableError) as excinfo:
        await connector_config(FakeAgentConfigSource(error=error), bindings())

    assert excinfo.value.reason_code == "connectors_unavailable"
    assert excinfo.value.reason_class == error.reason_class
    assert excinfo.value.code == error.code


async def test_a_malformed_agent_config_fails_closed() -> None:
    with pytest.raises(ConnectorsUnavailableError):
        await connector_config(FakeAgentConfigSource({"schema": "kei.agent-config/v1"}), bindings())


class RecordingSocket:
    def __init__(self) -> None:
        self.release_calls: list[dict[str, Any]] = []

    async def authorize(self, session: KeiProxySocketSession, **kwargs: Any) -> dict[str, Any]:
        return {"decision": "permit"}

    async def release_credential(
        self, session: KeiProxySocketSession, **kwargs: Any
    ) -> dict[str, Any]:
        self.release_calls.append(kwargs)
        return {"decision": "permit", "credential": {"access_token": SECRET}}


async def test_runner_releases_without_a_connector_for_a_platform_configured_tool() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        return httpx.Response(200, json={"number": 2, "title": "issue"})

    socket = RecordingSocket()
    config = await connector_config(FakeAgentConfigSource(), bindings())
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    runner = OperationRunner(socket, SESSION, config, http_client=http)

    result = await runner.run(
        CALLER,
        ops.OP_GET_ISSUE,
        issues_pb2.GetIssueRequest(owner="acme", repository="repo", issue_number=2),
        tool=TOOL,
        tool_args=TOOL_ARGS,
    )

    assert result.title == "issue"
    assert socket.release_calls[0]["connector_id"] == ""
    assert socket.release_calls[0]["capability"] == ""
    assert socket.release_calls[0]["tool"] == TOOL
    assert socket.release_calls[0]["tool_args"] == TOOL_ARGS
    assert str(seen[0].url).startswith("https://ghe.example/")


async def test_runner_without_a_connector_needs_the_tool_arguments() -> None:
    socket = RecordingSocket()
    config = await connector_config(FakeAgentConfigSource(), bindings())
    runner = OperationRunner(socket, SESSION, config)

    with pytest.raises(OperationDeniedError) as excinfo:
        await runner.run(
            CALLER,
            ops.OP_GET_ISSUE,
            issues_pb2.GetIssueRequest(owner="acme", repository="repo", issue_number=2),
            tool=TOOL,
        )

    assert excinfo.value.reason_code == "connector_not_named"
    assert socket.release_calls == []
