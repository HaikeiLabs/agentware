"""Python OperationRunner: generated connector operations behind kei-proxy.

Build-ahead draft mirroring ``typescript/src/operations/`` (agentware#325),
same behaviour with Python idioms. The generated connector contracts package
(``kei.connection``, ``kei.connectors.*``) is a local path dependency until
registry publishing; SQL connections are a follow-up, as in TypeScript.
"""

from .connections import (
    ConnectionKind,
    UnsupportedConnectionError,
    assert_supported_connection,
    build_connection,
)
from .runner import (
    ConnectorConfig,
    ConnectorEntry,
    ConnectRequiredError,
    EnrollmentRequiredError,
    OperationAudit,
    OperationAuditSink,
    OperationBinding,
    OperationDeniedError,
    OperationMeta,
    OperationRunner,
    OperationSocketClient,
    operation_meta,
)

__all__ = [
    "ConnectRequiredError",
    "ConnectionKind",
    "ConnectorConfig",
    "ConnectorEntry",
    "EnrollmentRequiredError",
    "OperationAudit",
    "OperationAuditSink",
    "OperationBinding",
    "OperationDeniedError",
    "OperationMeta",
    "OperationRunner",
    "OperationSocketClient",
    "UnsupportedConnectionError",
    "assert_supported_connection",
    "build_connection",
    "operation_meta",
]
