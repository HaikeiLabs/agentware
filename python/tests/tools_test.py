"""Tests for tools package."""

import sys
from typing import Any

sys.path.insert(0, "src")

from haikeilabs.agentware.tools import BaseTool, Result, ToolRegistry


class AddTool(BaseTool):
    @property
    def name(self) -> str:
        return "add"

    @property
    def description(self) -> str:
        return "Add two numbers"

    def execute(self, args: dict) -> Result:
        a = args.get("a", 0)
        b = args.get("b", 0)
        return Result(success=True, data=a + b)


def test_tool_registry_register():
    registry = ToolRegistry()
    tool = AddTool()
    registry.register(tool)
    assert tool.name in registry.names()


def test_tool_registry_get():
    registry = ToolRegistry()
    tool = AddTool()
    registry.register(tool)
    found, ok = registry.get("add")
    assert ok
    assert found.name == "add"


def test_tool_registry_all():
    registry = ToolRegistry()
    tool = AddTool()
    registry.register(tool)
    all_tools = registry.all()
    assert len(all_tools) == 1


def test_result_to_dict():
    result = Result(success=True, data=42)
    d = result.to_dict()
    assert d["success"] is True
    assert d["data"] == 42


ATTR_SCHEMA = {"type": "object", "properties": {"limit": {"type": "integer"}}}
METHOD_SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}}}


class AttrSchemaTool:
    """Tool that declares its JSON schema as a plain dict attribute."""

    name = "attr_tool"
    description = "attribute-style schema tool"

    def __init__(self) -> None:
        self.input_schema: dict[str, Any] = ATTR_SCHEMA

    def execute(self, args: dict) -> Result:
        return Result(success=True)


class MethodSchemaTool:
    """Tool that declares its JSON schema via a zero-argument method."""

    name = "method_tool"
    description = "method-style schema tool"

    def input_schema(self) -> dict[str, Any]:
        return METHOD_SCHEMA

    def execute(self, args: dict) -> Result:
        return Result(success=True)


def test_tool_registry_schemas_accepts_attribute_and_method_schemas():
    """schemas() reads both declaration styles instead of calling dicts."""
    registry = ToolRegistry()
    registry.register(AttrSchemaTool())
    registry.register(MethodSchemaTool())
    registry.register(AddTool())  # no schema: excluded, as before

    assert registry.schemas() == {
        "attr_tool": ATTR_SCHEMA,
        "method_tool": METHOD_SCHEMA,
    }
