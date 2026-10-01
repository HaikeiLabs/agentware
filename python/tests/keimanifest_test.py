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
            source="github",
            required_capabilities=["issue.read"],
            resource_types=[{"type": "issue", "parent_type": "repository"}],
            operation_class="read",
            service="github",
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
            source="github",
            required_capabilities=["issue.read"],
            resource_types=[{"type": "issue", "parent_type": "repository"}],
            operation_class="read",
            service="github",
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
            source="linear",
            required_capabilities=["issue.read"],
            resource_types=[{"type": "issue", "parent_type": "team"}],
            operation_class="read",
            service="linear",
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
            source="github",
            required_capabilities=["issue.comment"],
            resource_types=[{"type": "issue", "parent_type": "repository"}],
            operation_class="write",
            service="slack",
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
            source="github",
            required_capabilities=["issue.create"],
            resource_types=[],
            operation_class="write",
            service="email",
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
        os.path.dirname(__file__), "..", "..", "fixtures", "kei", "tool-manifest.v2.json"
    )


def load_fixture() -> dict:
    with open(fixture_path()) as f:
        return json.load(f)


def test_export_matches_fixture():
    registry = ToolRegistry()
    registry.register(GovernedAddTool())
    registry.register(GovernedListTool())
    registry.register(GovernedLinearTool())
    registry.register(UngovernedEchoTool())

    manifest_str = registry.export_kei_tool_manifest()
    manifest = json.loads(manifest_str)
    expected = load_fixture()

    assert manifest == expected, f"Manifest does not match fixture: {manifest} != {expected}"
    assert manifest_str == open(fixture_path(), encoding="utf-8").read().rstrip("\n")


def test_v1_export_requires_explicit_option():
    registry = ToolRegistry()
    registry.register(GovernedAddTool())
    manifest = json.loads(registry.export_kei_tool_manifest(version=1))
    assert "schema" not in manifest
    assert manifest["tools"][0]["action"] == "read"


def test_export_empty_registry():
    registry = ToolRegistry()
    manifest_str = registry.export_kei_tool_manifest()
    manifest = json.loads(manifest_str)
    assert manifest == {"schema": "kei.tool-manifest/v2", "tools": []}, (
        f"Expected empty tools, got {manifest}"
    )


def test_export_only_governed_tools():
    registry = ToolRegistry()
    registry.register(UngovernedEchoTool())
    manifest_str = registry.export_kei_tool_manifest()
    manifest = json.loads(manifest_str)
    assert manifest == {"schema": "kei.tool-manifest/v2", "tools": []}, (
        "Ungoverned tools should not appear in manifest"
    )


def test_kei_scope_dataclass():
    scope = KeiScope(
        source="github", required_capabilities=["issue.create"], operation_class="write"
    )
    assert scope.source == "github"
    assert scope.operation_class == "write"
    assert scope.required_capabilities == ["issue.create"]


def test_kei_scope_default_resources():
    scope = KeiScope(source="github", required_capabilities=["issue.read"])
    assert scope.resource_types == []


def test_governed_tool_protocol_check():
    """Verify that isinstance check works at runtime."""
    tool = GovernedAddTool()
    assert isinstance(tool, GovernedTool)


def test_ungoverned_tool_protocol_check():
    """Ungoverned tools should NOT pass isinstance(GovernedTool)."""
    tool = UngovernedEchoTool()
    assert not isinstance(tool, GovernedTool)
