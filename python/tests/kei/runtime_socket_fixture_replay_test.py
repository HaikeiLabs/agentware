"""Replay of the shared runtime-socket contract fixtures.

Replays ``testing/contracts/runtime-socket/constants.json``,
``client-cases.json`` and ``tool-sync-cases.json`` against
:mod:`haikeilabs.agentware.kei.runtime_socket` and
:mod:`haikeilabs.agentware.kei.tool_sync`, the way Go
(``go/kei/runtimesocket/contract_test.go``,
``go/kei/toolsync/contract_test.go``), TypeScript and Rust replay them. See
``testing/contracts/runtime-socket/README.md`` for the runner rules: a fake
daemon answers ``exchanges[i].response`` in order and records each request as
``{method, path, query, session, body}`` (``path`` excludes the query string,
``query`` is recorded separately); ``session`` is the ``X-Kei-Session`` header
or ``""``; ``body`` is the parsed JSON body or ``None``. A response is
``{status, body}`` (JSON), ``{status, raw, content_type}`` (verbatim bytes),
``{status}`` (no body), or ``{"stall": true}`` (never answers).

The client runs on the in-memory transport the SDK exposes for tests
(``KeiProxySocketAuthorizeClient(transport=...)``); the ``daemon: "absent"``
case runs on a real, unlistened Unix socket path so the refusal comes from a
genuine dial failure. ``httpx.MockTransport`` does not enforce the client's
timeout, so a ``stall`` answer is simulated the way the SDK's own tests
simulate one: the handler raises ``httpx.ReadTimeout``, which the client maps
to ``proxy_timeout`` exactly as it would after a real one.

The tool-sync cases run on the same in-memory transport the SDK exposes for
tests (``sync_tool_manifest(transport=...)``) instead of a loopback server:
the client is the same ``httpx.Client`` either way, so the request bytes,
response handling and error classes are identical. A ``network_error`` answer
is simulated by raising ``httpx.ConnectError``, the class a dropped connection
produces. The ``sleep`` function is replaced by a recorder. The ``ambient``
proxy layer is httpx's own resolution, not ``tool_sync``'s, so those cases
are skipped, as the README says.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from haikeilabs.agentware.kei import runtime_socket as rs
from haikeilabs.agentware.kei import tool_sync
from haikeilabs.agentware.kei.auth import BOOTSTRAP_TOKEN_ENV
from haikeilabs.agentware.kei.evaluator import REASON_CLASSES
from haikeilabs.agentware.kei.policy_bundle import (
    RUNTIME_CONTROL_PLANE_URL_ENV,
    RUNTIME_TOKEN_ENV,
)

FIXTURES = Path(__file__).resolve().parents[3] / "testing" / "contracts" / "runtime-socket"


def _load(name: str) -> dict[str, Any]:
    path = FIXTURES / name
    if not path.is_file():
        pytest.skip(f"contract fixtures ship with the repository, not the package: {path}")
    return json.loads(path.read_text())


CONSTANTS = _load("constants.json")
CLIENT_CASES = _load("client-cases.json")
TOOL_SYNC_CASES = _load("tool-sync-cases.json")
FILE_TOKEN = CLIENT_CASES["token"]


def _normalize(value: Any) -> Any:
    """Round-trip through JSON so dicts, lists and numbers compare however built."""
    return json.loads(json.dumps(value))


# Fake daemon ---------------------------------------------------------------


def _query_string(url: httpx.URL) -> str:
    """The URL's query string as text (httpx exposes it as bytes)."""
    query = url.query
    return query.decode("utf-8") if isinstance(query, bytes) else str(query)


class FakeDaemon:
    """Answers with ``exchanges[i].response`` in order and records each request."""

    def __init__(self, exchanges: list[dict[str, Any]]) -> None:
        self._exchanges = exchanges
        self.requests: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = f"<invalid json: {request.content.decode(errors='replace')}>"
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": _query_string(request.url),
                "session": request.headers.get(rs.SESSION_HEADER, ""),
                "body": body,
                "authorization": request.headers.get("Authorization", ""),
            }
        )
        index = len(self.requests) - 1
        if index >= len(self._exchanges):
            return httpx.Response(500, json={"error": {"code": "unexpected_request"}})
        response = self._exchanges[index]["response"]
        if response.get("stall"):
            raise httpx.ReadTimeout("the daemon stalled", request=request)
        if "raw" in response:
            headers = {"Content-Type": response["content_type"]} if response.get("content_type") else {}
            return httpx.Response(response["status"], content=response["raw"].encode(), headers=headers)
        if "body" in response:
            return httpx.Response(response["status"], json=response["body"])
        return httpx.Response(response["status"])

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


class FakeControlPlane:
    """Answers ``responses[i]`` in order and records each request.

    A response is ``{status, body}`` (JSON), ``{status, raw}`` (verbatim
    bytes) or ``{status}`` (no body); ``{"network_error": true}`` drops the
    connection without answering.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = responses
        self.requests: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "headers": request.headers.copy(),
                "body": request.content,
            }
        )
        index = len(self.requests) - 1
        if index >= len(self._responses):
            return httpx.Response(418)
        response = self._responses[index]
        if response.get("network_error"):
            raise httpx.ConnectError("the connection was dropped", request=request)
        if "raw" in response:
            return httpx.Response(response["status"], content=response["raw"].encode())
        if "body" in response:
            return httpx.Response(response["status"], json=response["body"])
        return httpx.Response(response["status"])

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def _absent_socket_path() -> str:
    """A fresh, short Unix socket path nothing is listening on (macOS caps
    socket paths near 104 bytes, so keep the directory shallow)."""
    directory = tempfile.mkdtemp(prefix="kei-", dir=".")
    return str(Path(directory) / "rt.sock")


# Op dispatch ----------------------------------------------------------------


def _session(args: dict[str, Any]) -> rs.KeiProxySocketSession:
    return rs.KeiProxySocketSession(args["source"], args["external_id"], args["session_id"])


def _release_result(body: dict[str, Any]) -> dict[str, Any]:
    """The raw release body in the fixture's result shape.

    Go's contract runner returns the typed ``ReleaseResult`` (omitempty on
    ``reason_code`` / ``reason`` / ``policy_id`` / ``credential``, always
    ``decision`` and ``bundle_state``); Python returns the raw response body,
    so map it onto that shape.
    """
    result: dict[str, Any] = {"decision": body["decision"], "bundle_state": body.get("bundle_state", "")}
    for key in ("reason_code", "reason", "policy_id"):
        value = body.get(key)
        if isinstance(value, str) and value:
            result[key] = value
    credential = body.get("credential")
    if isinstance(credential, dict) and credential:
        result["credential"] = {
            key: str(credential.get(key, ""))
            for key in ("connector_id", "issuer", "resource", "account_model", "access_token", "expires_at")
        }
    return result


async def _run_op(client: rs.KeiProxySocketAuthorizeClient, op: str, args: dict[str, Any]) -> Any:
    if op == "open_session":
        session = await client.open_session(
            rs.KeiProxySocketIdentity(args["source"], args["external_id"])
        )
        return {"session_id": session.session_id}
    if op == "close_session":
        await client.close_session(_session(args["session"]))
        return None
    if op == "forget_session":
        client.forget_session(rs.KeiProxySocketIdentity(args["source"], args["external_id"]))
        return None
    if op == "authorize":
        request = args["request"]
        return await client.authorize(
            _session(args["session"]),
            tool=request["tool"],
            action=request.get("action", ""),
            resource=request.get("resource", ""),
            span_id=request.get("span_id", ""),
            parent_span=request.get("parent_span", ""),
            delegation_depth=request.get("delegation_depth", 0),
            framework=request.get("framework", ""),
            tool_args_digest=request.get("tool_args_digest", ""),
            connector_id=request.get("connector_id", ""),
            capability=request.get("capability", ""),
        )
    if op == "release_credential":
        request = args["request"]
        body = await client.release_credential(
            _session(args["session"]),
            connector_id=request["connector_id"],
            capability=request["capability"],
            resource=request["resource"],
            tool=request.get("tool", ""),
            span_id=request.get("span_id", ""),
            parent_span=request.get("parent_span", ""),
            delegation_depth=request.get("delegation_depth", 0),
            framework=request.get("framework", ""),
            tool_args_digest=request.get("tool_args_digest", ""),
        )
        return _release_result(body)
    if op == "list_models":
        return await client.list_models(agent_id=args.get("agent_id"), profile_id=args.get("profile_id"))
    if op == "model_config":
        return await client.model_config(agent_id=args.get("agent_id"), profile_id=args.get("profile_id"))
    if op == "list_model_profiles":
        return await client.list_model_profiles(agent_id=args.get("agent_id"))
    if op == "chat_completion":
        return await client.chat_completion(_session(args["session"]), args["payload"])
    if op == "stream_chat_completion":
        return [
            line
            async for line in client.stream_chat_completion(_session(args["session"]), args["payload"])
        ]
    if op == "policy_bundle_status":
        return await client.policy_bundle_status()
    raise AssertionError(f"unknown op {op!r}")


def _assert_error(expected: dict[str, Any], exc: rs.KeiProxySocketError) -> None:
    assert exc.reason_class == expected["reason_class"], (
        f"reason_class {exc.reason_class!r}, contract says {expected['reason_class']!r}: {exc}"
    )
    assert exc.code == expected.get("code", ""), f"code {exc.code!r}, contract says {expected.get('code', '')!r}"
    assert FILE_TOKEN not in str(exc), "the error message must not contain the token"


def _expected_request(recorded: dict[str, Any]) -> dict[str, Any]:
    return {
        "method": recorded["method"],
        "path": recorded["path"],
        "query": recorded["query"],
        "session": recorded["session"],
        "body": recorded["body"],
    }


# Constants ------------------------------------------------------------------


def test_constants_match_the_contract() -> None:
    socket = CONSTANTS["socket"]
    schemas = CONSTANTS["schemas"]
    checks: list[tuple[str, Any, Any]] = [
        ("contract_version", CONSTANTS["contract_version"], 1),
        ("default_path", rs.DEFAULT_RUNTIME_SOCKET_PATH, socket["default_path"]),
        ("path_env", rs.RUNTIME_SOCKET_PATH_ENV, socket["path_env"]),
        ("token_env", BOOTSTRAP_TOKEN_ENV, socket["token_env"]),
        ("session_header", rs.SESSION_HEADER, socket["session_header"]),
        ("session_not_found", rs.SESSION_NOT_FOUND, socket["session_not_found"]),
        ("default_timeout", rs.DEFAULT_SOCKET_TIMEOUT, socket["default_timeout_ms"] / 1000),
        ("default_model_timeout", rs.DEFAULT_MODEL_TIMEOUT, socket["default_model_timeout_ms"] / 1000),
        ("session schema", rs.SESSION_SCHEMA, schemas["session"]),
        ("authorize_request schema", rs.AUTHORIZE_REQUEST_SCHEMA, schemas["authorize_request"]),
        ("authorize_response schema", rs.AUTHORIZE_RESPONSE_SCHEMA, schemas["authorize_response"]),
        (
            "policy_bundle_status schema",
            rs.POLICY_BUNDLE_STATUS_SCHEMA,
            schemas["policy_bundle_status"],
        ),
        (
            "credential_release_request schema",
            rs.CREDENTIAL_RELEASE_REQUEST_SCHEMA,
            schemas["credential_release_request"],
        ),
        (
            "credential_release_response schema",
            rs.CREDENTIAL_RELEASE_RESPONSE_SCHEMA,
            schemas["credential_release_response"],
        ),
    ]
    for name, got, want in checks:
        assert got == want, f"{name} = {got!r}, contract says {want!r}"
    for reason_class in CONSTANTS["socket_error_reason_classes"]:
        assert reason_class in REASON_CLASSES, f"reason class {reason_class!r} is not a known REASON_CLASSES entry"

    tool_sync_constants = CONSTANTS["tool_sync"]
    tool_checks: list[tuple[str, Any, Any]] = [
        ("tools path", tool_sync.TOOLS_PATH, tool_sync_constants["path"]),
        ("media type", tool_sync.TOOL_MANIFEST_V3_MEDIA_TYPE, tool_sync_constants["media_type"]),
        ("control_plane_url_env", RUNTIME_CONTROL_PLANE_URL_ENV, tool_sync_constants["control_plane_url_env"]),
        ("token_env", BOOTSTRAP_TOKEN_ENV, tool_sync_constants["token_env"]),
        ("egress_proxy_env", tool_sync.EGRESS_PROXY_ENV, tool_sync_constants["egress_proxy_env"]),
        ("no_proxy_env", tool_sync.NO_PROXY_ENV, tool_sync_constants["no_proxy_env"]),
        ("default_attempts", tool_sync.DEFAULT_ATTEMPTS, tool_sync_constants["default_attempts"]),
        (
            "default_backoff_ms",
            tool_sync.DEFAULT_BACKOFF_SECONDS * 1000,
            tool_sync_constants["default_backoff_ms"],
        ),
        ("max_backoff_ms", tool_sync.MAX_BACKOFF_SECONDS * 1000, tool_sync_constants["max_backoff_ms"]),
        (
            "default_timeout_ms",
            tool_sync.DEFAULT_TIMEOUT_SECONDS * 1000,
            tool_sync_constants["default_timeout_ms"],
        ),
    ]
    for name, got, want in tool_checks:
        assert got == want, f"{name} = {got!r}, contract says {want!r}"


# Client cases ----------------------------------------------------------------


@pytest.mark.parametrize("case", CLIENT_CASES["cases"], ids=[c["name"] for c in CLIENT_CASES["cases"]])
async def test_client_case(case: dict[str, Any]) -> None:
    env = case.get("env")
    if env is None:
        env = {BOOTSTRAP_TOKEN_ENV: FILE_TOKEN}
    timeout = case.get("timeout_ms", CONSTANTS["socket"]["default_timeout_ms"]) / 1000.0

    daemon: FakeDaemon | None = None
    directory = ""
    try:
        if case.get("daemon") == "absent":
            directory = Path(_absent_socket_path()).parent
            client = rs.KeiProxySocketAuthorizeClient(
                socket_path=str(directory / "rt.sock"), env=env, timeout=timeout
            )
        else:
            daemon = FakeDaemon(case.get("exchanges", []))
            client = rs.KeiProxySocketAuthorizeClient(env=env, timeout=timeout, transport=daemon.transport)

        for step in case["steps"]:
            expect = step["expect"]
            try:
                result = await _run_op(client, step["op"], step["args"])
            except rs.KeiProxySocketError as exc:
                if "error" not in expect:
                    raise
                _assert_error(expect["error"], exc)
            except ValueError:
                assert expect.get("invalid_argument"), f"{step['op']}: unexpected caller error"
            else:
                if "error" in expect:
                    pytest.fail(f"{step['op']} returned {result!r}, the contract expects an error")
                if expect.get("invalid_argument"):
                    pytest.fail(f"{step['op']} returned {result!r}, the contract expects a caller error")
                assert _normalize(result) == _normalize(expect["result"]), (
                    f"{step['op']} = {result!r}, contract says {expect['result']!r}"
                )

        if daemon is not None:
            expected = [
                {**exchange["request"], "query": exchange["request"].get("query", "")}
                for exchange in case.get("exchanges", [])
            ]
            recorded = [_expected_request(request) for request in daemon.requests]
            assert recorded == [_normalize(e) for e in expected], (
                f"recorded requests {recorded!r}, contract says {expected!r}"
            )
            for request in daemon.requests:
                assert request["authorization"] == f"Bearer {FILE_TOKEN}", (
                    f"{request['method']} {request['path']}: Authorization is {request['authorization']!r}"
                )
    finally:
        if directory:
            shutil.rmtree(directory, ignore_errors=True)


# Tool-sync cases --------------------------------------------------------------


@pytest.mark.parametrize(
    "case", TOOL_SYNC_CASES["sync_cases"], ids=[c["name"] for c in TOOL_SYNC_CASES["sync_cases"]]
)
def test_tool_sync_case(case: dict[str, Any]) -> None:
    plane = FakeControlPlane(case.get("responses", []))
    sleeps: list[float] = []
    config = tool_sync.ToolManifestSyncConfig(
        control_plane_url="http://127.0.0.1",  # the in-memory transport ignores it
        token=FILE_TOKEN,
        attempts=case.get("attempts", tool_sync.DEFAULT_ATTEMPTS),
        backoff_seconds=case.get("backoff_ms", tool_sync.DEFAULT_BACKOFF_SECONDS * 1000) / 1000,
        expect_tools=case["expect_tools"],
    )
    manifest = case["manifest_text"] if "manifest_text" in case else case["manifest"]
    expect = case["expect"]
    try:
        result = tool_sync.sync_tool_manifest(
            manifest, config, transport=plane.transport, sleep=sleeps.append
        )
    except tool_sync.ToolManifestSyncError as exc:
        assert expect.get("error_contains"), f"unexpected error: {exc}"
        message = str(exc)
        assert expect["error_contains"] in message, (
            f"error {message!r} lacks {expect['error_contains']!r}"
        )
        if expect.get("error_contains_also"):
            assert expect["error_contains_also"] in message, (
                f"error {message!r} lacks {expect['error_contains_also']!r}"
            )
        assert FILE_TOKEN not in message, f"the error leaks the token: {message!r}"
    else:
        assert not expect.get("error_contains"), f"the contract expects an error, got {result!r}"
        assert _normalize(
            {"sent": result.sent, "tool_count": result.tool_count, "counts": result.counts}
        ) == _normalize(expect), f"result {result!r}, contract says {expect!r}"

    assert len(plane.requests) == case["expect_requests"], (
        f"{len(plane.requests)} requests, contract says {case['expect_requests']}"
    )
    expected_request = TOOL_SYNC_CASES["expected_request"]
    for i, request in enumerate(plane.requests):
        assert request["method"] == expected_request["method"], f"request {i}: {request['method']}"
        assert request["path"] == expected_request["path"], f"request {i}: {request['path']}"
        for header, value in expected_request["headers"].items():
            assert request["headers"].get(header) == value, (
                f"request {i} header {header} is {request['headers'].get(header)!r}, contract says {value!r}"
            )
        manifest_document = json.loads(manifest) if isinstance(manifest, str) else manifest
        assert _normalize(json.loads(request["body"])) == _normalize(manifest_document), (
            f"request {i} body is not the manifest"
        )

    want_sleeps = case.get("expect_sleeps_ms", [])
    got_sleeps = [int(delay * 1000) for delay in sleeps]
    assert got_sleeps == want_sleeps, f"sleeps {got_sleeps} ms, contract says {want_sleeps} ms"


@pytest.mark.parametrize(
    "case", TOOL_SYNC_CASES["config_cases"], ids=[c["name"] for c in TOOL_SYNC_CASES["config_cases"]]
)
def test_tool_sync_config_case(case: dict[str, Any]) -> None:
    expect = case["expect"]
    try:
        config = tool_sync.ToolManifestSyncConfig.from_env(case["env"])
    except tool_sync.ToolManifestSyncError as exc:
        assert expect.get("error_contains"), f"unexpected error: {exc}"
        assert expect["error_contains"] in str(exc), (
            f"error {str(exc)!r} lacks {expect['error_contains']!r}"
        )
        return
    assert not expect.get("error_contains"), f"the contract expects an error, got {config!r}"
    assert config.tools_url == expect["tools_url"], (
        f"tools_url {config.tools_url!r}, contract says {expect['tools_url']!r}"
    )
    assert config.egress_proxy == expect["egress_proxy"], (
        f"egress_proxy {config.egress_proxy!r}, contract says {expect['egress_proxy']!r}"
    )
    assert config.no_proxy == expect["no_proxy"], (
        f"no_proxy {config.no_proxy!r}, contract says {expect['no_proxy']!r}"
    )
    assert config.token == FILE_TOKEN, "the token was not read from the environment"


@pytest.mark.parametrize(
    "case",
    TOOL_SYNC_CASES["proxy_cases"],
    ids=[c["layer"] + "/" + c["name"] for c in TOOL_SYNC_CASES["proxy_cases"]],
)
def test_tool_sync_proxy_case(case: dict[str, Any]) -> None:
    if case["layer"] == "ambient":
        pytest.skip("the ambient layer is httpx's own proxy resolution, not tool_sync's (see the fixture README)")
    env = {**case["env"], RUNTIME_CONTROL_PLANE_URL_ENV: case["url"], RUNTIME_TOKEN_ENV: FILE_TOKEN}
    config = tool_sync.ToolManifestSyncConfig.from_env(env)
    assert config.proxy() == case["expect_proxy"], (
        f"proxy {config.proxy()!r}, contract says {case['expect_proxy']!r}"
    )
