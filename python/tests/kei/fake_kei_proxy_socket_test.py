"""Contract test: the real socket client against the fake kei-proxy socket daemon.

Starts ``fixtures/kei/fake-kei-proxy-socket.py`` as a subprocess on a short temp
Unix socket path and drives it with the real ``KeiProxySocketAuthorizeClient``.
The daemon's canned decisions (``echo`` -> permit, ``delete_everything`` -> deny
with a reason_code + hint, anything else -> deny ``tool_not_registered``) and
its audit file are the shared contract for the Go and TS CI workers.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from haikeilabs.agentware.kei import KeiProxySocketAuthorizeClient, KeiProxySocketIdentity
from haikeilabs.agentware.kei import KeiProxySocketError

TOKEN = "kei_rt_fake-socket-secret-0123456789"
SCRIPT = Path(__file__).resolve().parents[3] / "fixtures" / "kei" / "fake-kei-proxy-socket.py"
REPO_ROOT = SCRIPT.parents[2]


def _wait_for_socket(path: str, proc: subprocess.Popen[bytes], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return
        if proc.poll() is not None:
            _, err = proc.communicate()
            raise RuntimeError(f"daemon exited early ({proc.returncode}): {err.decode()}")
        time.sleep(0.02)
    raise TimeoutError(f"socket {path} did not appear within {timeout}s")


@pytest.fixture
def fake_daemon() -> Iterator[tuple[str, str]]:
    # macOS caps socket paths near 104 bytes; keep the temp dir short.
    directory = tempfile.mkdtemp(prefix="fakekei-", dir=REPO_ROOT)
    socket_path = str(Path(directory) / "runtime.sock")
    audit_path = str(Path(directory) / "audit.jsonl")
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), socket_path, "--audit", audit_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_socket(socket_path, proc)
        yield socket_path, audit_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        Path(socket_path).unlink(missing_ok=True)
        Path(audit_path).unlink(missing_ok=True)
        Path(directory).rmdir()


def _client(path: str) -> KeiProxySocketAuthorizeClient:
    return KeiProxySocketAuthorizeClient(
        env={"KEI_RUNTIME_SOCKET_PATH": path, "KEI_RUNTIME_TOKEN": TOKEN}
    )


async def test_tool_manifest_registration_uses_runtime_route(
    fake_daemon: tuple[str, str],
) -> None:
    client = _client(fake_daemon[0])
    result = await client.register_tools(
        {"schema": "kei.tool-manifest/v3", "tools": [{"name": "x", "source": "minimal_harness"}]}
    )
    assert result == {"registered": 1}


async def test_tool_manifest_invalid_source_returns_400(
    fake_daemon: tuple[str, str],
) -> None:
    client = _client(fake_daemon[0])
    manifest = {
        "schema": "kei.tool-manifest/v3",
        "tools": [{"name": "x", "source": "bad-source"}],
    }
    with pytest.raises(KeiProxySocketError) as excinfo:
        await client.register_tools(manifest)
    assert excinfo.value.code == "invalid_manifest"


async def test_tool_manifest_valid_source_registers(
    fake_daemon: tuple[str, str],
) -> None:
    client = _client(fake_daemon[0])
    result = await client.register_tools(
        {
            "schema": "kei.tool-manifest/v3",
            "tools": [{"name": "x", "source": "minimal_harness"}],
        }
    )
    assert result == {"registered": 1}


async def test_echo_is_permitted_and_delete_is_denied_with_a_hint(
    fake_daemon: tuple[str, str],
) -> None:
    socket_path, audit_path = fake_daemon
    client = _client(socket_path)
    session = await client.open_session(KeiProxySocketIdentity("openwebui", "ada"))

    allowed = await client.authorize(session, tool="echo")
    assert allowed["decision"] == "permit"
    assert allowed["policy_id"] == "fake-allow-echo"

    denied = await client.authorize(session, tool="delete_everything")
    assert denied["decision"] == "deny"
    assert denied["reason_code"] == "no_matching_policy"
    assert denied["hint"] == "Add a policy for this tool: kei policies create"

    unknown = await client.authorize(session, tool="unregistered_tool")
    assert unknown["decision"] == "deny"
    assert unknown["reason_code"] == "tool_not_registered"
    assert unknown["hint"] == (
        "Register the harness tools: kei harness sync --harness custom --manifest tools.json"
    )

    await client.close_session(session)

    lines = [json.loads(line) for line in Path(audit_path).read_text().splitlines() if line.strip()]
    assert lines == [
        {
            "tool": "echo",
            "decision": "permit",
            "reason_code": "",
            "session": "fake-session",
            "harness": "openwebui",
            "invoking_user": "ada",
        },
        {
            "tool": "delete_everything",
            "decision": "deny",
            "reason_code": "no_matching_policy",
            "session": "fake-session",
            "harness": "openwebui",
            "invoking_user": "ada",
        },
        {
            "tool": "unregistered_tool",
            "decision": "deny",
            "reason_code": "tool_not_registered",
            "session": "fake-session",
            "harness": "openwebui",
            "invoking_user": "ada",
        },
    ]
