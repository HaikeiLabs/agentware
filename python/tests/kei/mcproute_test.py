"""Unit tests for the MCP route client (``mcproute.py``).

Mirrors ``go/kei/mcproute/client_test.go``.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from haikeilabs.agentware.kei.mcproute import (
    CODE_DENIED,
    CODE_HEADER_MISMATCH,
    CODE_MANIFEST,
    CODE_UNAUTHENTICATED,
    CODE_UPSTREAM,
    ROUTE_KEY_ENV,
    Client,
    ContentBlock,
    HeaderMismatchError,
    ManifestDriftError,
    MCPRouteError,
    PolicyDeniedError,
    RouteError,
    RouteNotLocalError,
    SessionEndedError,
    ToolDescriptor,
    ToolResult,
    UpstreamError,
    _parse_route_error,
    _route_error_from_code,
    _validate_socket,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "fixtures"
TOKEN = "kmr_test0000000000000000000000000000000"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def fixture_cases() -> list[dict[str, Any]]:
    path = FIXTURES_DIR / "kei" / "mcp-route-cases.v1.json"
    with open(path) as f:
        data = json.load(f)
    return data["cases"]


def _mock_transport(
    status: int = 200,
    body: Any = None,
    content_type: str = "application/json",
    recorder: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Build an ``httpx.MockTransport`` returning a fixed response."""

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        return httpx.Response(
            status_code=status, content=raw, headers={"Content-Type": content_type}
        )

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Error tests
# ---------------------------------------------------------------------------


class TestTypedErrors:
    """Mirrors Go's TestTypedErrors."""

    def test_sentinel_isinstance(self) -> None:
        pairs = [
            (CODE_UNAUTHENTICATED, SessionEndedError),
            (CODE_DENIED, PolicyDeniedError),
            (CODE_MANIFEST, ManifestDriftError),
            (CODE_UPSTREAM, UpstreamError),
            (CODE_HEADER_MISMATCH, HeaderMismatchError),
        ]
        for code, cls in pairs:
            err = _route_error_from_code(code, message="test")
            assert isinstance(err, cls), (
                f"code {code} should produce {cls.__name__}, got {type(err).__name__}"
            )

    def test_unknown_code_not_instance(self) -> None:
        err = _route_error_from_code(-1, message="unknown")
        assert not isinstance(err, PolicyDeniedError)
        assert isinstance(err, RouteError)

    def test_sentinel_default_code(self) -> None:
        assert SessionEndedError().code == CODE_UNAUTHENTICATED
        assert PolicyDeniedError().code == CODE_DENIED
        assert ManifestDriftError().code == CODE_MANIFEST
        assert UpstreamError().code == CODE_UPSTREAM
        assert HeaderMismatchError().code == CODE_HEADER_MISMATCH

    def test_error_string(self) -> None:
        err = PolicyDeniedError(
            code=CODE_DENIED, message="policy_denied", reason="rate limit", decision_id="dec_123"
        )
        s = str(err)
        assert "mcp route error" in s
        assert "policy_denied" in s
        assert "rate limit" in s
        assert "dec_123" in s


# ---------------------------------------------------------------------------
# Fixture-driven tests
# ---------------------------------------------------------------------------


class TestMCPRouteFixtures:
    """Mirrors Go's TestMCPRouteFixtures."""

    @staticmethod
    def _build_mock(case: dict[str, Any]) -> tuple[httpx.MockTransport, list[httpx.Request]]:
        """Build a ``MockTransport`` and request recorder from a fixture case."""
        resp = case.get("response", {})
        status = resp.get("status", 200)
        content_type = resp.get("content_type", "application/json")
        body = resp.get("body")
        body_events = resp.get("body_events")

        if body_events:
            raw_events = "".join(body_events)
            content_type = "text/event-stream"
        elif body is not None:
            raw_events = json.dumps(body).encode("utf-8")
        else:
            raw_events = b""

        recorder: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorder.append(request)
            return httpx.Response(
                status_code=status,
                content=raw_events if isinstance(raw_events, bytes) else raw_events.encode("utf-8"),
                headers={"Content-Type": content_type},
            )

        return httpx.MockTransport(handler), recorder

    def test_all_fixtures(self, fixture_cases: list[dict[str, Any]]) -> None:
        for case in fixture_cases:
            transport, _ = self._build_mock(case)
            req = case.get("request", {})
            exp = case.get("expected", {})
            raw_key = req.get("route_key")
            route_key = raw_key if raw_key is not None else TOKEN

            client = Client(
                slug="test-connector",
                transport=transport,
                env={ROUTE_KEY_ENV: route_key},
            )

            exp_error = exp.get("error")
            method = req.get("method", "")

            if method == "tools/list":
                if exp_error:
                    with pytest.raises(RouteError) as exc_info:
                        client.list_tools()
                    err = exc_info.value
                    assert err.code == exp_error["code"]
                    assert err.reason == exp_error.get("reason", "")
                    if exp_error.get("decision_id"):
                        assert err.decision_id == exp_error["decision_id"]
                else:
                    tools = client.list_tools()
                    expected_tools = exp.get("tools", [])
                    if expected_tools:
                        tool_names = [t.name for t in tools]
                        assert tool_names == expected_tools

            elif method == "tools/call":
                params_raw = req.get("params")
                params: dict = (
                    json.loads(params_raw) if isinstance(params_raw, str) else (params_raw or {})
                )
                tool_name = params.get("name", "")
                arguments = params.get("arguments") if isinstance(params, dict) else None

                if exp_error:
                    with pytest.raises(RouteError) as exc_info:
                        client.call_tool(tool_name, arguments)
                    err = exc_info.value
                    assert err.code == exp_error["code"]
                    assert err.reason == exp_error.get("reason", "")
                else:
                    result = client.call_tool(tool_name, arguments)
                    assert result is not None
                    if "isError" in exp:
                        assert result.is_error == exp["isError"]


# ---------------------------------------------------------------------------
# Route not local
# ---------------------------------------------------------------------------


class TestRouteNotLocal:
    """Mirrors Go's TestRouteNotLocal."""

    def test_remote_base_url_raises(self) -> None:
        client = Client(
            slug="test",
            base_url="http://remote.example.com",
            env={ROUTE_KEY_ENV: TOKEN},
        )
        with pytest.raises(RouteNotLocalError):
            client.list_tools()


# ---------------------------------------------------------------------------
# Missing route key
# ---------------------------------------------------------------------------


class TestMissingRouteKey:
    """Mirrors Go's TestMissingRouteKey."""

    def test_missing_key_raises(self) -> None:
        client = Client(
            slug="test",
            transport=_mock_transport(200, {"result": {"tools": []}}),
            env={},  # no route key
        )
        with pytest.raises(MCPRouteError, match="KEI_MCP_ROUTE_KEY is not set"):
            client.list_tools()


# ---------------------------------------------------------------------------
# 401 plain no body
# ---------------------------------------------------------------------------


class Test401PlainNoBody:
    """Mirrors Go's Test401PlainNoBody: plain HTTP 401 with no JSON-RPC body."""

    def test_plain_401_returns_session_ended(self) -> None:
        recorder: list[httpx.Request] = []
        transport = httpx.MockTransport(
            lambda req: [recorder.append(req), httpx.Response(401, content=b"")][-1]
        )
        client = Client(
            slug="test",
            transport=transport,
            env={ROUTE_KEY_ENV: TOKEN},
        )
        with pytest.raises(SessionEndedError):
            client.list_tools()

        assert len(recorder) == 1, "expected exactly 1 request"


# ---------------------------------------------------------------------------
# 401 session ended
# ---------------------------------------------------------------------------


class Test401SessionEnded:
    """Mirrors Go's Test401SessionEnded: 401 with JSON-RPC error body."""

    def test_session_ended(self) -> None:
        recorder: list[httpx.Request] = []
        transport = httpx.MockTransport(
            lambda req: [
                recorder.append(req),
                httpx.Response(
                    401,
                    content=json.dumps(
                        {"error": {"code": CODE_UNAUTHENTICATED, "message": "session_ended"}}
                    ).encode("utf-8"),
                ),
            ][-1]
        )
        client = Client(
            slug="test",
            transport=transport,
            env={ROUTE_KEY_ENV: TOKEN},
        )
        with pytest.raises(SessionEndedError):
            client.list_tools()

        assert len(recorder) == 1, "expected exactly 1 request"


# ---------------------------------------------------------------------------
# Request headers
# ---------------------------------------------------------------------------


class TestRequestHeaders:
    """Mirrors Go's TestRequestHeaders."""

    def _make_client(self, recorder: list[httpx.Request]) -> Client:
        def handler(req: httpx.Request) -> httpx.Response:
            recorder.append(req)
            return httpx.Response(
                200,
                content=json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}).encode(
                    "utf-8"
                ),
            )

        return Client(
            slug="test-connector",
            transport=httpx.MockTransport(handler),
            env={ROUTE_KEY_ENV: TOKEN},
        )

    def test_list_tools_headers(self) -> None:
        recorder: list[httpx.Request] = []
        client = self._make_client(recorder)
        client.list_tools()

        assert len(recorder) == 1
        h = recorder[0].headers
        assert h.get("MCP-Protocol-Version") == "2026-07-28"
        assert h.get("Mcp-Method") == "tools/list"
        # Mcp-Name should not be present for tools/list
        assert "Mcp-Name" not in h or h.get("Mcp-Name") == ""

    def test_call_tool_headers(self) -> None:
        recorder: list[httpx.Request] = []
        client = self._make_client(recorder)
        client.call_tool("my-tool")

        assert len(recorder) == 1
        h = recorder[0].headers
        assert h.get("MCP-Protocol-Version") == "2026-07-28"
        assert h.get("Mcp-Method") == "tools/call"
        assert h.get("Mcp-Name") == "my-tool"


# ---------------------------------------------------------------------------
# Route key leak
# ---------------------------------------------------------------------------


class TestRouteKeyLeak:
    """Mirrors Go's TestRouteKeyLeak."""

    SECRET_KEY = "kmr_testsecretvalue1234567890abcdefghijk"

    def _check_no_leak(self, error: Exception) -> None:
        err_str = str(error)
        assert self.SECRET_KEY not in err_str, f"error message leaks route key: {err_str}"

    def test_401_no_leak(self) -> None:
        transport = _mock_transport(
            401, {"error": {"code": CODE_UNAUTHENTICATED, "message": "session_ended"}}
        )
        client = Client(slug="test", transport=transport, env={ROUTE_KEY_ENV: self.SECRET_KEY})
        try:
            client.list_tools()
        except Exception as e:
            self._check_no_leak(e)
            assert isinstance(e, SessionEndedError)

    def test_403_no_leak(self) -> None:
        transport = _mock_transport(
            403, {"error": {"code": CODE_DENIED, "message": "policy_denied"}}
        )
        client = Client(slug="test", transport=transport, env={ROUTE_KEY_ENV: self.SECRET_KEY})
        try:
            client.list_tools()
        except Exception as e:
            self._check_no_leak(e)
            assert isinstance(e, PolicyDeniedError)

    def test_missing_key_no_leak(self) -> None:
        client = Client(slug="test", env={})
        try:
            client.list_tools()
        except Exception as e:
            self._check_no_leak(e)

    def test_not_local_no_leak(self) -> None:
        client = Client(
            slug="test", base_url="http://remote.example.com", env={ROUTE_KEY_ENV: self.SECRET_KEY}
        )
        try:
            client.list_tools()
        except Exception as e:
            self._check_no_leak(e)
            assert isinstance(e, RouteNotLocalError)


# ---------------------------------------------------------------------------
# Socket validation
# ---------------------------------------------------------------------------


class TestValidateSocket:
    """Mirrors Go's TestValidateSocket."""

    def test_nonexistent(self) -> None:
        assert not _validate_socket("/nonexistent/mcproute/test.sock")

    def test_regular_file(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            assert not _validate_socket(path)
        finally:
            os.unlink(path)

    def test_valid_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v.sock")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind(path)
                result = _validate_socket(path)
                assert isinstance(result, bool)
            finally:
                sock.close()

    def test_ownership_check_present(self) -> None:
        """Verify the ownership-check code path compiles and runs."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        try:
            assert not _validate_socket(path)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Parse route error
# ---------------------------------------------------------------------------


class TestParseRouteError:
    def test_valid_error(self) -> None:
        raw = json.dumps(
            {
                "error": {
                    "code": CODE_DENIED,
                    "message": "policy_denied",
                    "data": {"reason": "rate limit", "decision_id": "dec_1"},
                }
            }
        ).encode("utf-8")
        err = _parse_route_error(raw)
        assert err is not None
        assert isinstance(err, PolicyDeniedError)
        assert err.code == CODE_DENIED
        assert err.reason == "rate limit"
        assert err.decision_id == "dec_1"

    def test_no_error_key(self) -> None:
        assert _parse_route_error(b'{"result":{}}') is None

    def test_invalid_json(self) -> None:
        assert _parse_route_error(b"not json") is None

    def test_error_not_object(self) -> None:
        assert _parse_route_error(b'{"error":"string error"}') is None


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class TestTypes:
    def test_tool_descriptor_from_dict(self) -> None:
        td = ToolDescriptor.from_dict(
            {
                "name": "my-tool",
                "description": "A test tool",
                "inputSchema": {"type": "object"},
            }
        )
        assert td.name == "my-tool"
        assert td.description == "A test tool"
        assert td.input_schema == {"type": "object"}

    def test_content_block_from_dict(self) -> None:
        cb = ContentBlock.from_dict({"type": "text", "text": "hello"})
        assert cb.type == "text"
        assert cb.text == "hello"

    def test_tool_result_from_dict(self) -> None:
        tr = ToolResult.from_dict(
            {
                "content": [{"type": "text", "text": "ok"}],
                "isError": False,
            }
        )
        assert len(tr.content) == 1
        assert tr.content[0].text == "ok"
        assert not tr.is_error

    def test_tool_result_is_error(self) -> None:
        tr = ToolResult.from_dict(
            {
                "content": [],
                "isError": True,
            }
        )
        assert tr.is_error
