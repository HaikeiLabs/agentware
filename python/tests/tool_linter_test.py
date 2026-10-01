import json
import os

from pedro_agentware.lint_tools import lint_manifest


def test_shared_v2_fixture_is_valid_and_deterministic():
    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "fixtures", "kei", "tool-manifest.v2.json"
    )
    raw = open(path, encoding="utf-8").read()
    assert lint_manifest(raw) == []


def test_linter_rejects_undeclared_capability_and_approval_fields():
    manifest = {
        "schema": "kei.tool-manifest/v2",
        "tools": [
            {
                "name": "bad",
                "source": "github",
                "required_capabilities": ["admin.raw_sql"],
                "resource_types": [{"type": "repo:org/*"}],
                "operation_class": "read",
                "allow": True,
            }
        ],
    }
    errors = lint_manifest(json.dumps(manifest, indent=2))
    assert any("capability" in error for error in errors)
    assert any("resource type" in error for error in errors)
    assert any("allow" in error for error in errors)
