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

    def export_kei_tool_manifest(self) -> str:
        """Export the Kei tool manifest as a JSON string.

        Only tools implementing GovernedTool are included. The output is
        deterministic (sorted by name) and matches the catalog's
        POST /api/v1/tools create body shape.
        """
        entries = []
        names = sorted(name for name, tool in self._tools.items() if isinstance(tool, GovernedTool))
        for name in names:
            tool = self._tools[name]
            scope = tool.kei_scope()
            entry = {
                "name": name,
                "service": scope.service,
                "description": tool.description,
                "action": scope.action,
                "resources": list(scope.resources),
                "enabled": True,
            }
            entries.append(entry)

        manifest = {"tools": entries}
        return json.dumps(manifest, indent=2)

    def clear(self) -> None:
        """Clear all tools."""
        self._tools.clear()
