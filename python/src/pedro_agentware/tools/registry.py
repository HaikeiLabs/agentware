"""Tool registry for managing available tools."""

import json
from typing import Any

from .tool import GovernedTool


class ToolRegistry:
    """Registry for managing tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}

    def register(self, tool: Any) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> tuple[Any, bool]:
        """Get a tool by name. Returns (tool, found)."""
        tool = self._tools.get(name)
        return tool, tool is not None

    def all(self) -> list[Any]:
        """Get all tools, sorted by name."""
        names = sorted(self._tools.keys())
        return [self._tools[name] for name in names]

    def names(self) -> list[str]:
        """Get all tool names, sorted."""
        return sorted(self._tools.keys())

    def schemas(self) -> dict[str, dict[str, Any]]:
        """Get input schemas for tools that support them."""
        schemas = {}
        for name, tool in self._tools.items():
            if hasattr(tool, "input_schema"):
                schemas[name] = tool.input_schema()
        return schemas

    def export_kei_tool_manifest(self, *, version: int = 2) -> str:
        """Export the Kei tool manifest as a JSON string.

        Only tools implementing GovernedTool are included. Version 1 is a
        transition export and must be selected explicitly.
        """
        if version not in (1, 2):
            raise ValueError("manifest version must be 1 or 2")
        entries = []
        names = sorted(name for name, tool in self._tools.items() if isinstance(tool, GovernedTool))
        for name in names:
            tool = self._tools[name]
            scope = tool.kei_scope()
            if version == 1:
                entry = {
                    "name": name,
                    "service": scope.service or scope.source,
                    "description": tool.description,
                    "action": scope.operation_class,
                    "resources": [item["type"] for item in scope.resource_types],
                    "enabled": True,
                }
            else:
                entry = {
                    "name": name,
                    "source": scope.source,
                    "required_capabilities": sorted(scope.required_capabilities),
                    "resource_types": sorted(
                        (dict(item) for item in scope.resource_types),
                        key=lambda item: (item["type"], item.get("parent_type", "")),
                    ),
                    "operation_class": scope.operation_class,
                }
                if scope.service:
                    entry["service"] = scope.service
                entry["description"] = tool.description
                entry["enabled"] = True
            entries.append(entry)

        manifest = (
            {"tools": entries}
            if version == 1
            else {"schema": "kei.tool-manifest/v2", "tools": entries}
        )
        return json.dumps(manifest, indent=2, ensure_ascii=False)

    def clear(self) -> None:
        """Clear all tools."""
        self._tools.clear()
