"""A client for the kei-proxy MCP route over its Unix socket or loopback TCP.

``kei-proxy`` exposes an MCP-compatible ``/mcp/{slug}`` JSON-RPC endpoint on
its Unix socket. This module is the Python counterpart of the Go
``go/kei/mcproute/client.go`` and covers ``tools/list`` and ``tools/call``.

Transport order: Unix socket → 127.0.0.1:8080 → [::1]:8080. A non-loopback
``base_url`` raises :class:`RouteNotLocalError` — the client never dials a
remote host.

The route key (``KEI_MCP_ROUTE_KEY``) is read from the environment on every
request and is never cached, never logged, and never leaked into error messages.
"""

from __future__ import annotations

import json
import os
import stat as _stat
import time
from typing import Any, ClassVar

import httpx

__all__ = [
    "DEFAULT_LOOPBACK_PORT",
    "DEFAULT_SSE_TIMEOUT",
    "DEFAULT_TIMEOUT",
    "MCP_ROUTE_PREFIX",
    "ROUTE_KEY_ENV",
    "SOCKET_PATH_ENV",
    "DEFAULT_SOCKET_PATH",
    "CODE_UNAUTHENTICATED",
    "CODE_DENIED",
    "CODE_MANIFEST",
    "CODE_UPSTREAM",
    "CODE_HEADER_MISMATCH",
    "MCPRouteError",
    "RouteNotLocalError",
    "RouteError",
    "SessionEndedError",
    "PolicyDeniedError",
    "ManifestDriftError",
    "UpstreamError",
    "HeaderMismatchError",
    "Client",
    "ContentBlock",
    "ToolDescriptor",
    "ToolResult",
    "MAX_RESPONSE_BYTES",
]

# --- constants ---

DEFAULT_SOCKET_PATH = "/run/kei-proxy/runtime.sock"
SOCKET_PATH_ENV = "KEI_RUNTIME_SOCKET_PATH"
ROUTE_KEY_ENV = "KEI_MCP_ROUTE_KEY"
DEFAULT_LOOPBACK_PORT = "8080"
MCP_ROUTE_PREFIX = "/mcp/"

DEFAULT_TIMEOUT = 10.0
DEFAULT_SSE_TIMEOUT = 120.0
MAX_RESPONSE_BYTES = 8 << 20

# JSON-RPC error codes from kei-connector-runtime/internal/mcproute/errors.go
CODE_UNAUTHENTICATED = -31401
CODE_DENIED = -31403
CODE_MANIFEST = -31409
CODE_UPSTREAM = -31502
CODE_HEADER_MISMATCH = -32020

# Placeholder host used for socket and transport modes (ignored by the transport).
_BASE_URL = "http://kei-mcp"


# --- errors ---


class MCPRouteError(Exception):
    """Base error for MCP route operations."""


class RouteNotLocalError(MCPRouteError):
    """The resolved transport address is not a loopback or Unix socket.

    The client never dials a remote host.
    """


class RouteError(MCPRouteError):
    """A structured JSON-RPC error from the kei-proxy MCP route.

    Subclasses represent specific error codes. Use the factory function
    ``_route_error_from_code`` to construct the correct subclass from a
    JSON-RPC error code.
    """

    _default_code: ClassVar[int] = 0

    def __init__(
        self,
        code: int = 0,
        message: str = "",
        reason: str = "",
        decision_id: str = "",
    ) -> None:
        if code == 0 and self._default_code != 0:
            code = self._default_code
        self.code = code
        self.message = message
        self.reason = reason
        self.decision_id = decision_id
        super().__init__(str(self))

    def __str__(self) -> str:
        s = f"mcp route error {self.code} ({self.message})"
        if self.reason:
            s += f": {self.reason}"
            if self.decision_id:
                s += f" [{self.decision_id}]"
        return s

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code}, message={self.message!r}, "
            f"reason={self.reason!r}, decision_id={self.decision_id!r})"
        )


class SessionEndedError(RouteError):
    """HTTP 401: the route key expired or was revoked. Never retry."""

    _default_code = CODE_UNAUTHENTICATED


class PolicyDeniedError(RouteError):
    """The policy evaluator denied the tool call (JSON-RPC code -31403)."""

    _default_code = CODE_DENIED


class ManifestDriftError(RouteError):
    """Connector manifest no longer matches the approved listing (-31409)."""

    _default_code = CODE_MANIFEST


class UpstreamError(RouteError):
    """Upstream MCP server unreachable or returned an error (-31502)."""

    _default_code = CODE_UPSTREAM


class HeaderMismatchError(RouteError):
    """MCP headers do not match the JSON-RPC method (-32020)."""

    _default_code = CODE_HEADER_MISMATCH


_CODE_MAP: dict[int, type[RouteError]] = {
    CODE_UNAUTHENTICATED: SessionEndedError,
    CODE_DENIED: PolicyDeniedError,
    CODE_MANIFEST: ManifestDriftError,
    CODE_UPSTREAM: UpstreamError,
    CODE_HEADER_MISMATCH: HeaderMismatchError,
}


def _route_error_from_code(
    code: int,
    message: str = "",
    reason: str = "",
    decision_id: str = "",
) -> RouteError:
    """Create the appropriate :class:`RouteError` subclass for a JSON-RPC code.

    If the code is not recognised, a plain :class:`RouteError` is returned.
    """
    cls = _CODE_MAP.get(code, RouteError)
    return cls(code=code, message=message, reason=reason, decision_id=decision_id)


# --- types ---


class ToolDescriptor:
    """Describes an MCP tool."""

    def __init__(
        self, name: str, description: str = "", input_schema: dict[str, Any] | None = None
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema or {}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ToolDescriptor:
        return cls(
            name=d.get("name", ""),
            description=d.get("description", ""),
            input_schema=d.get("inputSchema") or d.get("input_schema", {}),
        )

    def __repr__(self) -> str:
        return f"ToolDescriptor(name={self.name!r})"


class ContentBlock:
    """A single piece of content in a tool result."""

    def __init__(self, type: str, text: str = "") -> None:
        self.type = type
        self.text = text

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ContentBlock:
        return cls(type=d.get("type", ""), text=d.get("text", ""))

    def __repr__(self) -> str:
        return f"ContentBlock(type={self.type!r})"


class ToolResult:
    """The response from ``tools/call``."""

    def __init__(self, content: list[ContentBlock] | None = None, is_error: bool = False) -> None:
        self.content = content or []
        self.is_error = is_error

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ToolResult:
        raw_content = d.get("content", [])
        if isinstance(raw_content, list):
            content = [
                ContentBlock.from_dict(c)
                if isinstance(c, dict)
                else ContentBlock(type="text", text=str(c))
                for c in raw_content
            ]
        else:
            content = []
        return cls(content=content, is_error=bool(d.get("isError", False)))

    def __repr__(self) -> str:
        return f"ToolResult(content={self.content!r}, is_error={self.is_error})"


# --- socket validation ---


def _validate_socket(path: str) -> bool:
    """Check whether *path* exists, is a Unix socket, and is safe to dial.

    Returns ``True`` when:
    - exists and is a socket (``S_ISSOCK``),
    - is owned by the current ``uid``, **or** is not group/world-writable.

    Returns ``False`` when any check fails.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not _stat.S_ISSOCK(st.st_mode):
        return False
    try:
        own_uid = os.getuid()
    except AttributeError:
        return True  # non-Unix: skip ownership check
    if st.st_uid == own_uid:
        return True
    # Not owned by us: reject if group (0020) or world (0002) writable.
    if st.st_mode & 0o022:
        return False
    return True


def _is_loopback(host: str) -> bool:
    """Check whether *host* is a loopback address."""
    from ipaddress import ip_address

    try:
        addr = ip_address(host)
        return addr.is_loopback
    except ValueError:
        return False


# --- client ---


class Client:
    """Synchronous client for the kei-proxy MCP route.

    Talks to the MCP route over the kei-proxy Unix socket (default), or
    loopback TCP as a fallback. The route key (``KEI_MCP_ROUTE_KEY``) is read
    from the environment on **every** call, never cached.

    Args:
        slug: The MCP connector slug (e.g. ``"github-connector"``). Required.
        base_url: When set, bypasses socket/loopback discovery and sends
            requests to this URL directly. The host must be loopback.
        socket_path: The kei-proxy Unix socket path. Defaults to
            ``$KEI_RUNTIME_SOCKET_PATH``, then ``/run/kei-proxy/runtime.sock``.
        timeout: Timeout for JSON-RPC calls in seconds (default 10).
        sse_timeout: Per-line timeout for SSE responses in seconds (default 120).
        env: Where ``KEI_MCP_ROUTE_KEY`` and ``KEI_RUNTIME_SOCKET_PATH`` are
            read. ``None`` (default) means ``os.environ``.
        transport: Replaces the default transport (tests). When set, ``base_url``
            validation is skipped and ``_BASE_URL`` (``http://kei-mcp``) is used.
    """

    def __init__(
        self,
        slug: str,
        base_url: str = "",
        socket_path: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        sse_timeout: float = DEFAULT_SSE_TIMEOUT,
        env: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not slug:
            raise ValueError("slug is required")
        self._slug = slug
        self._base_url = base_url
        self._socket_path = socket_path
        self._timeout = timeout
        self._sse_timeout = sse_timeout
        self._env = env  # None means os.environ
        self._transport = transport

    @property
    def slug(self) -> str:
        return self._slug

    @property
    def socket_path(self) -> str:
        source = os.environ if self._env is None else self._env
        return self._socket_path or source.get(SOCKET_PATH_ENV, "")

    # --- helpers ---

    def _lookup(self, key: str) -> str:
        source = os.environ if self._env is None else self._env
        return source.get(key, "")

    def _build_url(self) -> str:
        from urllib.parse import quote

        return f"{MCP_ROUTE_PREFIX}{quote(self._slug, safe='')}"

    # --- transport / send ---

    def _send(
        self,
        url_path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> httpx.Response:
        """Send a single HTTP POST request.

        Transport selection order:
        1. Injected ``transport`` (test mode).
        2. ``base_url`` (validate loopback; used directly).
        3. Discovery: Unix socket → 127.0.0.1:8080 → [::1]:8080.
        """
        # --- 1. Injected transport (test mode) ---
        if self._transport is not None:
            with httpx.Client(
                base_url=_BASE_URL,
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                try:
                    return client.post(url_path, content=body, headers=headers)
                except httpx.HTTPError as exc:
                    raise MCPRouteError(f"mcp route: transport: {exc}") from exc

        # --- 2. Base URL mode ---
        if self._base_url:
            from urllib.parse import urlparse

            parsed = urlparse(self._base_url)
            host = parsed.hostname or ""
            if host and not _is_loopback(host) and parsed.scheme != "unix":
                raise RouteNotLocalError("mcp route: the route host is not a loopback address")
            with httpx.Client(
                base_url=self._base_url.rstrip("/"),
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                try:
                    return client.post(url_path, content=body, headers=headers)
                except httpx.HTTPError as exc:
                    raise MCPRouteError(f"mcp route: transport: {exc}") from exc

        # --- 3. Discovery: socket → IPv4 → IPv6 ---
        socket_path = self.socket_path
        last_error: Exception | None = None

        if socket_path and _validate_socket(socket_path):
            transport = httpx.HTTPTransport(uds=socket_path)
            try:
                with httpx.Client(
                    base_url=_BASE_URL,
                    transport=transport,
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as client:
                    return client.post(url_path, content=body, headers=headers)
            except (httpx.ConnectError, httpx.RemoteProtocolError, OSError) as exc:
                last_error = exc

        port = DEFAULT_LOOPBACK_PORT
        for host in ("127.0.0.1", "::1"):
            url = f"http://{host}:{port}{url_path}"
            try:
                with httpx.Client(
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as client:
                    return client.post(url, content=body, headers=headers)
            except (httpx.ConnectError, httpx.RemoteProtocolError, OSError) as exc:
                last_error = exc

        if last_error:
            raise MCPRouteError(f"mcp route: transport: {last_error}") from last_error
        raise MCPRouteError("mcp route: no transport available")

    # --- JSON-RPC call ---

    def call(
        self,
        method: str,
        tool_name: str = "",
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Send a JSON-RPC request to the MCP route.

        Args:
            method: The JSON-RPC method (e.g. ``"tools/list"``).
            tool_name: The tool name for ``"tools/call"`` (sent as ``Mcp-Name``
                header). Empty for other methods.
            params: The JSON-RPC params, or ``None``.

        Returns:
            The JSON-RPC ``result`` value (parsed JSON).

        Raises:
            RouteNotLocalError: *base_url* points to a non-loopback host.
            SessionEndedError: HTTP 401 (route key expired or revoked).
            PolicyDeniedError: JSON-RPC error code -31403.
            ManifestDriftError: JSON-RPC error code -31409.
            UpstreamError: JSON-RPC error code -31502.
            HeaderMismatchError: JSON-RPC error code -32020.
            RouteError: Other JSON-RPC errors.
            MCPRouteError: Transport or protocol errors.
        """
        route_key = self._lookup(ROUTE_KEY_ENV)
        if not route_key:
            raise MCPRouteError(f"mcp route: {ROUTE_KEY_ENV} is not set")

        request_id = int(time.time() * 1000)
        request_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            },
        ).encode("utf-8")

        url_path = self._build_url()

        headers: dict[str, str] = {
            "Authorization": f"Bearer {route_key}",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": method,
        }
        if tool_name:
            headers["Mcp-Name"] = tool_name

        response = self._send(url_path, request_body, headers)

        # --- status code handling (matches Go) ---

        if response.status_code == 401:
            re = _parse_route_error(response.content)
            if re is not None:
                raise re
            raise SessionEndedError(
                code=CODE_UNAUTHENTICATED,
                message="session ended — the route key expired or was revoked",
            )

        if response.status_code == 403:
            re = _parse_route_error(response.content)
            if re is not None:
                raise re
            raise MCPRouteError(f"mcp route: HTTP {response.status_code}")

        # SSE handling.
        ct = response.headers.get("Content-Type", "")
        if "text/event-stream" in ct:
            return self._read_sse(response.content, request_id)

        raw = response.content[:MAX_RESPONSE_BYTES]

        if response.status_code < 200 or response.status_code > 299:
            re = _parse_route_error(raw)
            if re is not None:
                raise re
            raise MCPRouteError(f"mcp route: HTTP {response.status_code}")

        # Decode JSON-RPC envelope.
        try:
            rpc = json.loads(raw)
        except json.JSONDecodeError:
            raise MCPRouteError("mcp route: decode response: invalid JSON")

        if not isinstance(rpc, dict):
            raise MCPRouteError("mcp route: decode response: not a JSON object")

        if "error" in rpc:
            err = rpc["error"]
            if isinstance(err, dict):
                code = err.get("code", 0)
                msg = err.get("message", "")
                data = err.get("data")
                reason = ""
                decision_id = ""
                if isinstance(data, dict):
                    reason = data.get("reason", "")
                    decision_id = data.get("decision_id", "")
                raise _route_error_from_code(
                    code=code,
                    message=msg,
                    reason=reason,
                    decision_id=decision_id,
                )
            raise RouteError(message=str(err))

        return rpc.get("result")

    # --- SSE reader ---

    def _read_sse(self, body: bytes, expected_id: int) -> Any:
        """Read a ``text/event-stream`` and return the first response event's data."""
        data_buf: list[str] = []
        for line in body.decode("utf-8", errors="replace").split("\n"):
            line_stripped = line.strip()
            if line_stripped.startswith("data:"):
                data_buf.append(line_stripped[5:].strip())
            elif line_stripped == "" and data_buf:
                event_data = "".join(data_buf)
                try:
                    msg = json.loads(event_data)
                except json.JSONDecodeError:
                    data_buf = []
                    continue
                if isinstance(msg, dict) and msg.get("id") is not None:
                    return msg.get("result")
                data_buf = []
        raise MCPRouteError("mcp route: sse stream ended without a response")

    # --- high-level APIs ---

    def list_tools(self) -> list[ToolDescriptor]:
        """Call ``tools/list`` and return the tool descriptors."""
        result = self.call("tools/list")
        if not isinstance(result, dict):
            raise MCPRouteError("mcp route: decode tools/list result: expected an object")
        tools_raw = result.get("tools", [])
        if not isinstance(tools_raw, list):
            raise MCPRouteError("mcp route: decode tools/list result: expected a tools array")
        return [ToolDescriptor.from_dict(t) for t in tools_raw]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Call ``tools/call`` and return the tool result."""
        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = arguments
        result = self.call("tools/call", tool_name=name, params=params)
        if not isinstance(result, dict):
            raise MCPRouteError("mcp route: decode tools/call result: expected an object")
        return ToolResult.from_dict(result)


# --- internal helpers ---


def _parse_route_error(raw: bytes) -> RouteError | None:
    """Attempt to parse a JSON-RPC error response from *raw*.

    Returns a ``RouteError`` (or subclass) when the body contains an ``error``
    key, or ``None``.
    """
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if not isinstance(err, dict):
        return None
    code = err.get("code", 0)
    message = err.get("message", "")
    data = err.get("data")
    reason = ""
    decision_id = ""
    if isinstance(data, dict):
        reason = data.get("reason", "")
        decision_id = data.get("decision_id", "")
    return _route_error_from_code(
        code=code, message=message, reason=reason, decision_id=decision_id
    )
