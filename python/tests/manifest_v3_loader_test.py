"""Shared kei.tool-manifest/v3 loader fixture replay (fixtures/toolspec/manifest-v3)."""

import json
from pathlib import Path
from typing import Any

import pytest
from conformance import record

from haikeilabs.agentware.toolspec import (
    ManifestV3Error,
    ToolSpec,
    load_manifest_v3,
    parse_manifest_v3,
    serialize_tool_specs,
)

INDEX = Path(__file__).resolve().parents[2] / "fixtures" / "toolspec" / "manifest-v3" / "index.json"


def _normalized(spec: ToolSpec) -> dict[str, Any]:
    """The README's expected normalization: every field present, lists never null."""
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters,
        "service": spec.service,
        "source": spec.source,
        "operation_class": spec.operation_class,
        "route": spec.route,
        "required_capabilities": spec.required_capabilities or [],
        "resource_types": spec.resource_types or [],
        "enabled": spec.enabled,
    }


def test_manifest_v3_loader_walks_every_fixture_case() -> None:
    index = json.loads(INDEX.read_text())
    assert index["schema"] == "agentware.manifest-v3-loader-cases/v1"

    for case in index["valid"]:
        input_path = INDEX.parent / case["input"]
        expected = json.loads((INDEX.parent / case["expected"]).read_text())
        specs = load_manifest_v3(input_path)
        assert [_normalized(spec) for spec in specs] == expected["tools"], case["name"]
        # Canonical round-trip: the existing marshaller reproduces the canonical file.
        canonical = json.loads((INDEX.parent / case["canonical"]).read_text())
        assert serialize_tool_specs(specs) == canonical, case["name"]
        record("toolspec/manifest-v3/index.json", "valid", case["name"])

    for case in index["invalid"]:
        path = INDEX.parent / case["input"]
        with pytest.raises(ManifestV3Error) as excinfo:
            load_manifest_v3(path)
        assert excinfo.value.code == case["error"], case["name"]
        record("toolspec/manifest-v3/index.json", "invalid", case["name"])


def test_parse_accepts_str_and_bytes() -> None:
    data = (INDEX.parent / "valid" / "minimal.json").read_bytes()
    assert parse_manifest_v3(data) == parse_manifest_v3(data.decode("utf-8"))


def test_load_of_a_missing_file_raises_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        load_manifest_v3(INDEX.parent / "valid" / "does-not-exist.json")


def test_error_messages_do_not_echo_field_values() -> None:
    document = {
        "schema": "kei.tool-manifest/v3",
        "tools": [
            {
                "name": "super-secret-tool-name",
                "service": "codex",
                "source": "codex",
                "operation_class": "delete",
                "route": {"harness_executor": {"executor": "codex", "registration": "codex.shell"}},
                "description": "Run a local harness action",
                "enabled": True,
            }
        ],
    }
    with pytest.raises(ManifestV3Error) as excinfo:
        parse_manifest_v3(json.dumps(document))
    assert excinfo.value.code == "invalid_operation_class"
    assert "super-secret-tool-name" not in str(excinfo.value)
