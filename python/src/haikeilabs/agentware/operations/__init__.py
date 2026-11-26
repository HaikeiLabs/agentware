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
from .platform_config import (
    AgentConfigSource,
    ConnectorsUnavailableError,
    ToolBindings,
    connector_config,
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
    "AgentConfigSource",
    "ConnectRequiredError",
    "ConnectionKind",
    "ConnectorConfig",
    "ConnectorEntry",
    "ConnectorsUnavailableError",
    "EnrollmentRequiredError",
    "OperationAudit",
    "OperationAuditSink",
    "OperationBinding",
    "OperationDeniedError",
    "OperationMeta",
    "OperationRunner",
    "OperationSocketClient",
    "ToolBindings",
    "UnsupportedConnectionError",
    "assert_supported_connection",
    "build_connection",
    "connector_config",
    "operation_meta",
]
