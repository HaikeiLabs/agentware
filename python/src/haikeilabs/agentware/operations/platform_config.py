"""The runner's ConnectorConfig, built from the platform.

Connectors are set up in the Kei platform and bound to the agent there, so a
harness learns them at startup the way it learns its model profile: it asks
kei-proxy (``GET /v1/agent-config``, kei-proxy 0.12.0 or later) and names no
connector ID itself. :func:`connector_config` joins that answer with the
harness's generated operation bindings by tool name:

- the agent-config connector whose ``tools`` list the tool gives the
  installation's ``connection`` name and its ``base_url``;
- the contracts ``Connection`` of that name, on the contract connector the
  tool's operations belong to, gives the connection ``kind`` and header;
- the tool's ``binding_version`` is sent on authorize.

The entry carries no connector ID: the runner releases the credential with
the tool and its arguments, and kei-proxy derives the connector from the
tool's binding. Metadata only: nothing here is or names a credential.
"""

import dataclasses
import importlib
import re
from collections.abc import Mapping
from typing import Any, Protocol, cast

from kei.contracts.v1 import options_pb2

from haikeilabs.agentware.kei import KeiProxySocketError

from .runner import ConnectorEntry, OperationBinding

__all__ = [
    "AgentConfigSource",
    "ConnectorsUnavailableError",
    "ToolBindings",
    "connector_config",
]

# The generated operation bindings a harness serves, per registered tool name:
# operation id (``github.issues.get``) -> binding.
ToolBindings = Mapping[str, Mapping[str, OperationBinding]]

_CONTRACT_PACKAGE = re.compile(r"kei\.connectors\.[a-z0-9_]+\.v[0-9]+")
_KIND_PREFIX = "CONNECTION_KIND_"


class AgentConfigSource(Protocol):
    """What :func:`connector_config` reads; the socket client satisfies it."""

    async def agent_config(self) -> Mapping[str, Any]: ...


class ConnectorsUnavailableError(Exception):
    """kei-proxy could not say which connectors the agent has: fail closed.

    Raised for a daemon older than 0.12.0 (404), one without a fresh v2 policy
    bundle (503 ``agent_config_unavailable``), an unreachable daemon or a
    malformed answer. ``reason_class`` and ``code`` are the socket error's.
    """

    reason_code = "connectors_unavailable"

    def __init__(self, reason_class: str = "", code: str = "") -> None:
        detail = f" ({reason_class}{': ' + code if code else ''})" if reason_class else ""
        super().__init__(f"Connectors unavailable{detail}")
        self.reason_class = reason_class
        self.code = code


async def connector_config(
    client: AgentConfigSource, bindings: ToolBindings
) -> dict[str, ConnectorEntry]:
    """Build the runner's ConnectorConfig for the tools in ``bindings``, keyed by tool name.

    A tool is left out (and the runner then denies it ``connector_not_named``)
    when the platform does not bind it to this agent, the installation does not
    name its connection yet, or the connection is not one of the contract
    connector's. Raises :class:`ConnectorsUnavailableError` when the agent
    config cannot be read, and ``ValueError`` when one tool's bindings span
    more than one contract connector.
    """
    try:
        config = await client.agent_config()
    except KeiProxySocketError as exc:
        raise ConnectorsUnavailableError(exc.reason_class, exc.code) from None
    connectors = config.get("connectors") if isinstance(config, Mapping) else None
    if not isinstance(connectors, list):
        raise ConnectorsUnavailableError("malformed_response")

    bound: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for connector in connectors:
        if not isinstance(connector, Mapping) or not isinstance(connector.get("tools"), list):
            continue
        for tool in connector["tools"]:
            if isinstance(tool, Mapping) and isinstance(tool.get("tool"), str):
                bound[tool["tool"]] = (connector, tool)

    entries: dict[str, ConnectorEntry] = {}
    for tool_name, operations in bindings.items():
        prefix = _contract_prefix(tool_name, operations)
        if tool_name not in bound or prefix is None:
            continue
        connector, tool = bound[tool_name]
        connection_name = connector.get("connection")
        if not isinstance(connection_name, str) or not connection_name:
            continue
        contract = _contract_connector(prefix, operations)
        spec = (
            next((c for c in contract.connections if c.name == connection_name), None)
            if contract
            else None
        )
        if contract is None or spec is None:
            continue
        kind = options_pb2.ConnectionKind.Name(spec.kind).removeprefix(_KIND_PREFIX).lower()
        base_url = _string(connector.get("base_url")) or spec.base_url or contract.base_url
        version = _string(tool.get("binding_version"))
        entries[tool_name] = ConnectorEntry(
            connector_id="",
            connection_name=connection_name,
            kind=cast(Any, kind),
            base_url=base_url,
            header_name=spec.header_name,
            operations={
                op_id: dataclasses.replace(
                    binding, binding_version=version or binding.binding_version
                )
                for op_id, binding in operations.items()
            },
        )
    return entries


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _contract_prefix(tool: str, operations: Mapping[str, OperationBinding]) -> str | None:
    """The one contract connector (``github``) the tool's operation ids share."""
    prefixes = {op_id.split(".")[0] for op_id in operations}
    if len(prefixes) > 1:
        raise ValueError(f"tool {tool!r} binds operations of more than one contract connector")
    return prefixes.pop() if prefixes else None


def _contract_connector(prefix: str, operations: Mapping[str, OperationBinding]) -> Any:
    """The contracts ``Connector`` declared next to the bindings' request messages.

    Each generated connector package declares its ``Connector`` in
    ``connector_pb2`` beside the request messages; it is imported from the
    first binding's package and must carry the operations' prefix as its id.
    """
    for binding in operations.values():
        package = binding.request_type.DESCRIPTOR.file.package
        if not _CONTRACT_PACKAGE.fullmatch(package):
            return None
        try:
            module = importlib.import_module(f"{package}.connector_pb2")
            options = module.DESCRIPTOR.GetOptions()
        except (ImportError, AttributeError):
            return None
        if not options.HasExtension(options_pb2.connector):
            return None
        contract = options.Extensions[options_pb2.connector]
        return contract if contract.id == prefix else None
    return None
