"""Language-level tool declarations and the runtime manifest v3 serializer."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from haikeilabs.agentware.llm.request import ToolDefinition

MANIFEST_SCHEMA = "kei.tool-manifest/v3"


class ManifestV3Error(Exception):
    """A kei.tool-manifest/v3 document could not be loaded.

    ``code`` is one of the README error codes (invalid_json,
    unsupported_schema, unknown_field, missing_field, invalid_type,
    invalid_operation_class, invalid_route, duplicate_tool_name) and is the
    stable, machine-checkable part of the error. The message is generic on
    purpose: it names the rule that failed, never the field's value.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ToolRegistrar(Protocol):
    """Runtime socket client interface used to submit a serialized manifest."""

    async def register_tools(self, manifest: Mapping[str, Any]) -> dict[str, Any]: ...


@dataclass
class ToolSpec:
    """A model tool declaration with optional explicit runtime routing metadata."""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    service: str | None = None
    source: str | None = None
    operation_class: str | None = None
    route: dict[str, Any] | None = None
    required_capabilities: list[str] | None = None
    resource_types: list[dict[str, str]] | None = None
    enabled: bool = True

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ToolSpec":
        """Create a ToolSpec from a plain JSON object."""
        return cls(**value)

    @classmethod
    def from_tool_definition(cls, value: ToolDefinition) -> "ToolSpec":
        """Adapt an existing model-only definition without inventing wire metadata."""
        return cls(name=value.name, description=value.description, parameters=value.input_schema)

    @property
    def input_schema(self) -> dict[str, Any]:
        """Compatibility property consumed by existing model formatters."""
        return self.parameters

    def to_tool_definition(self) -> ToolDefinition:
        """Convert to the existing LLM request model for legacy entry points."""
        return ToolDefinition(
            name=self.name, description=self.description, input_schema=self.parameters
        )

    def to_wire(self) -> dict[str, Any]:
        """Validate runtime metadata and return this tool's manifest v3 entry."""
        for field_name in ("service", "source", "operation_class", "route"):
            value = getattr(self, field_name)
            if value is None or value == "":
                raise ValueError(f"{field_name} is required")
        if self.operation_class not in ("read", "write"):
            raise ValueError("operation_class must be read or write")
        route = self.route
        if not isinstance(route, dict) or len(route) != 1:
            raise ValueError("route must contain exactly one route kind")
        route_kind, route_value = next(iter(route.items()))
        if route_kind not in ("harness_executor", "connector_binding") or not isinstance(
            route_value, dict
        ):
            raise ValueError("route must contain exactly one route kind")
        if route_kind == "harness_executor":
            if not route_value.get("executor") or not route_value.get("registration"):
                raise ValueError("harness_executor route requires executor and registration")
        elif not route_value.get("agent_id") or not route_value.get("connector_id"):
            raise ValueError("connector_binding route requires agent_id and connector_id")

        result: dict[str, Any] = {
            "name": self.name,
            "service": self.service,
            "source": self.source,
            "operation_class": self.operation_class,
            "route": self.route,
        }
        if self.required_capabilities is not None:
            result["required_capabilities"] = self.required_capabilities
        if self.resource_types is not None:
            result["resource_types"] = self.resource_types
        result["description"] = self.description
        result["enabled"] = self.enabled
        return result


_TOOL_FIELD_TYPES: dict[str, type] = {
    "name": str,
    "description": str,
    "service": str,
    "source": str,
    "operation_class": str,
    "route": dict,
    "required_capabilities": list,
    "resource_types": list,
    "parameters": dict,
    "enabled": bool,
}

_HARNESS_ROUTE_FIELDS = {"executor", "registration"}
_CONNECTOR_ROUTE_FIELDS = {"agent_id", "connector_id"}


def _fail(code: str, message: str) -> ManifestV3Error:
    return ManifestV3Error(code, f"tool manifest: {message}")


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _parse_route(index: int, tool: Mapping[str, Any]) -> None:
    """Validate tools[i].route and its kind-specific rules."""
    route = tool["route"]
    if len(route) != 1:
        raise _fail("invalid_route", f"tools[{index}].route must hold exactly one route kind")
    kind, value = next(iter(route.items()))
    if kind not in ("harness_executor", "connector_binding"):
        raise _fail("unknown_field", f"tools[{index}].route has an unknown route kind")
    if not isinstance(value, dict):
        raise _fail("invalid_route", f"tools[{index}].route.{kind} must be an object")
    if kind == "harness_executor":
        if set(value) != _HARNESS_ROUTE_FIELDS or not all(
            _nonempty_str(value[name]) for name in _HARNESS_ROUTE_FIELDS
        ):
            raise _fail(
                "invalid_route",
                "tools["
                + str(index)
                + "].route.harness_executor needs non-empty executor and registration",
            )
        if "required_capabilities" in tool:
            raise _fail(
                "invalid_route",
                "tools["
                + str(index)
                + "].required_capabilities must not be present on a harness route",
            )
        if "resource_types" in tool:
            raise _fail(
                "invalid_route",
                f"tools[{index}].resource_types must not be present on a harness route",
            )
        return
    if set(value) != _CONNECTOR_ROUTE_FIELDS or not all(
        _nonempty_str(value[name]) for name in _CONNECTOR_ROUTE_FIELDS
    ):
        raise _fail(
            "invalid_route",
            "tools["
            + str(index)
            + "].route.connector_binding needs non-empty agent_id and connector_id",
        )
    capabilities = tool.get("required_capabilities")
    if (
        not isinstance(capabilities, list)
        or not capabilities
        or not all(_nonempty_str(capability) for capability in capabilities)
    ):
        raise _fail(
            "invalid_route",
            "tools["
            + str(index)
            + "] needs a non-empty required_capabilities array of strings on a connector route",
        )
    resource_types = tool.get("resource_types")
    if resource_types is not None and not resource_types:
        raise _fail(
            "invalid_route", f"tools[{index}].resource_types must not be empty on a connector route"
        )


def _parse_resource_types(index: int, tool: Mapping[str, Any]) -> None:
    for position, entry in enumerate(tool.get("resource_types") or []):
        if not isinstance(entry, dict):
            raise _fail(
                "invalid_type", f"tools[{index}].resource_types[{position}] must be an object"
            )
        if not _nonempty_str(entry.get("type")):
            raise _fail(
                "missing_field",
                f"tools[{index}].resource_types[{position}].type is missing or empty",
            )
        if "parent_type" in entry and not isinstance(entry["parent_type"], str):
            raise _fail(
                "invalid_type",
                f"tools[{index}].resource_types[{position}].parent_type must be a string",
            )


def _parse_tool(index: int, tool: Mapping[str, Any], seen_names: set[str]) -> ToolSpec:
    for key in tool:
        if key not in _TOOL_FIELD_TYPES:
            raise _fail("unknown_field", f"tools[{index}] has an unknown field")
    for key, expected in _TOOL_FIELD_TYPES.items():
        if key in tool and not isinstance(tool[key], expected):
            raise _fail("invalid_type", f"tools[{index}].{key} has the wrong type")
    for key in ("name", "description", "service", "source"):
        if not _nonempty_str(tool.get(key)):
            raise _fail("missing_field", f"tools[{index}].{key} is missing or empty")
    operation_class = tool.get("operation_class")
    if not _nonempty_str(operation_class):
        raise _fail(
            "missing_field", "tools[" + str(index) + "].operation_class is missing or empty"
        )
    if operation_class not in ("read", "write"):
        raise _fail(
            "invalid_operation_class",
            "tools[" + str(index) + "].operation_class must be read or write",
        )
    if "route" not in tool:
        raise _fail("missing_field", f"tools[{index}].route is missing")
    _parse_route(index, tool)
    _parse_resource_types(index, tool)
    name = tool["name"]
    if name in seen_names:
        raise _fail("duplicate_tool_name", f"tools[{index}] repeats a tool name")
    seen_names.add(name)
    return ToolSpec(
        name=name,
        description=tool["description"],
        parameters=tool.get("parameters", {}),
        service=tool["service"],
        source=tool["source"],
        operation_class=operation_class,
        route=tool["route"],
        required_capabilities=tool.get("required_capabilities"),
        resource_types=tool.get("resource_types"),
        enabled=tool.get("enabled", True),
    )


def parse_manifest_v3(data: bytes | str) -> list[ToolSpec]:
    """Parse a kei.tool-manifest/v3 document into typed ToolSpecs.

    Strict: unknown fields, wrong types, missing or empty required fields,
    unknown operation classes, malformed routes, and duplicate tool names
    all raise :class:`ManifestV3Error` with the README error code. Tools
    come back in file order. The error message names the rule, never the
    field's value.
    """
    try:
        document = json.loads(data)
    except ValueError:
        raise _fail("invalid_json", "the manifest is not valid JSON") from None
    if not isinstance(document, dict):
        raise _fail("invalid_type", "the manifest must be a JSON object")
    if document.get("schema") != MANIFEST_SCHEMA:
        raise _fail("unsupported_schema", f"the manifest schema is not {MANIFEST_SCHEMA}")
    for key in document:
        if key not in ("schema", "tools"):
            raise _fail("unknown_field", "the manifest has an unknown field")
    if "tools" not in document:
        raise _fail("missing_field", "the manifest is missing tools")
    tools = document["tools"]
    if not isinstance(tools, list):
        raise _fail("invalid_type", "tools must be an array")
    seen_names: set[str] = set()
    specs: list[ToolSpec] = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise _fail("invalid_type", f"tools[{index}] must be an object")
        specs.append(_parse_tool(index, tool, seen_names))
    return specs


def load_manifest_v3(path: str | Path) -> list[ToolSpec]:
    """Read and parse a kei.tool-manifest/v3 file into typed ToolSpecs.

    A missing or unreadable file raises the platform OSError (e.g.
    FileNotFoundError); document problems raise :class:`ManifestV3Error`.
    """
    return parse_manifest_v3(Path(path).read_bytes())


def serialize_tool_specs(specs: list[ToolSpec]) -> dict[str, Any]:
    """Serialize and validate runtime-routed ToolSpecs as a v3 manifest."""
    return {"schema": MANIFEST_SCHEMA, "tools": [spec.to_wire() for spec in specs]}


async def register_tool_specs(client: ToolRegistrar, specs: list[ToolSpec]) -> dict[str, Any]:
    """Serialize and register ToolSpecs through the runtime Unix socket client."""
    return await client.register_tools(serialize_tool_specs(specs))


__all__ = [
    "MANIFEST_SCHEMA",
    "ManifestV3Error",
    "ToolSpec",
    "load_manifest_v3",
    "parse_manifest_v3",
    "register_tool_specs",
    "serialize_tool_specs",
]
