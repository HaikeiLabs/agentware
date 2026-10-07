"""Tests for Kei tool manifest export."""

import json
import os
import sys

import pytest

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


def test_v3_connector_route_wins_over_local_dispatch_handler():
    registry = ToolRegistry()
    registry.register(
        UngovernedEchoTool(),
        {
            "service": "local",
            "source": "harness",
            "operation_class": "write",
            "route": {"harness_executor": {"executor": "pi", "registration": "local_echo"}},
        },
    )
    registry.register(
        GovernedAddTool(),
        {
            "service": "github",
            "source": "github",
            "operation_class": "read",
            "route": {"connector_binding": {"agent_id": "agent-1", "connector_id": "binding-1"}},
            "required_capabilities": ["issue.read"],
            "resource_types": [{"type": "issue", "parent_type": "repository"}],
        },
    )
    entries = json.loads(registry.export_kei_tool_manifest(version=3))["tools"]
    assert entries[0]["name"] == "github.get_issue"
    assert isinstance(
        registry.get("github.get_issue")[0], GovernedAddTool
    )  # local execute handler remains registered
    assert entries[0]["route"] == {
        "connector_binding": {"agent_id": "agent-1", "connector_id": "binding-1"}
    }
    assert "harness_executor" not in entries[0]["route"]  # connector route wins
    assert entries[1]["route"] == {
        "harness_executor": {"executor": "pi", "registration": "local_echo"}
    }
    assert "required_capabilities" not in entries[1]
    assert "resource_types" not in entries[1]


def test_v3_connector_route_requires_agent_identity():
    registry = ToolRegistry()
    registry.register(
        GovernedAddTool(),
        {
            "service": "github",
            "source": "github",
            "operation_class": "read",
            "route": {"connector_binding": {"connector_id": "binding-1"}},
            "required_capabilities": ["issue.read"],
        },
    )
    try:
        registry.export_kei_tool_manifest(version=3)
    except ValueError as exc:
        assert "connector route" in str(exc)
    else:
        raise AssertionError("connector route without agent_id was exported")


def test_v3_export_rejects_empty_service_or_source():
    registry = ToolRegistry()
    registry.register(
        UngovernedEchoTool(),
        {
            "service": "",
            "source": "harness",
            "operation_class": "write",
            "route": {"harness_executor": {"executor": "pi", "registration": "local_echo"}},
        },
    )
    try:
        registry.export_kei_tool_manifest(version=3)
    except ValueError as exc:
        assert "service and source" in str(exc)
    else:
        raise AssertionError("invalid route registration was exported")


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


class V4Tool(GovernedAddTool):
    @property
    def description(self) -> str:
        return "Fetch issue"

    schema = None

    def input_schema(self) -> dict:
        return self.schema or {
            "type": "object",
            "properties": {"issue_number": {"type": "integer", "minimum": 1}},
            "required": ["issue_number"],
            "additionalProperties": False,
        }


def v4_registry(operation=None, *, caps=None, context=None, resources=None, schema=None):
    registry = ToolRegistry()
    operation = operation or {
        "id": "get-issue",
        "capability": "issue.read",
        "resource": {
            "type": "issue",
            "id": {"from": "args", "pointer": "/issue_number", "type": "integer"},
            "parent": {
                "type": "repository",
                "id": {"from": "context", "field": "repository", "type": "string"},
            },
        },
        "provider_resource_template": "repos/{parent.id}/issues/{resource.id}",
        "provider_input": {},
    }
    plan = {
        "context_schema": context
        or {
            "type": "object",
            "properties": {
                "repository": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "pattern": "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
                }
            },
            "required": ["repository"],
            "additionalProperties": False,
        },
        "operations": [operation],
    }
    tool = V4Tool()
    tool.schema = schema
    registry.register(
        tool,
        {
            "service": "github",
            "source": "github",
            "operation_class": "read",
            "route": {"connector_binding": {"agent_id": "agent-1", "connector_id": "github-1"}},
            "required_capabilities": caps if caps is not None else ["issue.read"],
            "resource_types": resources
            if resources is not None
            else [{"type": "issue", "parent_type": "repository"}],
            "plan": plan,
        },
    )
    return registry


def test_v4_explicit_export_matches_shared_fixture():
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    fixture_path = os.path.join(root, "fixtures", "kei", "tool-manifest.v4.json")
    with open(fixture_path, encoding="utf-8") as fixture_file:
        expected = json.load(fixture_file)
    assert json.loads(v4_registry().export_kei_tool_manifest(version=4)) == expected


def test_v4_rejects_malformed_plans_and_capability_coverage():
    base = v4_registry()._registrations["github.get_issue"]["plan"]["operations"][0]
    bad_type = json.loads(json.dumps(base))
    bad_type["resource"]["id"]["type"] = "string"
    undeclared = json.loads(json.dumps(base))
    undeclared["resource"]["parent"]["id"]["field"] = "missing"
    extra_ref = json.loads(json.dumps(base))
    extra_ref["provider_input"] = {
        "ref": {"from": "args", "pointer": "/issue_number", "type": "integer", "extra": True}
    }
    malformed = json.loads(json.dumps(base))
    malformed["provider_resource_template"] = "repos/{context.repository}/{resource.id}"
    for operation in (bad_type, undeclared, extra_ref, malformed):
        with pytest.raises(ValueError):
            v4_registry(operation).export_kei_tool_manifest(version=4)
    with pytest.raises(ValueError):
        v4_registry(caps=["issue.read", "issue.write"]).export_kei_tool_manifest(version=4)
    with pytest.raises(ValueError):
        v4_registry(caps=["issue.write"]).export_kei_tool_manifest(version=4)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda op: op.update(provider_resource_template="repos/{parent.id}/issues"),
        lambda op: op.update(provider_resource_template="issues/{resource.id}"),
        lambda op: op["resource"].update(unexpected=True),
        lambda op: op["resource"]["parent"].update(unexpected=True),
    ],
    ids=[
        "missing-resource-placeholder",
        "missing-parent-placeholder",
        "unknown-resource-key",
        "unknown-parent-key",
    ],
)
def test_v4_rejects_template_selector_omission_and_unknown_resource_keys(mutation):
    operation = v4_registry()._registrations["github.get_issue"]["plan"]["operations"][0]
    operation = json.loads(json.dumps(operation))
    mutation(operation)
    with pytest.raises(ValueError):
        v4_registry(operation).export_kei_tool_manifest(version=4)


def test_v4_provider_input_accepts_typed_object_and_array_refs():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "issue_number": {"type": "integer", "minimum": 1},
            "metadata": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "labels": {
                        "type": "array",
                        "maxItems": 4,
                        "items": {"type": "string", "maxLength": 30},
                    }
                },
            },
            "labels": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string", "maxLength": 30},
            },
        },
        "required": ["issue_number"],
    }
    operation = v4_registry()._registrations["github.get_issue"]["plan"]["operations"][0]
    operation = json.loads(json.dumps(operation))
    operation["provider_input"] = {
        "metadata": {"ref": {"from": "args", "pointer": "/metadata", "type": "object"}},
        "labels": {"ref": {"from": "args", "pointer": "/labels", "type": "array"}},
    }
    assert json.loads(v4_registry(operation, schema=schema).export_kei_tool_manifest(version=4))[
        "tools"
    ]
    operation["provider_input"]["labels"]["ref"]["type"] = "string"
    with pytest.raises(ValueError):
        v4_registry(operation, schema=schema).export_kei_tool_manifest(version=4)


def test_v4_resource_less_and_parent_only_collection_operations():
    no_resource = {"id": "noop", "capability": "issue.read", "provider_input": {}}
    assert json.loads(v4_registry(no_resource, resources=[]).export_kei_tool_manifest(version=4))[
        "tools"
    ]
    parent_only = {
        "id": "list",
        "capability": "issue.read",
        "resource": {
            "type": "issue",
            "parent": {
                "type": "repository",
                "id": {"from": "context", "field": "repository", "type": "string"},
            },
        },
        "provider_resource_template": "repos/{parent.id}/issues",
        "provider_input": {},
    }
    assert json.loads(v4_registry(parent_only).export_kei_tool_manifest(version=4))["tools"]
