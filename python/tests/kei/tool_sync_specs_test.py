"""Typed-spec and manifest-path entry points for the tool manifest sync.

They must send exactly what ``sync_tool_manifest`` sends today for the same
manifest — the existing str/Mapping callers keep their behaviour.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from haikeilabs.agentware.kei import (
    ToolManifestSyncConfig,
    ToolManifestSyncError,
    sync_manifest_path,
    sync_tool_manifest,
    sync_tool_specs,
)
from haikeilabs.agentware.toolspec import ToolSpec, load_manifest_v3, serialize_tool_specs

TOKEN = "kei_rt_sync-secret-token-0123456789"
URL = "https://kei.example.test/"
COUNTS = {"upserted": 1, "unchanged": 0, "disabled": 0}
FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "toolspec" / "manifest-v3"


class Recorder:
    """An httpx transport that records every request and answers 200."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=COUNTS)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def config(**overrides: Any) -> ToolManifestSyncConfig:
    return ToolManifestSyncConfig(control_plane_url=URL, token=TOKEN, **overrides)


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="local.echo",
            description="Echo text back",
            service="codex",
            source="codex",
            operation_class="write",
            route={"harness_executor": {"executor": "codex", "registration": "local.echo"}},
        )
    ]


def test_sync_tool_specs_sends_exactly_what_sync_tool_manifest_sends() -> None:
    rec_specs = Recorder()
    result = sync_tool_specs(specs(), config(), transport=rec_specs.transport)
    assert result.sent and result.tool_count == 1

    rec_manifest = Recorder()
    sync_tool_manifest(serialize_tool_specs(specs()), config(), transport=rec_manifest.transport)
    (spec_req,) = rec_specs.requests
    (manifest_req,) = rec_manifest.requests
    assert spec_req.content == manifest_req.content
    assert spec_req.method == manifest_req.method == "PUT"
    assert str(spec_req.url) == "https://kei.example.test/api/v1/runtime/tools"
    assert spec_req.headers["Authorization"] == f"Bearer {TOKEN}"


def test_sync_manifest_path_sends_the_marshalled_loaded_specs() -> None:
    path = FIXTURE / "valid" / "minimal.json"
    rec = Recorder()
    result = sync_manifest_path(path, config(), transport=rec.transport)
    assert result.sent and result.tool_count == 1
    expected = serialize_tool_specs(load_manifest_v3(path))
    assert json.loads(rec.requests[0].content) == expected


def test_sync_tool_specs_with_no_tools_is_never_sent_when_tools_are_expected() -> None:
    rec = Recorder()
    with pytest.raises(ToolManifestSyncError, match="empty manifest"):
        sync_tool_specs([], config(), transport=rec.transport)
    assert rec.requests == []


def test_sync_tool_specs_skips_an_empty_manifest_when_no_tools_are_expected() -> None:
    rec = Recorder()
    result = sync_tool_specs([], config(expect_tools=False), transport=rec.transport)
    assert (result.sent, result.tool_count) == (False, 0)
    assert rec.requests == []
