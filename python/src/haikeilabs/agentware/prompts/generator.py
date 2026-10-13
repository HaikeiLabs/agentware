"""Prompt generator for tools."""

from typing import Any, Protocol

from ..tools import ToolRegistry


class PromptGenerator(Protocol):
    """Protocol for prompt generators."""

    def generate_tool_section(self, registry: ToolRegistry) -> str:
        """Generate the formatted tool documentation block."""
        ...

    def generate_tool_schemas(self, registry: ToolRegistry) -> list[dict[str, Any]]:
        """Generate JSON schema block for native tool calling."""
        ...


class DefaultPromptGenerator:
    """Default implementation of PromptGenerator."""

    def __init__(self) -> None:
        pass

    def generate_tool_section(self, registry: ToolRegistry) -> str:
        """Generate the tool section."""
        tools = registry.all()
        if not tools:
            return ""

        lines = ["## Available Tools\n"]
        for tool in tools:
            lines.append(f"- **{tool.name}**: {tool.description}")
        return "\n".join(lines)

    def generate_tool_schemas(self, registry: ToolRegistry) -> list[dict[str, Any]]:
        """Generate JSON schemas for tools."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": getattr(tool, "input_schema", {}) or {},
                },
            }
            for tool in registry.all()
        ]
