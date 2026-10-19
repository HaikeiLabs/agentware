"""Language-level tool declarations and the runtime manifest v3 serializer."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from haikeilabs.agentware.llm.request import ToolDefinition

MANIFEST_SCHEMA = "kei.tool-manifest/v3"


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


def serialize_tool_specs(specs: list[ToolSpec]) -> dict[str, Any]:
    """Serialize and validate runtime-routed ToolSpecs as a v3 manifest."""
    return {"schema": MANIFEST_SCHEMA, "tools": [spec.to_wire() for spec in specs]}


async def register_tool_specs(client: ToolRegistrar, specs: list[ToolSpec]) -> dict[str, Any]:
    """Serialize and register ToolSpecs through the runtime Unix socket client."""
    return await client.register_tools(serialize_tool_specs(specs))


__all__ = ["MANIFEST_SCHEMA", "ToolSpec", "register_tool_specs", "serialize_tool_specs"]
