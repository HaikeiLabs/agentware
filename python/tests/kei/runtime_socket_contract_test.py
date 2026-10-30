"""Contract test: the socket client against a fake kei-proxy daemon on a real Unix socket.

The fake mirrors kei-connector-runtime ``runtime/socket_authorize.go`` and
``serve.go``: strict request schemas (unknown fields are a 400), a bearer
token on every route, ``kei.session/v2`` named sessions answered 201, an
unknown session answered as a ``session_not_found`` deny on authorize and a
404 error elsewhere, and ``{"error": {"code", "message", "type"}}`` errors.
"""

import json
import socket
import socketserver
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import pytest

from haikeilabs.agentware.kei import (
    KeiProxyEvaluator,
    KeiProxySocketAuthorizationClient,
    KeiProxySocketAuthorizeClient,
    KeiProxySocketError,
    KeiProxySocketIdentity,
    select_authorization_client,
)
from haikeilabs.agentware.middleware.types import Action, CallerContext

TOKEN = "kei_rt_contract-secret-token-0123456789"
AUTHORIZE_FIELDS = {
    "schema", "tool", "action", "resource", "connector", "span_id",
    "parent_span", "delegation_depth", "framework", "tool_args_digest", "shell_command",
}  # fmt: skip
SESSION_FIELDS = {"schema", "user_id", "email", "status", "enrollment", "source", "external_id"}
LINKED = {("openwebui", "ada"): "user-ada"}
DENIED_TOOLS = {"github.delete_repo"}


class Daemon:
    """State shared by the handler threads."""

    def __init__(self) -> None:
        self.sessions: dict[str, str] = {}
        self.next_id = 0
        self.log: list[tuple[str, str, dict[str, Any]]] = []
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    daemon: Daemon

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def address_string(self) -> str:
        return "unix"

    def _body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _send(self, status: int, body: Any = None, content_type: str = "application/json") -> None:
        data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        self.send_response(status)
        if data:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, code: str) -> None:
        self._send(status, {"error": {"message": code, "type": "runtime_error", "code": code}})

    def _route(self) -> None:
        body = self._body()
        with self.daemon.lock:
            self.daemon.log.append((self.command, self.path, body or {}))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._error(401, "invalid_runtime_token")
        session = self.headers.get("X-Kei-Session", "")
        route = (self.command, self.path)
        if route == ("PUT", "/v1/session"):
            if body is None or set(body) - SESSION_FIELDS or body.get("schema") != "kei.session/v2":
                return self._error(400, "invalid_request")
            if not body.get("source") or not body.get("external_id"):
                return self._error(400, "invalid_session")
            with self.daemon.lock:
                self.daemon.next_id += 1
                session_id = f"sess-{self.daemon.next_id}"
                user = LINKED.get((body["source"], body["external_id"]), "")
                self.daemon.sessions[session_id] = user
            status = "linked" if user else "unlinked"
            return self._send(
                201,
                {"schema": "kei.session/v2", "session_id": session_id, "user_id": user, "status": status},
            )
        if route == ("DELETE", "/v1/session"):
            with self.daemon.lock:
                if self.daemon.sessions.pop(session, None) is None:
                    return self._error(404, "session_not_found")
            return self._send(204)
        if route == ("POST", "/v1/authorize"):
            if (
                body is None
                or set(body) - AUTHORIZE_FIELDS
                or body.get("schema") != "kei.authorize-request/v1"
                or not body.get("tool")
            ):
                return self._error(400, "invalid_request")
            answer: dict[str, Any] = {"schema": "kei.authorize-response/v1", "bundle_state": "fresh"}
            if session not in self.daemon.sessions:
                answer |= {"decision": "deny", "reason_code": "session_not_found"}
            elif not self.daemon.sessions[session]:
                answer |= {
                    "decision": "deny",
                    "reason_code": "subject_unlinked",
                    "reason": "link your account",
                    "enrollment": {"claim_url": "https://kei.example.test/claim/1"},
                }
            elif body["tool"] in DENIED_TOOLS:
                answer |= {"decision": "deny", "reason_code": "policy_deny", "policy_id": "p-1"}
            else:
                answer |= {"decision": "permit", "policy_id": "p-allow"}
            return self._send(200, answer)
        if route == ("GET", "/v1/models"):
            return self._send(200, {"object": "list", "data": [{"id": "gpt-4.1", "object": "model"}]})
        if route == ("POST", "/v1/chat/completions"):
            if session not in self.daemon.sessions:
                return self._error(404, "session_not_found")
            if body and body.get("stream"):
                return self._send(
                    200,
                    b'data: {"choices": []}\n\ndata: [DONE]\n\n',
                    content_type="text/event-stream",
                )
            return self._send(200, {"id": "chatcmpl-1", "model": (body or {}).get("model")})
        if route == ("GET", "/v1/policy-bundle/status"):
            return self._send(
                200, {"schema": "kei.policy-bundle-status/v1", "bundle_schema": "kei.policy-bundle/v2"}
            )
        return self._error(404, "not_found")

    do_GET = do_PUT = do_POST = do_DELETE = _route  # noqa: N815 (http.server hook names)


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def daemon() -> Iterator[tuple[Daemon, str]]:
    # macOS caps socket paths near 104 bytes; pytest's tmp_path can exceed it.
    directory = tempfile.mkdtemp(prefix="kei-", dir="/tmp")
    path = str(Path(directory) / "runtime.sock")
    state = Daemon()
    handler = type("BoundHandler", (Handler,), {"daemon": state})
    server = UnixServer(path, handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, path
    finally:
        server.shutdown()
        server.server_close()
        Path(path).unlink(missing_ok=True)
        Path(directory).rmdir()


def client(path: str, token: str = TOKEN) -> KeiProxySocketAuthorizeClient:
    return KeiProxySocketAuthorizeClient(env={"KEI_RUNTIME_SOCKET_PATH": path, "KEI_RUNTIME_TOKEN": token})


ADA = KeiProxySocketIdentity("openwebui", "ada")


async def test_session_authorize_and_close(daemon: tuple[Daemon, str]) -> None:
    state, path = daemon
    c = client(path)
    session = await c.open_session(ADA)
    assert session.session_id == "sess-1"
    allowed = await c.authorize(
        session,
        tool="github.get_issue",
        action="execute",
        resource="github:issue:1",
        span_id="s-1",
        parent_span="p-0",
        delegation_depth=1,
        framework="openwebui",
        tool_args_digest="a" * 64,
    )
    assert allowed["decision"] == "permit" and allowed["policy_id"] == "p-allow"
    [default_body] = [entry[2] for entry in state.log if entry[:2] == ("POST", "/v1/authorize")]
    assert default_body == {
        "schema": "kei.authorize-request/v1",
        "tool": "github.get_issue",
        "action": "execute",
        "resource": "github:issue:1",
        "span_id": "s-1",
        "parent_span": "p-0",
        "delegation_depth": 1,
        "framework": "openwebui",
        "tool_args_digest": "a" * 64,
    }
    denied = await c.authorize(session, tool="github.delete_repo")
    assert (denied["decision"], denied["reason_code"]) == ("deny", "policy_deny")
    await c.close_session(session)
    assert state.sessions == {}
    # Closing again is idempotent.
    await c.close_session(session)


async def test_exact_target_authorize_contract(daemon: tuple[Daemon, str]) -> None:
    state, path = daemon
    c = client(path)
    session = await c.open_session(ADA)

    answer = await c.authorize(
        session,
        tool="github.read_repo",
        connector_id="conn-fixture-1",
        capability="repository.read",
        resource="repos/owner/name",
    )

    assert answer["decision"] == "permit"
    [body] = [entry[2] for entry in state.log if entry[:2] == ("POST", "/v1/authorize")]
    assert body == {
        "schema": "kei.authorize-request/v1",
        "tool": "github.read_repo",
        "connector": {
            "connector_id": "conn-fixture-1",
            "capability": "repository.read",
        },
        "resource": "repos/owner/name",
    }
    assert "user_id" not in body and "agent_id" not in body


async def test_an_unlinked_subject_is_a_deny_with_its_enrollment(daemon: tuple[Daemon, str]) -> None:
    _, path = daemon
    c = client(path)
    answer = await c.authorize(
        await c.open_session(KeiProxySocketIdentity("openwebui", "stranger")), tool="t"
    )
    assert answer["reason_code"] == "subject_unlinked"
    assert answer["enrollment"] == {"claim_url": "https://kei.example.test/claim/1"}


async def test_an_ended_session_is_reopened_once(daemon: tuple[Daemon, str]) -> None:
    state, path = daemon
    c = client(path)
    session = await c.open_session(ADA)
    state.sessions.clear()  # the daemon restarted or the session idled out
    assert (await c.authorize(session, tool="t"))["decision"] == "permit"
    assert [e[:2] for e in state.log].count(("PUT", "/v1/session")) == 2
    state.sessions.clear()
    reply = await c.chat_completion(await c.open_session(ADA), {"model": "gpt-4.1", "messages": []})
    assert reply["id"] == "chatcmpl-1"


async def test_model_routes_and_bundle_status(daemon: tuple[Daemon, str]) -> None:
    _, path = daemon
    c = client(path)
    assert [m["id"] for m in await c.list_models()] == ["gpt-4.1"]
    session = await c.open_session(ADA)
    lines = [line async for line in c.stream_chat_completion(session, {"model": "gpt-4.1"})]
    assert lines == ['data: {"choices": []}', "data: [DONE]"]
    status = await c.policy_bundle_status()
    assert status["bundle_schema"] == "kei.policy-bundle/v2"


async def test_a_wrong_token_fails_closed(daemon: tuple[Daemon, str]) -> None:
    _, path = daemon
    with pytest.raises(KeiProxySocketError) as exc:
        await client(path, token="wrong").open_session(ADA)
    assert (exc.value.reason_class, exc.value.code) == ("proxy_error", "invalid_runtime_token")


async def test_no_daemon_on_the_socket_fails_closed() -> None:
    directory = tempfile.mkdtemp(prefix="kei-", dir="/tmp")
    path = str(Path(directory) / "runtime.sock")
    try:
        with pytest.raises(KeiProxySocketError) as exc:
            await client(path).open_session(ADA)
        assert exc.value.reason_class == "proxy_unavailable"
        # A socket file nobody listens on.
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.close()
        with pytest.raises(KeiProxySocketError) as exc:
            await client(path).open_session(ADA)
        assert exc.value.reason_class == "proxy_unavailable"
    finally:
        Path(path).unlink(missing_ok=True)
        Path(directory).rmdir()


def test_the_evaluator_over_the_socket(daemon: tuple[Daemon, str]) -> None:
    state, path = daemon
    env = {"KEI_RUNTIME_SOCKET_PATH": path, "KEI_RUNTIME_TOKEN": TOKEN}
    authorization = select_authorization_client("openwebui", env=env)
    assert isinstance(authorization, KeiProxySocketAuthorizationClient)
    evaluator = KeiProxyEvaluator(authorization)
    ada = CallerContext(user_id="ada", invoking_subject="ada", framework="openwebui", span_id="s")

    allow = evaluator.evaluate("github.get_issue", {"owner": "o", "repo": "r"}, ada)
    assert allow.action is Action.ALLOW and allow.rule == "p-allow"
    deny = evaluator.evaluate("github.delete_repo", {}, ada)
    assert deny.action is Action.DENY and deny.reason == "kei-proxy deny"
    # Every request was accepted by the daemon's strict schema: no 400s.
    assert all(not body.get("user_id") for _, _, body in state.log)
    assert [e[:2] for e in state.log].count(("PUT", "/v1/session")) == 1


def test_the_evaluator_denies_when_the_daemon_is_down() -> None:
    env = {"KEI_RUNTIME_SOCKET_PATH": "/tmp/kei-no-such-daemon.sock", "KEI_RUNTIME_TOKEN": TOKEN}
    evaluator = KeiProxyEvaluator(select_authorization_client("openwebui", env=env))
    decision = evaluator.evaluate("t", {}, CallerContext(user_id="ada", invoking_subject="ada"))
    assert decision.action is Action.DENY
    assert decision.reason.startswith("kei-proxy proxy_unavailable")
    assert TOKEN not in decision.reason
