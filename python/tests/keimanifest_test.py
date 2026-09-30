"""Tests for Kei tool manifest export."""

import json
import os
import sys

sys.path.insert(0, "src")

from pedro_agentware.tools import BaseTool, GovernedTool, KeiScope, Result, ToolRegistry


class GovernedAddTool(BaseTool):
    """Governed tool for testing manifest export."""

    @property
    def name(self) -> str:
        return "github.get_issue"

    @property
    def description(self) -> str:
        return "Fetch an issue from a GitHub repository"

    def execute(self, args: dict) -> Result:
        return Result(success=True)

    def kei_scope(self) -> KeiScope:
        return KeiScope(
            service="github",
            action="read",
            resources=["repo:haikeilabs/*", "issue:*"],
        )


class GovernedListTool(BaseTool):
    @property
    def name(self) -> str:
        return "github.list_issues"

    @property
    def description(self) -> str:
        return "List issues in a GitHub repository"

    def execute(self, args: dict) -> Result:
        return Result(success=True)

    def kei_scope(self) -> KeiScope:
        return KeiScope(
            service="github",
            action="read",
            resources=["repo:haikeilabs/*", "issue:*"],
        )


class GovernedLinearTool(BaseTool):
    @property
    def name(self) -> str:
        return "linear.get_issue"

    @property
    def description(self) -> str:
        return "Fetch an issue from Linear"

    def execute(self, args: dict) -> Result:
        return Result(success=True)

    def kei_scope(self) -> KeiScope:
        return KeiScope(
            service="linear",
            action="read",
            resources=["team:*", "issue:*"],
        )


class GovernedSlackTool(BaseTool):
    @property
    def name(self) -> str:
        return "slack.post_message"

    @property
    def description(self) -> str:
        return "Post a message to a Slack channel"

    def execute(self, args: dict) -> Result:
        return Result(success=True)

    def kei_scope(self) -> KeiScope:
        return KeiScope(
            service="slack",
            action="write",
            resources=["channel:*"],
        )


class GovernedEmailTool(BaseTool):
    @property
    def name(self) -> str:
        return "send_email"

    @property
    def description(self) -> str:
        return "Send an email message"

    def execute(self, args: dict) -> Result:
        return Result(success=True)

    def kei_scope(self) -> KeiScope:
        return KeiScope(
            service="email",
            action="write",
            resources=[],
        )


class UngovernedEchoTool(BaseTool):
    """Tool that does NOT implement GovernedTool."""

    @property
    def name(self) -> str:
        return "local_echo"

    @property
    def description(self) -> str:
        return "Echo input back"

    def execute(self, args: dict) -> Result:
        return Result(success=True)

    # NOTE: no kei_scope() method — this tool should be excluded


def fixture_path() -> str:
    return os.path.join(
        os.path.dirname(__file__), "..", "..", "fixtures", "kei", "tool-manifest.v1.json"
    )


def load_fixture() -> dict:
    with open(fixture_path()) as f:
        return json.load(f)


def test_export_matches_fixture():
    registry = ToolRegistry()
    registry.register(GovernedAddTool())
    registry.register(GovernedListTool())
    registry.register(GovernedLinearTool())
    registry.register(GovernedSlackTool())
    registry.register(GovernedEmailTool())
    registry.register(UngovernedEchoTool())

    manifest_str = registry.export_kei_tool_manifest()
    manifest = json.loads(manifest_str)
    expected = load_fixture()

    assert manifest == expected, (
        f"Manifest does not match fixture\n--- got:\n{json.dumps(manifest, indent=2)}\n"
        f"--- want:\n{json.dumps(expected, indent=2)}"
    )


def test_export_empty_registry():
    registry = ToolRegistry()
    manifest_str = registry.export_kei_tool_manifest()
    manifest = json.loads(manifest_str)
    assert manifest == {"tools": []}, f"Expected empty tools, got {manifest}"


def test_export_only_governed_tools():
    registry = ToolRegistry()
    registry.register(UngovernedEchoTool())
    manifest_str = registry.export_kei_tool_manifest()
    manifest = json.loads(manifest_str)
    assert manifest == {"tools": []}, "Ungoverned tools should not appear in manifest"


def test_kei_scope_dataclass():
    scope = KeiScope(service="test", action="write", resources=["r1", "r2"])
    assert scope.service == "test"
    assert scope.action == "write"
    assert scope.resources == ["r1", "r2"]


def test_kei_scope_default_resources():
    scope = KeiScope(service="test", action="read")
    assert scope.resources == []


def test_governed_tool_protocol_check():
    """Verify that isinstance check works at runtime."""
    tool = GovernedAddTool()
    assert isinstance(tool, GovernedTool)


def test_ungoverned_tool_protocol_check():
    """Ungoverned tools should NOT pass isinstance(GovernedTool)."""
    tool = UngovernedEchoTool()
    assert not isinstance(tool, GovernedTool)
