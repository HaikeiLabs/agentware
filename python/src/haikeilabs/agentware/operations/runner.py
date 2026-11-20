"""The Python OperationRunner: one generated operation behind kei-proxy.

Mirrors ``typescript/src/operations/runner.ts`` (agentware#325), same
behaviour with Python idioms. For one operation call the runner:

1. looks up the installed connector metadata for the operation (metadata
   only, no credentials),
2. reads the operation's resource template from the generated op descriptor
   and renders the request's fields into it,
3. asks the kei-proxy daemon for one decision (``connector:<op.id>`` on the
   rendered resource) in the caller's session,
4. on an explicit allow, releases the session credential,
5. builds the provider connection with the released token and calls the
   generated operation.

Fail-closed: anything that is not an explicit allow (``allow``/``permit``)
raises :class:`OperationDeniedError`; a failed authorize or release denies before
any provider request. The credential never appears in a log line, an error
message or an audit record: errors carry reason codes only, and the audit
record is metadata only (operation id, decision, outcome).
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx
from google.protobuf.message import Message
from kei.connection import HttpConnection, Op
from kei.contracts.v1 import options_pb2

from haikeilabs.agentware.kei import KeiProxySocketSession
from haikeilabs.agentware.middleware.types import CallerContext

from .connections import (
    ConnectionKind,
    UnsupportedConnectionError,
    assert_supported_connection,
    build_connection,
)

__all__ = [
    "ConnectRequiredError",
    "ConnectorConfig",
    "ConnectorEntry",
    "EnrollmentRequiredError",
    "OperationAudit",
    "OperationAuditSink",
    "OperationBinding",
    "OperationDeniedError",
    "OperationRunner",
    "OperationSocketClient",
    "operation_meta",
]

_PLACEHOLDER = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
_ALL_DOTS = re.compile(r"^\.+$")
_SAFE_REASON = re.compile(r"[a-z][a-z0-9_]{0,99}")


class OperationSocketClient(Protocol):
    """The socket methods the runner needs.

    :class:`~haikeilabs.agentware.kei.KeiProxySocketAuthorizeClient` satisfies
    this protocol; tests substitute a fake. Only ``permit``/``allow`` is an
    allow; a deny is returned as a dict, never raised.
    """

    async def authorize(
        self,
        session: KeiProxySocketSession,
        *,
        tool: str = "",
        action: str = "",
        resource: str = "",
        parent_span: str = "",
        delegation_depth: int = 0,
    ) -> Mapping[str, Any]: ...

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
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class OperationBinding:
    """Binds a generated request message type to its generated queries class."""

    request_type: type[Message]
    queries: Callable[[HttpConnection], Any]


@dataclass
class ConnectorEntry:
    """Installed connector routing metadata, indexed by the contract connector prefix.

    Metadata only: no credential lives here; the token is released per call.
    """

    connector_id: str
    connection_name: str
    kind: ConnectionKind
    base_url: str
    header_name: str = ""
    operations: dict[str, OperationBinding] = field(default_factory=dict)


ConnectorConfig = Mapping[str, ConnectorEntry]


@dataclass(frozen=True)
class OperationAudit:
    """An audit record contains metadata only."""

    operation_id: str
    decision: Literal["allow", "deny"]
    outcome: Literal["success", "denied", "error"]


OperationAuditSink = Callable[[OperationAudit], None]


class OperationDeniedError(Exception):
    """A policy or credential release was not explicitly allowed."""

    def __init__(self, reason_code: str, decision: str = "deny") -> None:
        super().__init__(f"Operation denied ({reason_code})")
        self.reason_code = reason_code
        self.decision = decision


class EnrollmentRequiredError(Exception):
    """The caller must enroll an account; details pass back without logging."""

    def __init__(self, enrollment: Any) -> None:
        super().__init__("Enrollment required")
        self.enrollment = enrollment


class ConnectRequiredError(Exception):
    """The caller must connect an account; details pass back without logging."""

    def __init__(self, connect: Any) -> None:
        super().__init__("Connection required")
        self.connect = connect


def operation_meta(op: Op, request_type: type[Message]) -> Any:
    """The generated ``operation`` option metadata for ``op`` on ``request_type``.

    Raises :class:`OperationDeniedError` (``invalid_operation``) when the request
    descriptor carries no operation option or one that does not name ``op``.
    """
    try:
        meta = request_type.DESCRIPTOR.GetOptions().Extensions[options_pb2.operation]
    except (AttributeError, KeyError):
        raise OperationDeniedError("invalid_operation") from None
    if meta.id != op.id or not meta.connector or not meta.method_name or not meta.resource_template:
        raise OperationDeniedError("invalid_operation")
    return meta


def render_resource(template: str, request_type: type[Message], request: Message) -> str:
    """Render the authorize/release resource from ``template`` and ``request``.

    Every placeholder is one escaped path segment: ``/`` becomes ``%2F``, a
    value made only of dots becomes ``%2E`` repeated. Any other brace in the
    template, an unknown field, a non-string/int value or an empty value
    raises :class:`OperationDeniedError` (``invalid_resource``).
    """
    if not isinstance(request, Message) or not template:
        raise OperationDeniedError("invalid_resource")
    if re.search(r"[{}]", _PLACEHOLDER.sub("", template)):
        raise OperationDeniedError("invalid_resource")
    fields = request_type.DESCRIPTOR.fields_by_name

    def replace(match: re.Match[str]) -> str:
        name = match.group(0)[1:-1]
        if name not in fields:
            raise OperationDeniedError("invalid_resource")
        value = getattr(request, name)
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise OperationDeniedError("invalid_resource")
        text = str(value)
        if not text:
            raise OperationDeniedError("invalid_resource")
        if _ALL_DOTS.fullmatch(text):
            return "%2E" * len(text)
        return quote(text, safe="!*'()")

    return _PLACEHOLDER.sub(replace, template)


def _snake(name: str) -> str:
    """The generated Python method name for an operation's ``method_name``."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _safe_reason(value: Any, fallback: str) -> str:
    """A reason code safe to expose; anything malformed becomes ``fallback``."""
    if isinstance(value, str) and _SAFE_REASON.fullmatch(value):
        return value
    return fallback


def _check_decision(response: Mapping[str, Any]) -> None:
    """Only an explicit ``allow``/``permit`` passes; everything else raises."""
    decision = response.get("decision")
    if decision in ("allow", "permit"):
        return
    if (
        decision == "enrollment_required"
        or response.get("reason_code") == "enrollment_required"
        or "enrollment" in response
    ):
        raise EnrollmentRequiredError(response.get("enrollment"))
    if (
        decision == "connect_required"
        or response.get("reason_code") == "connect_required"
        or "connect" in response
    ):
        raise ConnectRequiredError(response.get("connect"))
    raise OperationDeniedError(_safe_reason(response.get("reason_code"), "denied"))


class OperationRunner:
    """Runs one generated operation behind kei-proxy authorization and release.

    Args:
        client: The socket client (the daemon's session, authorize and
            credential-release routes).
        session: The daemon session the calls run in.
        connectors: Installed connector routing metadata, indexed by the
            contract connector prefix (``github`` for ``github.issues.get``).
        audit: An optional metadata-only audit sink; it sees the operation
            id, the decision and the outcome, never a credential.
        http_client: The ``httpx.AsyncClient`` the provider connection sends
            through. Defaults to a private client per call, closed after it.
    """

    def __init__(
        self,
        client: OperationSocketClient,
        session: KeiProxySocketSession,
        connectors: ConnectorConfig,
        audit: OperationAuditSink | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client
        self._session = session
        self._connectors = connectors
        self._audit = audit
        self._http_client = http_client

    async def run(
        self,
        caller: CallerContext,
        op: Op,
        request: Message,
        *,
        tool: str = "",
        resource_template_override: str | None = None,
    ) -> Any:
        """Authorize, release and execute ``op`` for ``caller``."""
        decision: Literal["allow", "deny"] = "deny"
        outcome: Literal["success", "denied", "error"] = "denied"
        try:
            if caller is None or op is None or not isinstance(getattr(op, "id", None), str):
                raise OperationDeniedError("invalid_context")
            prefix = op.id.split(".")[0]
            config = self._connectors.get(prefix)
            binding = config.operations.get(op.id) if config is not None else None
            if (
                config is None
                or binding is None
                or not config.connector_id
                or not config.connection_name
            ):
                raise OperationDeniedError("connector_not_named")
            meta = operation_meta(op, binding.request_type)
            if not isinstance(request, binding.request_type):
                raise OperationDeniedError("invalid_request")
            if meta.connector != prefix or config.connection_name not in meta.connections:
                raise OperationDeniedError("invalid_connection")
            assert_supported_connection(config.kind)
            template = (
                meta.resource_template
                if resource_template_override is None
                else resource_template_override
            )
            resource = render_resource(template, binding.request_type, request)
            capability = f"connector:{op.id}"
            parent_span = caller.parent_span
            delegation_depth = caller.delegation_depth
            try:
                result = await self._client.authorize(
                    self._session,
                    tool=tool,
                    action=capability,
                    resource=resource,
                    parent_span=parent_span,
                    delegation_depth=delegation_depth,
                )
            except Exception:
                raise OperationDeniedError("proxy_unavailable") from None
            _check_decision(result)
            try:
                result = await self._client.release_credential(
                    self._session,
                    connector_id=config.connector_id,
                    capability=capability,
                    resource=resource,
                    tool=tool,
                    parent_span=parent_span,
                    delegation_depth=delegation_depth,
                )
            except Exception:
                raise OperationDeniedError("credential_release_failed") from None
            _check_decision(result)
            credential = result.get("credential")
            token = credential.get("access_token") if isinstance(credential, Mapping) else None
            if not isinstance(token, str) or not token:
                raise OperationDeniedError("credential_release_failed")
            decision = "allow"
            outcome = "error"
            own_client = httpx.AsyncClient() if self._http_client is None else None
            client = self._http_client if own_client is None else own_client
            try:
                conn = build_connection(
                    config.kind, config.base_url, token, client, config.header_name
                )
                queries = binding.queries(conn)
                method = getattr(queries, _snake(meta.method_name), None)
                if not callable(method):
                    raise RuntimeError("Generated operation method is unavailable")
                response = await method(request)
            finally:
                if own_client is not None:
                    await own_client.aclose()
            outcome = "success"
            return response
        except (
            OperationDeniedError,
            EnrollmentRequiredError,
            ConnectRequiredError,
            UnsupportedConnectionError,
        ):
            raise
        except Exception:
            raise RuntimeError("Operation execution failed") from None
        finally:
            try:
                if self._audit is not None:
                    self._audit(OperationAudit(_operation_id(op), decision, outcome))
            except Exception:
                pass  # Audit sinks cannot expose execution data or alter results.


def _operation_id(op: Op | None) -> str:
    """The operation id for the audit record, or an empty string."""
    if op is None:
        return ""
    op_id = getattr(op, "id", None)
    return op_id if isinstance(op_id, str) else ""
