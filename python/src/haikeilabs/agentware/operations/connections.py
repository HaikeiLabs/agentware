"""Connection building for the Python OperationRunner.

Mirrors ``typescript/src/operations/connections.ts``: connection
configuration is metadata; the credential is released per call and handed to
the connection here. Only the HTTP connection kinds are supported today; SQL
and the remaining kinds need a future runtime integration.
"""

from typing import Any, Literal

from kei.connection import HttpConnection, http_connection

__all__ = [
    "ConnectionKind",
    "UnsupportedConnectionError",
    "assert_supported_connection",
    "build_connection",
]

# The connection kinds a connector installation can name. Only the HTTP kinds
# are supported by the runner; the rest raise UnsupportedConnectionError.
ConnectionKind = Literal[
    "http_bearer",
    "graphql_bearer",
    "http_header",
    "http_basic",
    "sql_dsn",
    "key_pair_jwt",
    "token_exchange",
]

_SUPPORTED_HTTP_KINDS = ("http_bearer", "graphql_bearer", "http_header", "http_basic")


class UnsupportedConnectionError(Exception):
    """The requested connection kind is not supported by the Python OperationRunner."""


def assert_supported_connection(kind: str) -> None:
    """Raise :class:`UnsupportedConnectionError` unless ``kind`` is an HTTP kind."""
    if kind not in _SUPPORTED_HTTP_KINDS:
        raise UnsupportedConnectionError()


def build_connection(
    kind: str,
    base_url: str,
    credential: str,
    client: Any,
    header_name: str = "",
) -> HttpConnection:
    """Build an HTTP connection only after authorization and credential release.

    ``client`` is the ``httpx.AsyncClient`` the connection sends through: the
    connection makes no request except through it.
    """
    if kind in ("http_bearer", "graphql_bearer"):
        return http_connection(base_url, "bearer", credential, client)
    if kind == "http_header":
        return http_connection(base_url, "header", credential, client, header_name)
    if kind == "http_basic":
        return http_connection(base_url, "basic", credential, client)
    raise UnsupportedConnectionError()
