"""A client for the ``kei-proxy serve`` daemon over its Unix socket.

``kei-proxy serve`` listens on a Unix socket (``KEI_RUNTIME_SOCKET_PATH``,
default ``/run/kei-proxy/runtime.sock``). This module is the Python
counterpart of the TypeScript SDK's ``KeiProxySocketAuthorizeClient``
(``typescript/src/kei/socketAuthorizeClient.ts``) and covers the routes a
harness needs:

- ``PUT`` / ``DELETE /v1/session``: open and close a named runtime session
  (``kei.session/v2``) for an explicit ``source`` / ``external_id`` identity.
  The daemon resolves the Kei user from that identity; requests in the
  session carry ``X-Kei-Session`` and never name the user themselves.
- ``POST /v1/authorize``: one ``kei.authorize-request/v1`` decision.
- ``GET /v1/models`` and ``POST /v1/chat/completions``: the model routes,
  each call made inside a session so the daemon authorizes and audits
  ``model.invoke`` for the session's user.
- ``GET /v1/policy-bundle/status``: which bundle the daemon holds.

Fail-closed contract: an unreachable daemon, a timeout, a non-2xx answer, a
body that is not a JSON object, or a response with the wrong schema raises
:class:`KeiProxySocketError`. It subclasses
:class:`~pedro_agentware.kei.evaluator.KeiProxyAuthorizeError`, so
:class:`~pedro_agentware.kei.KeiProxyEvaluator` turns every one of them into a
DENY with the matching reason class. A session the daemon no longer knows
(``session_not_found``) is reopened once; a second miss fails closed.

Security contract: requests carry the runtime token (``KEI_RUNTIME_TOKEN``) in
the ``Authorization`` header and nowhere else. It is read per request, never
stored on the client, and never appears in a log line, an error message or a
repr. Errors carry only the daemon's error *code*: daemon messages and
response bodies are never copied into them.
"""

import asyncio
import concurrent.futures
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx

from .auth import BOOTSTRAP_TOKEN_ENV
from .evaluator import AuthorizationClient, KeiProxyAuthorizeError

__all__ = [
    "AUTHORIZE_REQUEST_SCHEMA",
    "AUTHORIZE_RESPONSE_SCHEMA",
    "DEFAULT_MODEL_TIMEOUT",
    "DEFAULT_RUNTIME_SOCKET_PATH",
    "DEFAULT_SOCKET_TIMEOUT",
    "POLICY_BUNDLE_STATUS_SCHEMA",
    "RUNTIME_SOCKET_PATH_ENV",
    "SESSION_HEADER",
    "SESSION_NOT_FOUND",
    "SESSION_SCHEMA",
    "KeiProxySocketAuthorizationClient",
    "KeiProxySocketAuthorizeClient",
    "KeiProxySocketError",
    "KeiProxySocketIdentity",
    "KeiProxySocketSession",
    "select_authorization_client",
]

DEFAULT_RUNTIME_SOCKET_PATH = "/run/kei-proxy/runtime.sock"
RUNTIME_SOCKET_PATH_ENV = "KEI_RUNTIME_SOCKET_PATH"
SESSION_HEADER = "X-Kei-Session"
SESSION_SCHEMA = "kei.session/v2"
AUTHORIZE_REQUEST_SCHEMA = "kei.authorize-request/v1"
AUTHORIZE_RESPONSE_SCHEMA = "kei.authorize-response/v1"
POLICY_BUNDLE_STATUS_SCHEMA = "kei.policy-bundle-status/v1"
SESSION_NOT_FOUND = "session_not_found"

DEFAULT_SOCKET_TIMEOUT = 10.0
DEFAULT_MODEL_TIMEOUT = 120.0

# The Unix-socket transport ignores the host; it only has to be a valid URL.
_BASE_URL = "http://kei-proxy"

_T = TypeVar("_T")


class KeiProxySocketError(KeiProxyAuthorizeError):
    """A daemon call that produced no usable answer. Callers fail closed.

    ``reason_class`` is one of
    :data:`~pedro_agentware.kei.evaluator.REASON_CLASSES`. ``code`` is the
    daemon's error code (``{"error": {"code": ...}}``) when it sent one, e.g.
    ``invalid_runtime_token`` or ``session_not_found``.
    """

    def __init__(self, reason_class: str, detail: str, code: str = "") -> None:
        super().__init__(reason_class, detail)
        self.code = code


@dataclass(frozen=True)
class KeiProxySocketIdentity:
    """Who a runtime session is for: a harness ``source`` and its user id there."""

    source: str
    external_id: str


@dataclass(frozen=True)
class KeiProxySocketSession:
    """A named daemon session, as ``PUT /v1/session`` returned it."""

    source: str
    external_id: str
    session_id: str

    @property
    def identity(self) -> KeiProxySocketIdentity:
        return KeiProxySocketIdentity(self.source, self.external_id)


class KeiProxySocketAuthorizeClient:
    """Async client for the kei-proxy daemon's Unix socket.

    One session is cached per ``(source, external_id)``, as in the TypeScript
    SDK. Every method raises :class:`KeiProxySocketError` instead of returning
    a partial answer.
    """

    def __init__(
        self,
        socket_path: str | None = None,
        timeout: float = DEFAULT_SOCKET_TIMEOUT,
        model_timeout: float = DEFAULT_MODEL_TIMEOUT,
        env: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Build a client.

        Args:
            socket_path: The daemon's socket. Defaults to
                ``$KEI_RUNTIME_SOCKET_PATH``, then
                :data:`DEFAULT_RUNTIME_SOCKET_PATH`.
            timeout: Seconds for session, authorize and status calls.
            model_timeout: Seconds for model calls.
            env: Where ``KEI_RUNTIME_TOKEN`` and the socket path are read.
                Defaults to ``os.environ``; the token is read on every request.
            transport: Replaces the Unix-socket transport (tests).
        """
        source = os.environ if env is None else env
        self._socket_path = socket_path or source.get(RUNTIME_SOCKET_PATH_ENV) or (
            DEFAULT_RUNTIME_SOCKET_PATH
        )
        self._timeout = timeout
        self._model_timeout = model_timeout
        self._env = env
        self._transport = transport
        self._sessions: dict[tuple[str, str], KeiProxySocketSession] = {}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(socket_path={self._socket_path!r})"

    @property
    def socket_path(self) -> str:
        return self._socket_path

    # Sessions -------------------------------------------------------------

    async def open_session(self, identity: KeiProxySocketIdentity) -> KeiProxySocketSession:
        """Create, or reuse, the daemon session for ``identity``."""
        source, external_id = identity.source, identity.external_id
        if not isinstance(source, str) or not source.strip():
            raise ValueError("a socket session needs a non-empty source")
        if not isinstance(external_id, str) or not external_id.strip():
            raise ValueError("a socket session needs a non-empty external_id")
        key = (source, external_id)
        cached = self._sessions.get(key)
        if cached is not None:
            return cached
        body = _json_object(
            await self._send(
                "PUT",
                "/v1/session",
                json_body={"schema": SESSION_SCHEMA, "source": source, "external_id": external_id},
            )
        )
        session_id = body.get("session_id")
        if body.get("schema") != SESSION_SCHEMA or not isinstance(session_id, str) or not session_id:
            raise KeiProxySocketError("malformed_response", "the daemon returned an invalid session")
        session = KeiProxySocketSession(source, external_id, session_id)
        self._sessions[key] = session
        return session

    async def close_session(self, session: KeiProxySocketSession) -> None:
        """End ``session`` on the daemon. A session the daemon already ended is not an error."""
        self._forget(session)
        response = await self._send("DELETE", "/v1/session", session_id=session.session_id)
        if response.status_code == 404 and _error_code(response) == SESSION_NOT_FOUND:
            return
        _check_status(response)

    def forget_session(self, identity: KeiProxySocketIdentity) -> None:
        """Drop the cached session for ``identity``; the next call opens a new one."""
        self._sessions.pop((identity.source, identity.external_id), None)

    # Authorize ------------------------------------------------------------

    async def authorize(
        self,
        session: KeiProxySocketSession,
        *,
        tool: str,
        action: str = "",
        resource: str = "",
        span_id: str = "",
        parent_span: str = "",
        delegation_depth: int = 0,
        agent_id: str = "",
        framework: str = "",
        tool_args_digest: str = "",
    ) -> dict[str, Any]:
        """Ask the daemon for one decision in ``session``.

        Returns the ``kei.authorize-response/v1`` object (``decision`` plus
        ``reason_code``, ``reason``, ``policy_id``, ``enrollment`` ...). Only
        ``permit``/``allow`` is an allow; a ``deny`` is returned, not raised.
        The request names no user: the daemon decides for the session's
        subject.
        """
        if not session.source or not session.external_id or not session.session_id:
            raise ValueError("socket authorize needs an explicit named session")
        request: dict[str, Any] = {"schema": AUTHORIZE_REQUEST_SCHEMA, "tool": tool}
        optional: dict[str, Any] = {
            "action": action,
            "resource": resource,
            "span_id": span_id,
            "parent_span": parent_span,
            "delegation_depth": delegation_depth,
            "agent_id": agent_id,
            "framework": framework,
            "tool_args_digest": tool_args_digest,
        }
        request.update({k: v for k, v in optional.items() if v})

        for attempt in range(2):
            try:
                response = _json_object(
                    await self._send(
                        "POST", "/v1/authorize", session_id=session.session_id, json_body=request
                    )
                )
            except KeiProxySocketError as exc:
                if attempt == 0 and exc.code == SESSION_NOT_FOUND:
                    session = await self._reopen(session)
                    continue
                raise
            if response.get("schema") != AUTHORIZE_RESPONSE_SCHEMA or not isinstance(
                response.get("decision"), str
            ):
                raise KeiProxySocketError(
                    "malformed_response", "the daemon returned an invalid authorize response"
                )
            if attempt == 0 and response.get("reason_code") == SESSION_NOT_FOUND:
                session = await self._reopen(session)
                continue
            return response
        raise KeiProxySocketError("proxy_error", "socket authorize failed closed")

    # Model routes ---------------------------------------------------------

    async def list_models(self) -> list[dict[str, Any]]:
        """The models the daemon advertises (``GET /v1/models``), each with an ``id``."""
        body = _json_object(await self._send("GET", "/v1/models", timeout=self._timeout))
        data = body.get("data")
        if not isinstance(data, list):
            raise KeiProxySocketError("malformed_response", "the daemon returned no model list")
        return [m for m in data if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]]

    async def chat_completion(
        self, session: KeiProxySocketSession, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """One non-streaming chat completion in ``session``."""
        body = {**payload, "stream": False}

        async def call(session_id: str) -> httpx.Response:
            return await self._send(
                "POST",
                "/v1/chat/completions",
                session_id=session_id,
                json_body=body,
                timeout=self._model_timeout,
            )

        return _json_object(await self._with_session(session, call))

    async def stream_chat_completion(
        self, session: KeiProxySocketSession, payload: Mapping[str, Any]
    ) -> AsyncIterator[str]:
        """A streaming chat completion in ``session``, as its server-sent ``data:`` lines.

        Lines are passed through unchanged, ``data: [DONE]`` included; blank
        lines and comments are dropped.
        """
        body = {**payload, "stream": True}
        for attempt in range(2):
            client = self._client(self._model_timeout)
            try:
                request = client.build_request(
                    "POST",
                    "/v1/chat/completions",
                    json=body,
                    headers={SESSION_HEADER: session.session_id},
                )
                response = await client.send(request, stream=True)
            except (httpx.HTTPError, OSError) as exc:
                await client.aclose()
                raise _transport_error(exc) from None
            except BaseException:
                await client.aclose()
                raise
            try:
                if response.is_error:
                    await response.aread()
                    code = _error_code(response)
                    if attempt == 0 and code == SESSION_NOT_FOUND:
                        session = await self._reopen(session)
                        continue
                    raise _status_error(response.status_code, code)
                try:
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            yield line
                except httpx.HTTPError as exc:
                    raise _transport_error(exc) from None
                return
            finally:
                await response.aclose()
                await client.aclose()

    # Status ---------------------------------------------------------------

    async def policy_bundle_status(self) -> dict[str, Any]:
        """The daemon's ``kei.policy-bundle-status/v1`` (e.g. its ``bundle_schema``)."""
        body = _json_object(await self._send("GET", "/v1/policy-bundle/status"))
        if body.get("schema") != POLICY_BUNDLE_STATUS_SCHEMA:
            raise KeiProxySocketError(
                "malformed_response", "the daemon returned an invalid bundle status"
            )
        return body

    # Transport ------------------------------------------------------------

    def _client(self, timeout: float) -> httpx.AsyncClient:
        env = os.environ if self._env is None else self._env
        token = env.get(BOOTSTRAP_TOKEN_ENV, "")
        if not token:
            raise KeiProxySocketError("missing_token", f"{BOOTSTRAP_TOKEN_ENV} is not set")
        return httpx.AsyncClient(
            transport=self._transport or httpx.AsyncHTTPTransport(uds=self._socket_path),
            base_url=_BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            # Ambient HTTP(S)_PROXY must never reroute a socket request.
            trust_env=False,
            follow_redirects=False,
        )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        session_id: str = "",
        json_body: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """One request; transport failures become :class:`KeiProxySocketError`."""
        headers = {SESSION_HEADER: session_id} if session_id else {}
        client = self._client(self._timeout if timeout is None else timeout)
        try:
            async with client:
                return await client.request(method, path, json=json_body, headers=headers)
        except (httpx.HTTPError, OSError) as exc:
            raise _transport_error(exc) from None

    async def _with_session(
        self,
        session: KeiProxySocketSession,
        call: Callable[[str], Awaitable[httpx.Response]],
    ) -> httpx.Response:
        """Run ``call`` in ``session``, reopening it once on ``session_not_found``."""
        response = await call(session.session_id)
        if response.is_error and _error_code(response) == SESSION_NOT_FOUND:
            session = await self._reopen(session)
            response = await call(session.session_id)
        return response

    async def _reopen(self, stale: KeiProxySocketSession) -> KeiProxySocketSession:
        self._forget(stale)
        return await self.open_session(stale.identity)

    def _forget(self, session: KeiProxySocketSession) -> None:
        """Drop ``session`` from the cache, unless it was already replaced."""
        key = (session.source, session.external_id)
        cached = self._sessions.get(key)
        if cached is not None and cached.session_id == session.session_id:
            del self._sessions[key]


class KeiProxySocketAuthorizationClient:
    """The socket client as a synchronous :class:`AuthorizationClient`.

    :class:`~pedro_agentware.kei.KeiProxyEvaluator` is synchronous and names
    the acting user (``user_id``, the invoking subject) on every call. This
    adapter opens, or reuses, the daemon session for ``(source, user_id)`` and
    authorizes in it. Each call runs on a private event loop in a worker
    thread, so it is safe to call from inside a running loop.

    ``invoking_subject``, ``resources`` and ``workspace_id`` are accepted and
    not sent: the daemon takes the subject from the session, derives workspace
    scope from the runtime token, and rejects unknown request fields.
    """

    def __init__(self, source: str, client: KeiProxySocketAuthorizeClient | None = None) -> None:
        """Build an adapter.

        Args:
            source: The session source for every subject, e.g. ``openwebui``
                or ``discord``: the identity namespace the daemon resolves
                ``user_id`` in.
            client: The async client. Defaults to one configured from the
                environment.
        """
        if not source.strip():
            raise ValueError("source must be non-empty")
        self._source = source
        self._client = client or KeiProxySocketAuthorizeClient()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(source={self._source!r}, client={self._client!r})"

    @property
    def client(self) -> KeiProxySocketAuthorizeClient:
        return self._client

    def authorize(
        self,
        user_id: str,
        tool: str,
        action: str,
        resource: str,
        *,
        span_id: str = "",
        invoking_subject: str = "",
        parent_span: str = "",
        delegation_depth: int = 0,
        agent_id: str = "",
        agent_version: str = "",
        framework: str = "",
        tool_args_digest: str = "",
        resources: list[str] | None = None,
        workspace_id: str = "",
    ) -> dict[str, Any]:
        """Authorize one call for ``user_id`` over the socket."""
        if not user_id:
            raise KeiProxySocketError("proxy_error", "no invoking subject to open a session for")
        identity = KeiProxySocketIdentity(self._source, user_id)

        async def run() -> dict[str, Any]:
            session = await self._client.open_session(identity)
            return await self._client.authorize(
                session,
                tool=tool,
                action=action,
                resource=resource,
                span_id=span_id,
                parent_span=parent_span,
                delegation_depth=delegation_depth,
                agent_id=agent_id,
                framework=framework,
                tool_args_digest=tool_args_digest,
            )

        return _run_blocking(run)


def select_authorization_client(
    source: str,
    *,
    socket_path: str | None = None,
    env: Mapping[str, str] | None = None,
    executable: str = "kei-proxy",
    timeout: float = DEFAULT_SOCKET_TIMEOUT,
    expected_sha256: str | None = None,
) -> AuthorizationClient:
    """The :class:`AuthorizationClient` for :class:`~pedro_agentware.kei.KeiProxyEvaluator`.

    The socket client when a socket is configured (``socket_path``, or
    ``KEI_RUNTIME_SOCKET_PATH`` in ``env``), otherwise the ``kei-proxy
    authorize`` subprocess client. The choice is made once, here: a
    configured socket that is down is a refusal, never a silent fall back
    to the subprocess.
    """
    source_env = os.environ if env is None else env
    path = socket_path or source_env.get(RUNTIME_SOCKET_PATH_ENV, "")
    if path:
        return KeiProxySocketAuthorizationClient(
            source, KeiProxySocketAuthorizeClient(socket_path=path, timeout=timeout, env=env)
        )
    # Imported here: authorize_client imports this package's evaluator, and
    # the socket path never needs the subprocess machinery.
    from .authorize_client import KeiProxyAuthorizeClient

    return KeiProxyAuthorizeClient(
        executable=executable, timeout=timeout, env=env, expected_sha256=expected_sha256
    )


def _run_blocking(factory: Callable[[], Awaitable[_T]]) -> _T:
    """Run a coroutine to completion from synchronous code, inside a loop or not."""

    async def main() -> _T:
        return await factory()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, main()).result()


def _transport_error(exc: BaseException) -> KeiProxySocketError:
    if isinstance(exc, KeiProxySocketError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return KeiProxySocketError("proxy_timeout", "the kei-proxy daemon did not answer in time")
    return KeiProxySocketError(
        "proxy_unavailable", f"the kei-proxy daemon is unreachable ({type(exc).__name__})"
    )


def _status_error(status: int, code: str) -> KeiProxySocketError:
    what = "runtime authentication failed" if status == 401 else f"the daemon answered {status}"
    return KeiProxySocketError("proxy_error", f"{what} ({code})" if code else what, code)


def _error_code(response: httpx.Response) -> str:
    """The daemon's ``{"error": {"code": ...}}``, or an empty string."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or "")
    if isinstance(error, str):
        return error
    return str(body.get("code") or "")


def _check_status(response: httpx.Response) -> None:
    if not 200 <= response.status_code < 300:
        raise _status_error(response.status_code, _error_code(response))


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """A 2xx response's JSON object body (``{}`` when empty)."""
    _check_status(response)
    if response.status_code == 204 or not response.content.strip():
        return {}
    try:
        body = response.json()
    except ValueError:
        raise KeiProxySocketError("malformed_response", "the daemon's answer is not JSON") from None
    if not isinstance(body, dict):
        raise KeiProxySocketError(
            "malformed_response", "the daemon's answer is not a JSON object"
        )
    return body
