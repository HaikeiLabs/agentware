"""Unit tests for the Python OperationRunner.

Mirrors ``typescript/tests/operations/runner.test.ts`` (agentware#325): the
runner authorizes ``connector:<op.id>`` over the socket, releases the
credential, builds the connection only after both succeed, calls the
generated operation, and never lets the credential reach a log, an error or
an audit record.
"""

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import pytest
from kei.connection import HttpConnection
from kei.connectors.github.v1 import issues_pb2, ops

from haikeilabs.agentware.kei import KeiProxySocketSession
from haikeilabs.agentware.middleware.types import CallerContext
from haikeilabs.agentware.operations import (
    ConnectorConfig,
    ConnectorEntry,
    ConnectRequiredError,
    EnrollmentRequiredError,
    OperationAudit,
    OperationBinding,
    OperationDeniedError,
    OperationRunner,
    UnsupportedConnectionError,
)

SECRET = "credential-DO-NOT-LEAK"
SESSION = KeiProxySocketSession("test", "user", "s")
CALLER = CallerContext(trusted=False, parent_span="p", delegation_depth=1)
BASE_URL = "http://github.test"
TOOL_ARGS = {"owner": "acme", "repository": "repo", "issue_number": 2}
TOOL_CONTEXT = {"channel": "c1"}


def make_request() -> issues_pb2.GetIssueRequest:
    return issues_pb2.GetIssueRequest(owner="acme", repository="repo", issue_number=2)


class FakeSocketClient:
    """Records the authorize and release requests the runner makes."""

    def __init__(
        self,
        *,
        authorize_result: dict[str, Any] | None = None,
        release_result: dict[str, Any] | None = None,
        authorize_error: Exception | None = None,
        release_error: Exception | None = None,
    ) -> None:
        self.authorize_calls: list[dict[str, Any]] = []
        self.release_calls: list[dict[str, Any]] = []
        self.authorize_result = (
            authorize_result if authorize_result is not None else {"decision": "allow"}
        )
        self.release_result = (
            release_result
            if release_result is not None
            else {"decision": "permit", "credential": {"access_token": SECRET}}
        )
        self.authorize_error = authorize_error
        self.release_error = release_error

    async def authorize(
        self,
        session: KeiProxySocketSession,
        *,
        tool: str = "",
        action: str = "",
        resource: str = "",
        parent_span: str = "",
        delegation_depth: int = 0,
        binding_version: str = "",
        tool_args: Mapping[str, Any] | None = None,
        tool_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.authorize_calls.append(
            {
                "tool": tool,
                "action": action,
                "resource": resource,
                "parent_span": parent_span,
                "delegation_depth": delegation_depth,
                "binding_version": binding_version,
                "tool_args": tool_args,
                "tool_context": tool_context,
            }
        )
        if self.authorize_error is not None:
            raise self.authorize_error
        return self.authorize_result

    async def release_credential(
        self,
        session: KeiProxySocketSession,
        *,
        connector_id: str = "",
        capability: str = "",
        resource: str = "",
        tool: str = "",
        parent_span: str = "",
        delegation_depth: int = 0,
        tool_args: Mapping[str, Any] | None = None,
        tool_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.release_calls.append(
            {
                "connector_id": connector_id,
                "capability": capability,
                "resource": resource,
                "tool": tool,
                "parent_span": parent_span,
                "delegation_depth": delegation_depth,
                "tool_args": tool_args,
                "tool_context": tool_context,
            }
        )
        if self.release_error is not None:
            raise self.release_error
        return self.release_result


class Provider:
    """A fake GitHub server: asserts the released credential is on the wire."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        if self.status == 200:
            return httpx.Response(200, json={"number": 2, "title": "issue"})
        return httpx.Response(self.status, text=SECRET)


def make_connectors(queries: Callable[[HttpConnection], Any], **overrides: Any) -> ConnectorConfig:
    entry = ConnectorEntry(
        connector_id="installed-github",
        connection_name="pat",
        kind="http_bearer",
        base_url=BASE_URL,
        operations={ops.OP_GET_ISSUE.id: OperationBinding(issues_pb2.GetIssueRequest, queries)},
    )
    for key, value in overrides.items():
        setattr(entry, key, value)
    return {"github": entry}


def make_queries_spy() -> tuple[Callable[[HttpConnection], Any], list[HttpConnection]]:
    calls: list[HttpConnection] = []

    def build(conn: HttpConnection) -> Any:
        calls.append(conn)
        return ops.GithubQueries(conn)

    return build, calls


def make_issue_comment_request() -> issues_pb2.CreateIssueCommentRequest:
    return issues_pb2.CreateIssueCommentRequest(
        owner="acme", repository="repo", issue_number=2, body="hello"
    )


def make_runner(
    client: FakeSocketClient,
    connectors: ConnectorConfig,
    provider: Provider,
    audit_calls: list[OperationAudit] | None = None,
) -> OperationRunner:
    http = httpx.AsyncClient(transport=httpx.MockTransport(provider.handler), base_url=BASE_URL)
    return OperationRunner(
        client,
        SESSION,
        connectors,
        audit=audit_calls.append if audit_calls is not None else None,
        http_client=http,
    )


async def test_allow_then_call() -> None:
    queries, built = make_queries_spy()
    provider = Provider()
    socket = FakeSocketClient()
    audit: list[OperationAudit] = []
    runner = make_runner(socket, make_connectors(queries), provider, audit)
    result = await runner.run(
        CALLER,
        ops.OP_GET_ISSUE,
        make_request(),
        tool="issue",
        tool_args=TOOL_ARGS,
        tool_context=TOOL_CONTEXT,
    )

    assert result.title == "issue"
    # No action: the runtime derives the operation from the registered plan.
    assert socket.authorize_calls == [
        {
            "tool": "issue",
            "action": "",
            "resource": "repos/acme/repo",
            "parent_span": "p",
            "delegation_depth": 1,
            "binding_version": "",
            "tool_args": TOOL_ARGS,
            "tool_context": TOOL_CONTEXT,
        }
    ]
    # The release capability is the bare operation id (the runtime's
    # capability pattern rejects ':'), with the same tool arguments.
    assert socket.release_calls == [
        {
            "connector_id": "installed-github",
            "capability": "github.issues.get",
            "resource": "repos/acme/repo",
            "tool": "issue",
            "parent_span": "p",
            "delegation_depth": 1,
            "tool_args": TOOL_ARGS,
            "tool_context": TOOL_CONTEXT,
        }
    ]
    assert provider.requests == 1
    assert len(built) == 1
    assert audit == [OperationAudit("github.issues.get", "allow", "success")]


async def test_authorizes_the_resource_from_the_generated_op_constant() -> None:
    # The generated Op constant carries its own resource_template. It differs
    # from the descriptor's ("repos/{owner}/{repository}"), so a runner that
    # still read the descriptor would authorize "repos/acme/widgets" instead
    # of the Op constant's "github:repo/acme/widgets".
    queries, _ = make_queries_spy()
    provider = Provider()
    socket = FakeSocketClient()
    runner = make_runner(socket, make_connectors(queries), provider)
    op = ops.OP_GET_ISSUE._replace(resource_template="github:repo/{owner}/{repository}")

    await runner.run(
        CALLER, op, issues_pb2.GetIssueRequest(owner="acme", repository="widgets", issue_number=2)
    )

    assert socket.authorize_calls[0]["resource"] == "github:repo/acme/widgets"


async def test_descriptor_option_is_the_fallback_for_empty_op_fields() -> None:
    # Contracts generated before the Op constants carried metadata leave the
    # fields empty; the request descriptor's operation option remains the
    # fallback in that case only.
    queries, _ = make_queries_spy()
    provider = Provider()
    socket = FakeSocketClient()
    runner = make_runner(socket, make_connectors(queries), provider)
    op = ops.OP_GET_ISSUE._replace(resource_template="", method_name="")

    await runner.run(CALLER, op, make_request())

    assert socket.authorize_calls[0]["resource"] == "repos/acme/repo"


@pytest.mark.parametrize("version", ["", "v0.20.0"])
async def test_sends_the_configured_binding_version(version: str) -> None:
    queries, _ = make_queries_spy()
    provider = Provider()
    socket = FakeSocketClient()
    entry = ConnectorEntry(
        connector_id="installed-github",
        connection_name="pat",
        kind="http_bearer",
        base_url=BASE_URL,
        operations={
            ops.OP_GET_ISSUE.id: OperationBinding(
                issues_pb2.GetIssueRequest, queries, binding_version=version
            )
        },
    )
    runner = make_runner(socket, {"github": entry}, provider)

    await runner.run(CALLER, ops.OP_GET_ISSUE, make_request())

    assert socket.authorize_calls[0]["binding_version"] == version


async def test_binding_stale_denied_before_release_and_provider() -> None:
    # kei-proxy denies with reason_code=binding_stale when the authorize
    # request names a binding version it does not serve. The runner passes
    # the reason through untouched and never releases or executes.
    queries, built = make_queries_spy()
    provider = Provider()
    socket = FakeSocketClient(authorize_result={"decision": "deny", "reason_code": "binding_stale"})
    runner = make_runner(socket, make_connectors(queries), provider)

    with pytest.raises(OperationDeniedError) as excinfo:
        await runner.run(CALLER, ops.OP_GET_ISSUE, make_request())

    assert excinfo.value.reason_code == "binding_stale"
    assert socket.release_calls == []
    assert built == []
    assert provider.requests == 0


async def test_wildcard_granted_read_works_and_write_deny_reason_untouched() -> None:
    # A read-only .* wildcard grant allows the read end to end; the same
    # policy set denies the write and the runner surfaces kei-proxy's deny
    # reason code unchanged.
    queries, _ = make_queries_spy()
    provider = Provider()
    entry = ConnectorEntry(
        connector_id="installed-github",
        connection_name="pat",
        kind="http_bearer",
        base_url=BASE_URL,
        operations={
            ops.OP_GET_ISSUE.id: OperationBinding(issues_pb2.GetIssueRequest, queries),
            ops.OP_CREATE_ISSUE_COMMENT.id: OperationBinding(
                issues_pb2.CreateIssueCommentRequest, queries
            ),
        },
    )
    read_socket = FakeSocketClient(authorize_result={"decision": "allow"})
    read_runner = make_runner(read_socket, {"github": entry}, provider)

    issue = await read_runner.run(CALLER, ops.OP_GET_ISSUE, make_request())

    assert issue.title == "issue"
    assert provider.requests == 1

    write_socket = FakeSocketClient(
        authorize_result={"decision": "deny", "reason_code": "write_not_granted"}
    )
    write_runner = make_runner(write_socket, {"github": entry}, provider)

    with pytest.raises(OperationDeniedError) as excinfo:
        await write_runner.run(CALLER, ops.OP_CREATE_ISSUE_COMMENT, make_issue_comment_request())

    assert excinfo.value.reason_code == "write_not_granted"
    assert write_socket.release_calls == []
    assert provider.requests == 1


async def test_deny_builds_no_connection() -> None:
    queries, built = make_queries_spy()
    provider = Provider()
    socket = FakeSocketClient(
        authorize_result={"decision": "deny", "reason_code": "connector_not_named"}
    )
    runner = make_runner(socket, make_connectors(queries), provider)

    with pytest.raises(OperationDeniedError) as excinfo:
        await runner.run(CALLER, ops.OP_GET_ISSUE, make_request())

    assert excinfo.value.reason_code == "connector_not_named"
    assert socket.release_calls == []
    assert built == []
    assert provider.requests == 0


@pytest.mark.parametrize(
    ("decision", "extra", "error_type"),
    [
        ("enrollment_required", {"enrollment": {"url": "claim"}}, EnrollmentRequiredError),
        ("connect_required", {"connect": {"url": "connect"}}, ConnectRequiredError),
    ],
)
async def test_enrollment_and_connect_returned(
    decision: str, extra: dict[str, Any], error_type: type[Exception]
) -> None:
    queries, built = make_queries_spy()
    provider = Provider()
    socket = FakeSocketClient(authorize_result={"decision": decision, **extra})
    runner = make_runner(socket, make_connectors(queries), provider)

    with pytest.raises(error_type) as excinfo:
        await runner.run(CALLER, ops.OP_GET_ISSUE, make_request())

    if error_type is EnrollmentRequiredError:
        assert excinfo.value.enrollment == {"url": "claim"}
    else:
        assert excinfo.value.connect == {"url": "connect"}
    assert socket.release_calls == []
    assert built == []
    assert provider.requests == 0


@pytest.mark.parametrize(
    "failure",
    ["deny", "timeout", "unavailable"],
)
async def test_release_failure_timeout_unavailable_deny(failure: str) -> None:
    queries, built = make_queries_spy()
    provider = Provider()
    if failure == "deny":
        socket = FakeSocketClient(
            release_result={"decision": "deny", "reason_code": "no_credential"}
        )
    else:
        socket = FakeSocketClient(release_error=Exception(f"{failure} {SECRET}"))
    runner = make_runner(socket, make_connectors(queries), provider)

    with pytest.raises(OperationDeniedError):
        await runner.run(CALLER, ops.OP_GET_ISSUE, make_request())

    assert built == []
    assert provider.requests == 0


@pytest.mark.parametrize("status", [401, 500])
async def test_runner_never_logs_credential(status: int, caplog: pytest.LogCaptureFixture) -> None:
    queries, _ = make_queries_spy()
    provider = Provider(status=status)
    socket = FakeSocketClient()
    audit: list[OperationAudit] = []
    runner = make_runner(socket, make_connectors(queries), provider, audit)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as excinfo:
            await runner.run(CALLER, ops.OP_GET_ISSUE, make_request())

    leaked = json.dumps([str(excinfo.value), repr(excinfo.value), repr(audit), caplog.text])
    assert SECRET not in leaked
    assert provider.requests == 1


async def test_connector_not_named_surfaces_reason() -> None:
    queries, built = make_queries_spy()
    provider = Provider()
    socket = FakeSocketClient()
    config = make_connectors(queries)
    del config["github"].operations[ops.OP_GET_ISSUE.id]
    runner = make_runner(socket, config, provider)

    with pytest.raises(OperationDeniedError) as excinfo:
        await runner.run(CALLER, ops.OP_GET_ISSUE, make_request())

    assert excinfo.value.reason_code == "connector_not_named"
    assert socket.authorize_calls == []
    assert socket.release_calls == []
    assert built == []
    assert provider.requests == 0


async def test_resource_template_override() -> None:
    queries, _ = make_queries_spy()
    provider = Provider()
    socket = FakeSocketClient()
    runner = make_runner(socket, make_connectors(queries), provider)

    await runner.run(
        CALLER, ops.OP_GET_ISSUE, make_request(), resource_template_override="orgs/{owner}"
    )

    assert socket.authorize_calls[0]["resource"] == "orgs/acme"


@pytest.mark.parametrize("kind", ["timeout", "unavailable"])
async def test_authorization_fails_closed(kind: str) -> None:
    queries, built = make_queries_spy()
    provider = Provider()
    socket = FakeSocketClient(authorize_error=Exception(kind))
    runner = make_runner(socket, make_connectors(queries), provider)

    with pytest.raises(OperationDeniedError):
        await runner.run(CALLER, ops.OP_GET_ISSUE, make_request())

    assert socket.release_calls == []
    assert built == []
    assert provider.requests == 0


async def test_unsupported_sql_connection_rejected_before_release() -> None:
    queries, built = make_queries_spy()
    provider = Provider()
    socket = FakeSocketClient()
    config = make_connectors(queries, kind="sql_dsn")
    runner = make_runner(socket, config, provider)

    with pytest.raises(UnsupportedConnectionError):
        await runner.run(CALLER, ops.OP_GET_ISSUE, make_request())

    assert socket.authorize_calls == []
    assert socket.release_calls == []
    assert built == []
    assert provider.requests == 0


class WireDaemon:
    """A daemon behind the real socket client: records every JSON body sent."""

    def __init__(self) -> None:
        self.bodies: dict[str, dict[str, Any]] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies[request.url.path] = body
        if request.url.path == "/v1/authorize":
            return httpx.Response(
                200, json={"schema": "kei.authorize-response/v1", "decision": "permit"}
            )
        return httpx.Response(
            200,
            json={
                "schema": "kei.credential-release-response/v1",
                "decision": "permit",
                "credential": {"access_token": SECRET},
            },
        )


async def test_wire_bodies_carry_bare_op_id_and_tool_arguments() -> None:
    # The real socket client, so the assertion is on what reaches kei-proxy:
    # kei-connector-runtime proxy-v0.11.0 decides a connector_binding tool only
    # from tool_args (decideV2ConnectorPlan) and releases only for a capability
    # matching ^[a-z0-9_]+(\.[a-z0-9_]+)*$ that the tool's plan requires.
    from haikeilabs.agentware.kei import KeiProxySocketAuthorizeClient

    daemon = WireDaemon()
    client = KeiProxySocketAuthorizeClient(
        env={"KEI_RUNTIME_TOKEN": "kh_test"}, transport=httpx.MockTransport(daemon.handle)
    )
    queries, _ = make_queries_spy()
    runner = make_runner(client, make_connectors(queries), Provider())  # type: ignore[arg-type]
    await runner.run(
        CALLER,
        ops.OP_GET_ISSUE,
        make_request(),
        tool="github.get_issue",
        tool_args=TOOL_ARGS,
        tool_context=TOOL_CONTEXT,
    )

    authorize = daemon.bodies["/v1/authorize"]
    release = daemon.bodies["/v1/credentials:release"]
    assert authorize["tool"] == "github.get_issue"
    assert "action" not in authorize
    assert "connector" not in authorize
    assert authorize["tool_args"] == TOOL_ARGS
    assert authorize["tool_context"] == TOOL_CONTEXT
    assert release["connector"] == {
        "connector_id": "installed-github",
        "capability": "github.issues.get",
    }
    assert release["resource"] == "repos/acme/repo"
    assert release["tool"] == "github.get_issue"
    assert release["tool_args"] == TOOL_ARGS
    assert release["tool_context"] == TOOL_CONTEXT
    assert "connector:" not in json.dumps(daemon.bodies)


async def test_without_tool_arguments_none_are_sent() -> None:
    # Additive: a caller that passes no tool arguments sends none (the daemon
    # rejects tool_args on a tool that is not a registered connector_binding).
    queries, _ = make_queries_spy()
    socket = FakeSocketClient()
    runner = make_runner(socket, make_connectors(queries), Provider())
    await runner.run(CALLER, ops.OP_GET_ISSUE, make_request(), tool="issue")
    assert socket.authorize_calls[0]["tool_args"] is None
    assert socket.authorize_calls[0]["action"] == ""
    assert socket.release_calls[0]["tool_args"] is None
    assert socket.release_calls[0]["capability"] == "github.issues.get"
