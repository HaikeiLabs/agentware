"""Unit tests for the kei-proxy daemon socket client (in-memory transport)."""

import asyncio
import inspect
import json
from collections.abc import Callable
from dataclasses import fields
from typing import Any

import httpx
import pytest

from haikeilabs.agentware.kei import (
    KeiProxyAuthorizeClient,
    KeiProxyEvaluator,
    KeiProxySocketAuthorizationClient,
    KeiProxySocketAuthorizeClient,
    KeiProxySocketError,
    KeiProxySocketIdentity,
    KeiProxySocketSession,
    select_authorization_client,
)
from haikeilabs.agentware.middleware.types import Action, CallerContext

TOKEN = "kei_rt_unit-secret-token-0123456789"
ENV = {"KEI_RUNTIME_TOKEN": TOKEN}
ADA = KeiProxySocketIdentity("openwebui", "ada")


def test_authorize_clients_do_not_accept_agent_id():
    for client in (
        KeiProxyAuthorizeClient,
        KeiProxySocketAuthorizeClient,
        KeiProxySocketAuthorizationClient,
    ):
        assert "agent_id" not in inspect.signature(client.authorize).parameters


def test_caller_context_has_no_authorize_agent_id_input():
    assert "agent_id" not in {item.name for item in fields(CallerContext)}


def test_release_credential_allow_returns_credential() -> None:
    daemon = FakeDaemon()
    daemon.routes[("POST", "/v1/credentials:release")] = {
        "schema": "kei.credential-release-response/v1",
        "decision": "allow",
        "credential": {
            "connector_id": "github",
            "issuer": "https://auth.example.com",
            "resource": "repo:owner/name",
            "account_model": "per_user",
            "access_token": "ghs_secret",
            "expires_at": "2026-10-17T10:00:00Z",
        },
        "policy_id": "pol_01J",
        "bundle_state": "ready",
    }
    client = daemon.client()
    session = asyncio.run(client.open_session(ADA))
    result = asyncio.run(
        client.release_credential(
            session,
            connector_id="github",
            capability="read",
            resource="repo:owner/name",
        )
    )
    assert result["decision"] == "allow"
    assert result["credential"]["access_token"] == "ghs_secret"
    assert result["credential"]["connector_id"] == "github"
    assert TOKEN not in str(result)


def test_release_credential_deny_returns_reason_code() -> None:
    daemon = FakeDaemon()
    daemon.routes[("POST", "/v1/credentials:release")] = {
        "schema": "kei.credential-release-response/v1",
        "decision": "deny",
        "reason_code": "credential_not_connected",
        "reason": "the connector is not connected for this user",
        "bundle_state": "ready",
    }
    client = daemon.client()
    session = asyncio.run(client.open_session(ADA))
    result = asyncio.run(
        client.release_credential(
            session,
            connector_id="github",
            capability="read",
            resource="repo:owner/name",
        )
    )
    assert result["decision"] == "deny"
    assert result["reason_code"] == "credential_not_connected"
    assert "credential" not in result


def test_release_credential_transport_failure_is_an_error() -> None:
    client = FakeDaemon(down=True).client()
    with pytest.raises(KeiProxySocketError):
        asyncio.run(
            client.release_credential(
                KeiProxySocketSession("test", "ada", "sess-1"),
                connector_id="github",
                capability="read",
                resource="repo:owner/name",
            )
        )


def test_release_credential_sends_correct_request_body() -> None:
    daemon = FakeDaemon()
    daemon.routes[("POST", "/v1/credentials:release")] = {
        "schema": "kei.credential-release-response/v1",
        "decision": "allow",
        "credential": {
            "connector_id": "github",
            "issuer": "https://auth.example.com",
            "resource": "repo:owner/name",
            "account_model": "per_user",
            "access_token": "ghs_secret",
            "expires_at": "2026-10-17T10:00:00Z",
        },
        "bundle_state": "ready",
    }
    client = daemon.client()
    asyncio.run(client.open_session(ADA))
    # forget so the next open_session is a real PUT
    client.forget_session(ADA)
    session = asyncio.run(client.open_session(ADA))
    asyncio.run(
        client.release_credential(
            session,
            connector_id="github",
            capability="read",
            resource="repo:owner/name",
            span_id="span-1",
            framework="openwebui",
        )
    )
    bodies = daemon.bodies("/v1/credentials:release")
    assert len(bodies) == 1
    body = bodies[0]
    assert body["schema"] == "kei.credential-release-request/v1"
    assert body["connector"] == {"connector_id": "github", "capability": "read"}
    assert body["resource"] == "repo:owner/name"
    assert body["span_id"] == "span-1"
    assert body["framework"] == "openwebui"


def test_register_tools_posts_manifest_to_runtime_route() -> None:
    daemon = FakeDaemon()
    daemon.routes[("POST", "/v1/tools:register")] = {"registered": 1}
    client = KeiProxySocketAuthorizeClient(env=ENV, transport=httpx.MockTransport(daemon.handle))

    result = asyncio.run(client.register_tools({"schema": "kei.tool-manifest/v3", "tools": []}))

    assert result == {"registered": 1}
    assert daemon.requests[-1].method == "POST"
    assert daemon.requests[-1].url.path == "/v1/tools:register"
    assert json.loads(daemon.requests[-1].content) == {
        "schema": "kei.tool-manifest/v3",
        "tools": [],
    }


PERMIT = {"schema": "kei.authorize-response/v1", "decision": "permit", "bundle_state": "fresh"}


def deny(reason_code: str = "policy_deny", **fields: Any) -> dict[str, Any]:
    return {
        "schema": "kei.authorize-response/v1",
        "decision": "deny",
        "reason_code": reason_code,
        "reason": f"{reason_code} reason",
        "bundle_state": "fresh",
        **fields,
    }


Answer = dict[str, Any] | Callable[[httpx.Request], httpx.Response] | httpx.Response


class FakeDaemon:
    """Answers like kei-proxy serve and records every request."""

    def __init__(self, down: bool = False, timeout: bool = False) -> None:
        self.down = down
        self.timeout = timeout
        self.requests: list[httpx.Request] = []
        self.sessions = 0
        self.authorize: list[Answer] = []
        self.credential_releases: list[Answer] = []
        self.routes: dict[tuple[str, str], Answer] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("socket gone", request=request)
        if self.timeout:
            raise httpx.ReadTimeout("slow", request=request)
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key == ("POST", "/v1/authorize") and self.authorize:
            return _respond(self.authorize.pop(0), request)
        if key == ("POST", "/v1/credentials:release") and self.credential_releases:
            return _respond(self.credential_releases.pop(0), request)
        if key in self.routes:
            return _respond(self.routes[key], request)
        if key == ("PUT", "/v1/session"):
            self.sessions += 1
            return httpx.Response(
                201,
                json={
                    "schema": "kei.session/v2",
                    "session_id": f"sess-{self.sessions}",
                    "user_id": "u",
                },
            )
        if key == ("POST", "/v1/authorize"):
            return httpx.Response(200, json=PERMIT)
        if key == ("DELETE", "/v1/session"):
            return httpx.Response(204)
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, **kwargs: Any) -> KeiProxySocketAuthorizeClient:
        return KeiProxySocketAuthorizeClient(env=ENV, transport=self.transport, **kwargs)

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path]


def _respond(answer: Answer, request: httpx.Request) -> httpx.Response:
    if isinstance(answer, httpx.Response):
        return answer
    if callable(answer):
        return answer(request)
    return httpx.Response(200, json=answer)


def error(status: int, code: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": {"code": code, "message": f"{code} {TOKEN}", "type": "runtime_error"}},
    )


# Sessions -----------------------------------------------------------------


async def test_open_session_puts_exactly_source_and_external_id() -> None:
    daemon = FakeDaemon()
    session = await daemon.client().open_session(ADA)
    assert session == KeiProxySocketSession("openwebui", "ada", "sess-1")
    assert daemon.bodies("/v1/session") == [
        {"schema": "kei.session/v2", "source": "openwebui", "external_id": "ada"}
    ]
    [request] = daemon.requests
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert "X-Kei-Session" not in request.headers


async def test_open_session_is_cached_per_identity() -> None:
    daemon = FakeDaemon()
    client = daemon.client()
    first = await client.open_session(ADA)
    assert await client.open_session(ADA) is first
    other = await client.open_session(KeiProxySocketIdentity("openwebui", "bob"))
    assert other.session_id == "sess-2"
    assert len(daemon.bodies("/v1/session")) == 2


@pytest.mark.parametrize("identity", [("", "ada"), ("openwebui", " "), ("openwebui", "")])
async def test_open_session_requires_an_explicit_identity(identity: tuple[str, str]) -> None:
    daemon = FakeDaemon()
    with pytest.raises(ValueError):
        await daemon.client().open_session(KeiProxySocketIdentity(*identity))
    assert daemon.requests == []


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(201, json={"session_id": "s"}),
        httpx.Response(201, json={"schema": "kei.session/v2"}),
        httpx.Response(201, json={"schema": "kei.session/v2", "session_id": ""}),
        httpx.Response(201, text="not json"),
        httpx.Response(201, json=["s"]),
    ],
)
async def test_an_invalid_session_answer_fails_closed(answer: httpx.Response) -> None:
    daemon = FakeDaemon()
    daemon.routes[("PUT", "/v1/session")] = answer
    with pytest.raises(KeiProxySocketError) as exc:
        await daemon.client().open_session(ADA)
    assert exc.value.reason_class == "malformed_response"


async def test_a_refused_session_fails_closed_with_the_daemon_code() -> None:
    daemon = FakeDaemon()
    daemon.routes[("PUT", "/v1/session")] = error(400, "invalid_session")
    with pytest.raises(KeiProxySocketError) as exc:
        await daemon.client().open_session(ADA)
    assert (exc.value.reason_class, exc.value.code) == ("proxy_error", "invalid_session")
    assert TOKEN not in str(exc.value)


async def test_close_session_deletes_and_forgets_it() -> None:
    daemon = FakeDaemon()
    client = daemon.client()
    session = await client.open_session(ADA)
    await client.close_session(session)
    [delete] = [r for r in daemon.requests if r.method == "DELETE"]
    assert delete.headers["X-Kei-Session"] == "sess-1"
    assert (await client.open_session(ADA)).session_id == "sess-2"


async def test_closing_an_ended_session_is_not_an_error() -> None:
    daemon = FakeDaemon()
    daemon.routes[("DELETE", "/v1/session")] = error(404, "session_not_found")
    client = daemon.client()
    await client.close_session(await client.open_session(ADA))


async def test_close_session_raises_on_other_errors() -> None:
    daemon = FakeDaemon()
    daemon.routes[("DELETE", "/v1/session")] = error(401, "invalid_runtime_token")
    client = daemon.client()
    with pytest.raises(KeiProxySocketError) as exc:
        await client.close_session(await client.open_session(ADA))
    assert str(exc.value).startswith("runtime authentication failed")


async def test_forget_session_makes_the_next_open_put_again() -> None:
    daemon = FakeDaemon()
    client = daemon.client()
    await client.open_session(ADA)
    client.forget_session(ADA)
    assert (await client.open_session(ADA)).session_id == "sess-2"


# Authorize ----------------------------------------------------------------


async def test_authorize_sends_the_request_schema_in_the_session_and_no_user() -> None:
    daemon = FakeDaemon()
    client = daemon.client()
    session = await client.open_session(ADA)
    answer = await client.authorize(
        session,
        tool="github.create_issue",
        action="execute",
        resource="github:repo:o/r",
        span_id="span-1",
        delegation_depth=0,
        framework="openwebui",
        tool_args_digest="d" * 64,
    )
    assert answer == PERMIT
    [request] = [r for r in daemon.requests if r.url.path == "/v1/authorize"]
    assert request.headers["X-Kei-Session"] == "sess-1"
    # Empty and zero fields are omitted; the daemon rejects unknown fields.
    assert json.loads(request.content) == {
        "schema": "kei.authorize-request/v1",
        "tool": "github.create_issue",
        "action": "execute",
        "resource": "github:repo:o/r",
        "span_id": "span-1",
        "framework": "openwebui",
        "tool_args_digest": "d" * 64,
    }
    assert b"ada" not in request.content


async def test_authorize_serializes_an_exact_connector_target() -> None:
    daemon = FakeDaemon()
    client = daemon.client()
    session = await client.open_session(ADA)

    await client.authorize(
        session,
        tool="github.read_repo",
        resource="repos/owner/name",
        connector_id="connector-1",
        capability="repository.read",
    )

    [request] = [r for r in daemon.requests if r.url.path == "/v1/authorize"]
    assert json.loads(request.content) == {
        "schema": "kei.authorize-request/v1",
        "tool": "github.read_repo",
        "connector": {"connector_id": "connector-1", "capability": "repository.read"},
        "resource": "repos/owner/name",
    }
    assert request.headers["X-Kei-Session"] == session.session_id
    assert "user_id" not in json.loads(request.content)
    assert "agent_id" not in json.loads(request.content)


@pytest.mark.parametrize(
    ("target", "resource"),
    [
        ({"connector_id": "connector-1"}, "repos/owner/name"),
        ({"capability": "repository.read"}, "repos/owner/name"),
        ({"connector_id": "connector-1", "capability": " "}, "repos/owner/name"),
        ({"connector_id": "connector-1", "capability": "repository.read"}, ""),
    ],
)
async def test_authorize_rejects_malformed_or_partial_targets(
    target: dict[str, str], resource: str
) -> None:
    daemon = FakeDaemon()
    client = daemon.client()
    session = await client.open_session(ADA)

    with pytest.raises(ValueError):
        await client.authorize(session, tool="github.read_repo", resource=resource, **target)
    assert not [r for r in daemon.requests if r.url.path == "/v1/authorize"]


async def test_synchronous_socket_adapter_forwards_an_exact_target() -> None:
    daemon = FakeDaemon()
    adapter = KeiProxySocketAuthorizationClient("openwebui", daemon.client())

    adapter.authorize(
        "ada",
        "github.read_repo",
        "execute",
        "repos/owner/name",
        connector_id="connector-1",
        capability="repository.read",
    )

    [request] = [r for r in daemon.requests if r.url.path == "/v1/authorize"]
    body = json.loads(request.content)
    assert body["connector"] == {
        "connector_id": "connector-1",
        "capability": "repository.read",
    }
    assert body["resource"] == "repos/owner/name"
    assert "user_id" not in body and "agent_id" not in body


async def test_release_credential_sends_exact_target_and_returns_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    daemon = FakeDaemon()
    released = {
        "schema": "kei.credential-release-response/v1",
        "decision": "permit",
        "credential": {"access_token": "secret-release-test", "token_type": "Bearer"},
    }
    daemon.credential_releases = [released]
    client = daemon.client()
    session = await client.open_session(ADA)

    response = await client.release_credential(
        session,
        connector_id="connector-1",
        capability="repository.read",
        resource="repos/owner/name",
        tool="github.read_repo",
        span_id="span-1",
        framework="openwebui",
    )

    assert response == released
    [request] = [r for r in daemon.requests if r.url.path == "/v1/credentials:release"]
    assert request.headers["X-Kei-Session"] == session.session_id
    assert json.loads(request.content) == {
        "schema": "kei.credential-release-request/v1",
        "connector": {"connector_id": "connector-1", "capability": "repository.read"},
        "resource": "repos/owner/name",
        "tool": "github.read_repo",
        "span_id": "span-1",
        "framework": "openwebui",
    }
    assert "secret-release-test" not in caplog.text


async def test_release_credential_reopens_a_missing_session_once() -> None:
    daemon = FakeDaemon()
    released = {"schema": "kei.credential-release-response/v1", "decision": "permit"}
    daemon.credential_releases = [error(404, "session_not_found"), released]
    client = daemon.client()
    session = await client.open_session(ADA)

    assert (
        await client.release_credential(
            session,
            connector_id="connector-1",
            capability="repository.read",
            resource="repos/owner/name",
        )
        == released
    )
    requests = [r for r in daemon.requests if r.url.path == "/v1/credentials:release"]
    assert [request.headers["X-Kei-Session"] for request in requests] == ["sess-1", "sess-2"]
    assert daemon.sessions == 2


async def test_synchronous_socket_adapter_releases_for_its_named_session() -> None:
    daemon = FakeDaemon()
    released = {"schema": "kei.credential-release-response/v1", "decision": "permit"}
    daemon.credential_releases = [released]
    adapter = KeiProxySocketAuthorizationClient("openwebui", daemon.client())

    assert (
        adapter.release_credential(
            "ada",
            connector_id="connector-1",
            capability="repository.read",
            resource="repos/owner/name",
        )
        == released
    )
    [session_request] = [r for r in daemon.requests if r.url.path == "/v1/session"]
    [release_request] = [r for r in daemon.requests if r.url.path == "/v1/credentials:release"]
    assert json.loads(session_request.content) == {
        "schema": "kei.session/v2",
        "source": "openwebui",
        "external_id": "ada",
    }
    assert release_request.headers["X-Kei-Session"] == "sess-1"


async def test_a_deny_is_returned_not_raised() -> None:
    daemon = FakeDaemon()
    daemon.authorize = [
        deny(
            "subject_unlinked",
            hint="Link your account in Kei.",
            enrollment={"claim_url": "https://x"},
        )
    ]
    client = daemon.client()
    answer = await client.authorize(await client.open_session(ADA), tool="t")
    assert answer["decision"] == "deny" and answer["reason_code"] == "subject_unlinked"


async def test_session_not_found_in_the_answer_reopens_once() -> None:
    daemon = FakeDaemon()
    daemon.authorize = [deny("session_not_found"), PERMIT]
    client = daemon.client()
    answer = await client.authorize(await client.open_session(ADA), tool="t")
    assert answer == PERMIT
    sessions = [
        r.headers.get("X-Kei-Session") for r in daemon.requests if r.url.path == "/v1/authorize"
    ]
    assert sessions == ["sess-1", "sess-2"]
    assert (await client.open_session(ADA)).session_id == "sess-2"


async def test_session_not_found_as_an_http_error_reopens_once() -> None:
    daemon = FakeDaemon()
    daemon.authorize = [error(403, "session_not_found"), PERMIT]
    client = daemon.client()
    assert await client.authorize(await client.open_session(ADA), tool="t") == PERMIT


async def test_a_second_session_not_found_stays_a_deny() -> None:
    daemon = FakeDaemon()
    daemon.authorize = [deny("session_not_found"), deny("session_not_found")]
    client = daemon.client()
    answer = await client.authorize(await client.open_session(ADA), tool="t")
    assert answer["decision"] == "deny"
    assert daemon.calls().count(("PUT", "/v1/session")) == 2


async def test_a_second_session_not_found_error_raises() -> None:
    daemon = FakeDaemon()
    daemon.authorize = [error(404, "session_not_found"), error(404, "session_not_found")]
    client = daemon.client()
    with pytest.raises(KeiProxySocketError) as exc:
        await client.authorize(await client.open_session(ADA), tool="t")
    assert exc.value.code == "session_not_found"


@pytest.mark.parametrize(
    ("answer", "reason_class"),
    [
        (error(500, "boom"), "proxy_error"),
        (error(401, "invalid_runtime_token"), "proxy_error"),
        (httpx.Response(200, text="{not json"), "malformed_response"),
        (httpx.Response(200, json=["permit"]), "malformed_response"),
        (httpx.Response(200, json={"decision": "permit"}), "malformed_response"),
        (
            httpx.Response(200, json={"schema": "kei.authorize-response/v1", "decision": 1}),
            "malformed_response",
        ),
    ],
)
async def test_authorize_fails_closed(answer: httpx.Response, reason_class: str) -> None:
    daemon = FakeDaemon()
    daemon.authorize = [answer]
    client = daemon.client()
    with pytest.raises(KeiProxySocketError) as exc:
        await client.authorize(await client.open_session(ADA), tool="t")
    assert exc.value.reason_class == reason_class
    assert TOKEN not in str(exc.value)


async def test_an_unreachable_daemon_is_a_refusal() -> None:
    client = FakeDaemon(down=True).client()
    with pytest.raises(KeiProxySocketError) as exc:
        await client.open_session(ADA)
    assert exc.value.reason_class == "proxy_unavailable"


async def test_a_slow_daemon_is_a_refusal() -> None:
    client = FakeDaemon(timeout=True).client()
    with pytest.raises(KeiProxySocketError) as exc:
        await client.open_session(ADA)
    assert exc.value.reason_class == "proxy_timeout"


async def test_no_runtime_token_sends_nothing() -> None:
    daemon = FakeDaemon()
    client = KeiProxySocketAuthorizeClient(env={}, transport=daemon.transport)
    with pytest.raises(KeiProxySocketError) as exc:
        await client.open_session(ADA)
    assert exc.value.reason_class == "missing_token"
    assert daemon.requests == []


async def test_authorize_needs_a_named_session() -> None:
    with pytest.raises(ValueError):
        await FakeDaemon().client().authorize(KeiProxySocketSession("s", "e", ""), tool="t")


def test_the_token_is_not_in_the_repr() -> None:
    client = KeiProxySocketAuthorizeClient(socket_path="/tmp/x.sock", env=ENV)
    assert TOKEN not in repr(client)
    assert TOKEN not in repr(KeiProxySocketAuthorizationClient("openwebui", client))


def test_the_socket_path_comes_from_the_environment() -> None:
    assert KeiProxySocketAuthorizeClient(env={}).socket_path == "/run/kei-proxy/runtime.sock"
    env = {"KEI_RUNTIME_SOCKET_PATH": "/var/run/k.sock"}
    assert KeiProxySocketAuthorizeClient(env=env).socket_path == "/var/run/k.sock"
    assert KeiProxySocketAuthorizeClient("/a.sock", env=env).socket_path == "/a.sock"


# Model routes -------------------------------------------------------------


async def test_list_models_keeps_entries_with_an_id() -> None:
    daemon = FakeDaemon()
    daemon.routes[("GET", "/v1/models")] = {
        "object": "list",
        "data": [{"id": "gpt-4.1", "object": "model"}, {"name": "no id"}, "junk", {"id": ""}],
    }
    assert await daemon.client().list_models() == [{"id": "gpt-4.1", "object": "model"}]


async def test_list_models_fails_closed() -> None:
    daemon = FakeDaemon()
    daemon.routes[("GET", "/v1/models")] = error(502, "profile_unavailable")
    with pytest.raises(KeiProxySocketError) as exc:
        await daemon.client().list_models()
    assert exc.value.code == "profile_unavailable"


async def test_chat_completion_runs_in_the_session() -> None:
    daemon = FakeDaemon()
    daemon.routes[("POST", "/v1/chat/completions")] = {"id": "c1"}
    client = daemon.client()
    answer = await client.chat_completion(
        await client.open_session(ADA), {"model": "m", "messages": [], "stream": True}
    )
    assert answer == {"id": "c1"}
    [request] = [r for r in daemon.requests if r.url.path == "/v1/chat/completions"]
    assert request.headers["X-Kei-Session"] == "sess-1"
    assert json.loads(request.content) == {"model": "m", "messages": [], "stream": False}


async def test_chat_completion_reopens_an_ended_session_once() -> None:
    daemon = FakeDaemon()
    answers = [error(404, "session_not_found"), httpx.Response(200, json={"id": "c2"})]
    daemon.routes[("POST", "/v1/chat/completions")] = lambda _: answers.pop(0)
    client = daemon.client()
    assert await client.chat_completion(await client.open_session(ADA), {"model": "m"}) == {
        "id": "c2"
    }
    sessions = [
        r.headers.get("X-Kei-Session")
        for r in daemon.requests
        if r.url.path.endswith("completions")
    ]
    assert sessions == ["sess-1", "sess-2"]


async def test_chat_completion_refusal_raises() -> None:
    daemon = FakeDaemon()
    daemon.routes[("POST", "/v1/chat/completions")] = error(403, "model_denied")
    client = daemon.client()
    with pytest.raises(KeiProxySocketError) as exc:
        await client.chat_completion(await client.open_session(ADA), {"model": "m"})
    assert exc.value.code == "model_denied"


async def test_stream_chat_completion_yields_data_lines() -> None:
    daemon = FakeDaemon()
    daemon.routes[("POST", "/v1/chat/completions")] = httpx.Response(
        200, text='data: {"x": 1}\n\n: keepalive\n\ndata: [DONE]\n\n'
    )
    client = daemon.client()
    session = await client.open_session(ADA)
    lines = [line async for line in client.stream_chat_completion(session, {"model": "m"})]
    assert lines == ['data: {"x": 1}', "data: [DONE]"]
    [request] = [r for r in daemon.requests if r.url.path.endswith("completions")]
    assert json.loads(request.content)["stream"] is True


async def test_stream_chat_completion_reopens_an_ended_session_once() -> None:
    daemon = FakeDaemon()
    answers = [error(403, "session_not_found"), httpx.Response(200, text="data: ok\n\n")]
    daemon.routes[("POST", "/v1/chat/completions")] = lambda _: answers.pop(0)
    client = daemon.client()
    session = await client.open_session(ADA)
    assert [line async for line in client.stream_chat_completion(session, {})] == ["data: ok"]


async def test_stream_chat_completion_fails_closed() -> None:
    daemon = FakeDaemon()
    daemon.routes[("POST", "/v1/chat/completions")] = error(500, "boom")
    client = daemon.client()
    session = await client.open_session(ADA)
    with pytest.raises(KeiProxySocketError):
        [line async for line in client.stream_chat_completion(session, {})]
    down = FakeDaemon(down=True).client()
    with pytest.raises(KeiProxySocketError) as exc:
        [line async for line in down.stream_chat_completion(session, {})]
    assert exc.value.reason_class == "proxy_unavailable"


async def test_policy_bundle_status() -> None:
    daemon = FakeDaemon()
    status = {"schema": "kei.policy-bundle-status/v1", "bundle_schema": "kei.policy-bundle/v2"}
    daemon.routes[("GET", "/v1/policy-bundle/status")] = status
    assert await daemon.client().policy_bundle_status() == status
    daemon.routes[("GET", "/v1/policy-bundle/status")] = {"bundle_schema": "x"}
    with pytest.raises(KeiProxySocketError):
        await daemon.client().policy_bundle_status()


# Evaluator adapter --------------------------------------------------------


def caller(**fields: Any) -> CallerContext:
    return CallerContext(user_id="ada", invoking_subject="ada", framework="openwebui", **fields)


def test_the_evaluator_allows_over_the_socket() -> None:
    daemon = FakeDaemon()
    evaluator = KeiProxyEvaluator(KeiProxySocketAuthorizationClient("openwebui", daemon.client()))
    decision = evaluator.evaluate("github.get_issue", {"owner": "o", "repo": "r"}, caller())
    assert decision.action is Action.ALLOW
    assert daemon.bodies("/v1/session") == [
        {"schema": "kei.session/v2", "source": "openwebui", "external_id": "ada"}
    ]
    [body] = daemon.bodies("/v1/authorize")
    assert body["tool"] == "github.get_issue"
    assert body["resource"] == "github:repo:o/r"
    assert "agent_id" not in body
    assert set(body) <= {
        "schema", "tool", "action", "resource", "span_id", "parent_span",
        "delegation_depth", "framework", "tool_args_digest",
    }  # fmt: skip


def test_the_evaluator_reuses_the_session() -> None:
    daemon = FakeDaemon()
    evaluator = KeiProxyEvaluator(KeiProxySocketAuthorizationClient("openwebui", daemon.client()))
    for _ in range(3):
        evaluator.evaluate("t", {}, caller())
    assert daemon.calls().count(("PUT", "/v1/session")) == 1


@pytest.mark.parametrize(
    ("daemon", "reason"),
    [
        (FakeDaemon(down=True), "kei-proxy proxy_unavailable"),
        (FakeDaemon(timeout=True), "kei-proxy proxy_timeout"),
    ],
)
def test_the_evaluator_denies_when_the_daemon_is_gone(daemon: FakeDaemon, reason: str) -> None:
    evaluator = KeiProxyEvaluator(KeiProxySocketAuthorizationClient("openwebui", daemon.client()))
    decision = evaluator.evaluate("t", {}, caller())
    assert decision.action is Action.DENY
    assert decision.reason.startswith(reason)


def test_socket_unavailable_error_includes_path_and_install_command() -> None:
    socket_path = "/run/custom/kei-proxy.sock"
    daemon = FakeDaemon(down=True)
    client = KeiProxySocketAuthorizeClient(
        socket_path=socket_path, env=ENV, transport=daemon.transport
    )
    decision = KeiProxyEvaluator(KeiProxySocketAuthorizationClient("openwebui", client)).evaluate(
        "t", {}, caller()
    )
    assert socket_path in decision.reason
    assert "--proxy-only" in decision.reason


def test_the_evaluator_denies_a_daemon_deny_with_its_enrollment() -> None:
    daemon = FakeDaemon()
    daemon.authorize = [
        deny(
            "subject_unlinked",
            hint="Link your account in Kei.",
            enrollment={"claim_url": "https://x"},
        )
    ]
    evaluator = KeiProxyEvaluator(KeiProxySocketAuthorizationClient("openwebui", daemon.client()))
    decision = evaluator.evaluate("t", {}, caller())
    assert decision.action is Action.DENY
    assert decision.reason == "kei-proxy deny: subject_unlinked reason"
    assert decision.enrollment == {"claim_url": "https://x"}
    assert decision.reason_code == "subject_unlinked"
    assert decision.hint == "Link your account in Kei."


def test_the_evaluator_denies_without_a_subject() -> None:
    daemon = FakeDaemon()
    evaluator = KeiProxyEvaluator(KeiProxySocketAuthorizationClient("openwebui", daemon.client()))
    decision = evaluator.evaluate("t", {}, CallerContext())
    assert decision.action is Action.DENY
    assert daemon.requests == []


async def test_the_adapter_works_inside_a_running_loop() -> None:
    daemon = FakeDaemon()
    adapter = KeiProxySocketAuthorizationClient("openwebui", daemon.client())
    answer = await asyncio.to_thread(lambda: adapter.authorize("ada", "t", "execute", "t"))
    assert answer == PERMIT
    assert adapter.authorize("ada", "t", "execute", "t") == PERMIT


def test_the_adapter_needs_a_source() -> None:
    with pytest.raises(ValueError):
        KeiProxySocketAuthorizationClient(" ")


# Client selection ---------------------------------------------------------


def test_a_configured_socket_selects_the_socket_client() -> None:
    env = {"KEI_RUNTIME_SOCKET_PATH": "/var/run/k.sock", "KEI_RUNTIME_TOKEN": TOKEN}
    client = select_authorization_client("openwebui", env=env)
    assert isinstance(client, KeiProxySocketAuthorizationClient)
    assert client.client.socket_path == "/var/run/k.sock"
    explicit = select_authorization_client("openwebui", socket_path="/a.sock", env={})
    assert isinstance(explicit, KeiProxySocketAuthorizationClient)


def test_no_socket_falls_back_to_the_subprocess_client() -> None:
    client = select_authorization_client("openwebui", env={"PATH": "/nonexistent"})
    assert isinstance(client, KeiProxyAuthorizeClient)
