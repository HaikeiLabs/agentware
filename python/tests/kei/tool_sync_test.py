"""Tool manifest sync: the manifest reaches Kei in replace mode, or the harness does not serve."""

import json
import logging
from typing import Any

import httpx
import pytest

from pedro_agentware.kei import (
    TOOL_MANIFEST_V2_MEDIA_TYPE,
    ToolManifestSyncConfig,
    ToolManifestSyncError,
    manifest_tool_count,
    sync_tool_manifest,
)

TOKEN = "kei_rt_sync-secret-token-0123456789"
URL = "https://kei.example.test/"
COUNTS = {"upserted": 2, "unchanged": 0, "disabled": 0}
MANIFEST = {
    "schema": "kei.tool-manifest/v2",
    "tools": [{"name": "github.get_issue"}, {"name": "github.create_issue"}],
}


class Recorder:
    """An httpx transport that answers from a script and records every request."""

    def __init__(self, *answers: httpx.Response | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def config(**overrides: Any) -> ToolManifestSyncConfig:
    return ToolManifestSyncConfig(control_plane_url=URL, token=TOKEN, **overrides)


def ok() -> httpx.Response:
    return httpx.Response(200, json=COUNTS)


def test_sync_puts_the_manifest_with_the_runtime_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rec = Recorder(ok())
    result = sync_tool_manifest(json.dumps(MANIFEST), config(), transport=rec.transport)
    assert (result.sent, result.tool_count, result.counts) == (True, 2, COUNTS)
    (req,) = rec.requests
    assert req.method == "PUT"
    assert str(req.url) == "https://kei.example.test/api/v1/runtime/tools"
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert req.headers["Content-Type"] == TOOL_MANIFEST_V2_MEDIA_TYPE
    assert req.headers["Accept"] == TOOL_MANIFEST_V2_MEDIA_TYPE
    assert json.loads(req.content) == MANIFEST
    assert "registered 2 tool(s)" in caplog.text and "upserted=2" in caplog.text
    assert TOKEN not in caplog.text


def test_a_mapping_manifest_is_accepted() -> None:
    rec = Recorder(ok())
    assert sync_tool_manifest(MANIFEST, config(), transport=rec.transport).sent
    assert json.loads(rec.requests[0].content) == MANIFEST


@pytest.mark.parametrize(
    "manifest", [{"tools": []}, {"schema": "x"}, {"tools": "nope"}, '{"tools": []}']
)
def test_an_empty_manifest_is_never_sent_when_tools_are_expected(manifest: Any) -> None:
    rec = Recorder()
    with pytest.raises(ToolManifestSyncError, match="empty manifest"):
        sync_tool_manifest(manifest, config(), transport=rec.transport)
    assert rec.requests == []


def test_an_empty_manifest_is_skipped_when_no_tools_are_expected() -> None:
    rec = Recorder()
    result = sync_tool_manifest({"tools": []}, config(), expect_tools=False, transport=rec.transport)
    assert (result.sent, result.tool_count) == (False, 0)
    assert rec.requests == []


@pytest.mark.parametrize("manifest", ["{not json", "[1, 2]"])
def test_an_unreadable_manifest_is_refused(manifest: str) -> None:
    with pytest.raises(ToolManifestSyncError):
        sync_tool_manifest(manifest, config(), transport=Recorder().transport)


def test_sync_retries_with_backoff_then_succeeds() -> None:
    rec = Recorder(httpx.ConnectError("refused"), httpx.Response(503, text="unavailable"), ok())
    slept: list[float] = []
    result = sync_tool_manifest(MANIFEST, config(), transport=rec.transport, sleep=slept.append)
    assert result.sent and len(rec.requests) == 3
    assert slept == [1.0, 2.0]


def test_sync_gives_up_and_never_leaks_the_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    echo = httpx.Response(401, text=f"bad token {TOKEN}")
    rec = Recorder(echo, httpx.Response(401, text=f"bad token {TOKEN}"), httpx.ReadTimeout(TOKEN))
    with pytest.raises(ToolManifestSyncError) as exc:
        sync_tool_manifest(
            MANIFEST, config(attempts=3), transport=rec.transport, sleep=lambda _: None
        )
    assert "after 3 attempt(s)" in str(exc.value)
    assert TOKEN not in str(exc.value)
    assert TOKEN not in caplog.text


def test_backoff_is_capped() -> None:
    rec = Recorder(*[httpx.Response(500)] * 7)
    slept: list[float] = []
    with pytest.raises(ToolManifestSyncError):
        sync_tool_manifest(
            MANIFEST,
            config(attempts=7, backoff_seconds=4.0),
            transport=rec.transport,
            sleep=slept.append,
        )
    assert slept == [4.0, 8.0, 16.0, 16.0, 16.0, 16.0]


def test_the_token_is_not_in_the_config_repr() -> None:
    assert TOKEN not in repr(config())


def test_from_env_reads_what_kei_proxy_reads() -> None:
    cfg = ToolManifestSyncConfig.from_env(
        {
            "KEI_RUNTIME_CONTROL_PLANE_URL": URL,
            "KEI_RUNTIME_TOKEN": TOKEN,
            "KEI_PROXY_EGRESS_PROXY": "http://egress:3128",
            "KEI_PROXY_NO_PROXY": "internal.test",
        }
    )
    assert cfg.tools_url == "https://kei.example.test/api/v1/runtime/tools"
    assert (cfg.egress_proxy, cfg.no_proxy) == ("http://egress:3128", "internal.test")


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"KEI_RUNTIME_TOKEN": TOKEN}, "KEI_RUNTIME_CONTROL_PLANE_URL is not set"),
        ({"KEI_RUNTIME_CONTROL_PLANE_URL": URL}, "KEI_RUNTIME_TOKEN is not set"),
        (
            {"KEI_RUNTIME_CONTROL_PLANE_URL": "ftp://x", "KEI_RUNTIME_TOKEN": TOKEN},
            "not an http",
        ),
    ],
)
def test_from_env_fails_closed(env: dict[str, str], message: str) -> None:
    with pytest.raises(ToolManifestSyncError, match=message):
        ToolManifestSyncConfig.from_env(env)


def test_egress_proxy_and_no_proxy() -> None:
    assert config().proxy() is None
    assert config(egress_proxy="http://egress:3128").proxy() == "http://egress:3128"
    bypass = config(egress_proxy="http://egress:3128", no_proxy="example.test,other")
    assert bypass.proxy() is None
    other = config(egress_proxy="http://egress:3128", no_proxy="internal.test")
    assert other.proxy() == "http://egress:3128"
    assert config(egress_proxy="http://egress:3128", no_proxy="*").proxy() is None


def test_only_the_egress_contract_decides_the_proxy() -> None:
    from pedro_agentware.kei import tool_sync

    with tool_sync._client(config(egress_proxy="http://egress:3128"), None) as client:
        assert client.trust_env is False  # ambient HTTP(S)_PROXY is ignored
    with tool_sync._client(config(), None) as client:
        assert client.trust_env is True  # no egress proxy: the environment's, as kei-proxy


def test_manifest_tool_count() -> None:
    assert manifest_tool_count(MANIFEST) == 2
    assert manifest_tool_count(json.dumps(MANIFEST)) == 2
    assert manifest_tool_count({"tools": None}) == 0
