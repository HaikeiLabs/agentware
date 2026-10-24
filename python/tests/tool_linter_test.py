import json
import os

from haikeilabs.agentware.lint_tools import lint_manifest


def test_shared_v3_fixture_is_valid_and_deterministic():
    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "fixtures", "kei", "tool-manifest.v3.json"
    )
    raw = open(path, encoding="utf-8").read()
    assert lint_manifest(raw) == []


def test_linter_rejects_undeclared_capability_and_approval_fields():
    manifest = {
        "schema": "kei.tool-manifest/v3",
        "tools": [
            {
                "name": "bad",
                "source": "github",
                "required_capabilities": ["admin.raw_sql"],
                "resource_types": [{"type": "repo:org/*"}],
                "operation_class": "read",
                "allow": True,
                "route": {"connector_binding": {"agent_id": "agent", "connector_id": "binding"}},
            }
        ],
    }
    errors = lint_manifest(json.dumps(manifest, indent=2))
    assert any("capability" in error for error in errors)
    assert any("resource type" in error for error in errors)
    assert any("allow" in error for error in errors)


def test_linter_rejects_unsupported_schema():
    assert lint_manifest('{"schema":"unsupported","tools":[]}') == [
        "schema must be kei.tool-manifest/v3"
    ]
