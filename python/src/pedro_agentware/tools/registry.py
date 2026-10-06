"""Tool registry and deterministic Kei tool-manifest exporters."""

import json
from typing import Any, TypedDict

from .tool import GovernedTool


class KeiToolRegistration(TypedDict, total=False):
    """Trusted route metadata attached to the registry dispatch entry."""

    service: str
    source: str
    operation_class: str
    route: dict[str, dict[str, str]]
    required_capabilities: list[str]
    resource_types: list[dict[str, str]]


class ToolRegistry:
    """Registry for dispatchable tools, with optional explicit route metadata."""

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}
        self._registrations: dict[str, KeiToolRegistration] = {}

    def register(self, tool: Any, registration: KeiToolRegistration | None = None) -> None:
        """Register a tool and, optionally, its trusted route registration."""
        self._tools[tool.name] = tool
        self._registrations.pop(tool.name, None)
        if registration is not None:
            self._registrations[tool.name] = registration

    def get(self, name: str) -> tuple[Any, bool]:
        tool = self._tools.get(name)
        return tool, tool is not None

    def all(self) -> list[Any]:
        return [self._tools[name] for name in sorted(self._tools)]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> dict[str, dict[str, Any]]:
        return {name: tool.input_schema() for name, tool in self._tools.items() if hasattr(tool, "input_schema")}

    def export_kei_tool_manifest(self, *, version: int = 2) -> str:
        """Export v1/v2 governed tools or explicitly registered v3 routes."""
        if version not in (1, 2, 3):
            raise ValueError("manifest version must be 1, 2, or 3")
        if version == 3:
            entries = []
            for name in sorted(self._registrations):
                registration = self._registrations[name]
                self._validate_registration(registration)
                entry: dict[str, Any] = {
                    "name": name,
                    "service": registration["service"],
                    "source": registration["source"],
                    "operation_class": registration["operation_class"],
                    "route": registration["route"],
                    "description": self._tools[name].description,
                    "enabled": True,
                }
                if "connector_binding" in registration["route"]:
                    entry["required_capabilities"] = sorted(registration["required_capabilities"])
                    if registration.get("resource_types"):
                        entry["resource_types"] = sorted(
                            (dict(item) for item in registration["resource_types"]),
                            key=lambda item: (item["type"], item.get("parent_type", "")),
                        )
                entries.append(entry)
            manifest = {"schema": "kei.tool-manifest/v3", "tools": entries}
            return json.dumps(manifest, indent=2, ensure_ascii=False)

        entries = []
        names = sorted(name for name, tool in self._tools.items() if isinstance(tool, GovernedTool))
        for name in names:
            tool = self._tools[name]
            scope = tool.kei_scope()
            if version == 1:
                entries.append({"name": name, "service": scope.service or scope.source, "description": tool.description,
                                "action": scope.operation_class, "resources": [item["type"] for item in scope.resource_types], "enabled": True})
            else:
                entry = {"name": name, "source": scope.source, "required_capabilities": sorted(scope.required_capabilities),
                         "resource_types": sorted((dict(item) for item in scope.resource_types), key=lambda item: (item["type"], item.get("parent_type", ""))),
                         "operation_class": scope.operation_class}
                if scope.service:
                    entry["service"] = scope.service
                entry["description"] = tool.description
                entry["enabled"] = True
                entries.append(entry)
        manifest = {"tools": entries} if version == 1 else {"schema": "kei.tool-manifest/v2", "tools": entries}
        return json.dumps(manifest, indent=2, ensure_ascii=False)

    @staticmethod
    def _validate_registration(value: KeiToolRegistration) -> None:
        if not value.get("service", "").strip() or not value.get("source", "").strip():
            raise ValueError("v3 registration requires non-empty service and source")
        if value.get("operation_class") not in ("read", "write"):
            raise ValueError("invalid operation_class")
        route = value.get("route") or {}
        connector = route.get("connector_binding")
        harness = route.get("harness_executor")
        if (connector is None) == (harness is None):
            raise ValueError("route must contain exactly one branch")
        if connector is not None:
            caps = value.get("required_capabilities")
            if not connector.get("connector_id", "").strip() or not caps or any(not item.strip() for item in caps):
                raise ValueError("connector route requires binding and non-empty capabilities")
            return
        if harness is None or not harness.get("executor", "").strip() or not harness.get("registration", "").strip():
            raise ValueError("harness route requires explicit identity")
        if "required_capabilities" in value or "resource_types" in value:
            raise ValueError("harness route omits connector-only fields")

    def clear(self) -> None:
        self._tools.clear()
        self._registrations.clear()
