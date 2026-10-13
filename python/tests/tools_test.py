"""Tests for tools package."""

import sys

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
