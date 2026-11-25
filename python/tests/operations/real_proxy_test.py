"""The OperationRunner end to end against a real ``kei-proxy serve``.

The daemon bootstraps from ``fixtures/kei/fake-control-plane.py`` serving its
``operation-plan`` bundle: a catalog-shaped ``kei.policy-bundle/v2`` with one
``connector_binding`` tool (``github.list_pulls``) whose plan derives a
``github.pulls.list`` operation from the tool arguments under an
``args_schema``, bound to a shared GitHub connector. The runner authorizes and
releases the credential over the real Unix socket; the provider call goes to
an ``httpx.MockTransport``. Nothing leaves the machine.

Skipped unless ``KEI_PROXY_TEST_BINARY`` names a kei-proxy binary (proxy-v0.11.0
or later, the first release that decides connector plans on the socket):

    go -C <kei-connector-runtime> build -o /tmp/kei-proxy ./cmd/kei-proxy
    KEI_PROXY_TEST_BINARY=/tmp/kei-proxy pytest tests/operations/real_proxy_test.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from kei.connectors.github.v1 import ops, pulls_pb2

from haikeilabs.agentware.kei import KeiProxySocketAuthorizeClient, KeiProxySocketIdentity
from haikeilabs.agentware.middleware.types import CallerContext
from haikeilabs.agentware.operations import (
    ConnectorEntry,
    OperationAudit,
    OperationBinding,
    OperationDeniedError,
    OperationRunner,
)

RUNTIME_TOKEN = "kh_live_ci_dummy_token_0123456789abcdef0123456789abcdef0123456789abcdef"
CONNECTOR_TOKEN = "fake-connector-token-e2e"
CONNECTOR_ID = "conn-github-ci"
TOOL = "github.list_pulls"
FAKE_CONTROL_PLANE = Path(__file__).resolve().parents[3] / "fixtures/kei/fake-control-plane.py"
BASE_URL = "http://github.test"


def _binary() -> str:
    binary = os.environ.get("KEI_PROXY_TEST_BINARY", "")
    if not binary or not Path(binary).is_file():
        pytest.skip("KEI_PROXY_TEST_BINARY is not set to a kei-proxy binary")
    return binary


class Runtime:
    """``kei-proxy serve`` and the fake control plane it bootstraps from."""

    def __init__(self, binary: str) -> None:
        # Short path: macOS limits a Unix socket path to 104 bytes.
        self.root = Path(tempfile.mkdtemp(prefix="kp-op-", dir="/tmp"))
        self.socket_path = str(self.root / "rt.sock")
        self.control_plane = subprocess.Popen(
            [sys.executable, str(FAKE_CONTROL_PLANE)],
            env={
                **os.environ,
                "FAKE_CONTROL_PLANE_BUNDLE": "operation-plan",
                "FAKE_CONTROL_PLANE_CONNECTOR_TOKEN": CONNECTOR_TOKEN,
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert self.control_plane.stdout is not None
        url = f"http://127.0.0.1:{self.control_plane.stdout.readline().strip()}"
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.root),
            "KEI_RUNTIME_TOKEN": RUNTIME_TOKEN,
            "KEI_RUNTIME_CONTROL_PLANE_URL": url,
            "KEI_RUNTIME_VERSION": "e2e",
            "KEI_RUNTIME_STATE_DIR": str(self.root / "state"),
            "KEI_RUNTIME_SOCKET_PATH": self.socket_path,
            "KEI_PROXY_LISTEN_ADDR": "127.0.0.1:0",
            "KEI_PROXY_AUDIT": str(self.root / "audit.jsonl"),
            "KEI_CREDENTIAL_RELEASE": "identity",
            "KEI_CREDENTIAL_RELEASE_URL": url,
            "ENV": "development",
        }
        self.proxy = subprocess.Popen(
            [binary, "serve"],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            self._wait_active()
        except Exception:
            self.stop()
            raise

    def _wait_active(self) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.proxy.poll() is not None:
                stderr = self.proxy.stderr.read().decode() if self.proxy.stderr else ""
                raise RuntimeError(f"kei-proxy serve exited: {stderr[-2000:]}")
            if os.path.exists(self.socket_path) and self._bundle_state() == "active":
                return
            time.sleep(0.1)
        raise TimeoutError("kei-proxy serve did not activate the operation-plan bundle")

    def _bundle_state(self) -> str:
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=self.socket_path),
                base_url="http://localhost",
                timeout=2,
            ) as client:
                response = client.get(
                    "/v1/policy-bundle/status",
                    headers={"Authorization": f"Bearer {RUNTIME_TOKEN}"},
                )
            body: dict[str, Any] = response.json() if response.status_code == 200 else {}
            return str(body.get("state", ""))
        except (httpx.HTTPError, ValueError):
            return ""

    def stop(self) -> None:
        for proc in (self.proxy, self.control_plane):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture
def runtime() -> Iterator[Runtime]:
    rt = Runtime(_binary())
    yield rt
    rt.stop()


class Provider:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=[{"number": 7, "title": "e2e"}])


async def _run(
    rt: Runtime, tool_args: dict[str, Any], provider: Provider, audit: list[OperationAudit]
) -> Any:
    socket = KeiProxySocketAuthorizeClient(socket_path=rt.socket_path, env=rt.env)
    session = await socket.open_session(KeiProxySocketIdentity("ci", "ci-user"))
    connectors = {
        "github": ConnectorEntry(
            connector_id=CONNECTOR_ID,
            connection_name="pat",
            kind="http_bearer",
            base_url=BASE_URL,
            operations={
                ops.OP_LIST_PULLS.id: OperationBinding(
                    pulls_pb2.ListPullsRequest, ops.GithubQueries
                )
            },
        )
    }
    http = httpx.AsyncClient(transport=httpx.MockTransport(provider.handler), base_url=BASE_URL)
    runner = OperationRunner(socket, session, connectors, audit=audit.append, http_client=http)
    try:
        return await runner.run(
            CallerContext(trusted=False),
            ops.OP_LIST_PULLS,
            pulls_pb2.ListPullsRequest(owner="acme", repository="widgets"),
            tool=TOOL,
            tool_args=tool_args,
            tool_context={},
        )
    finally:
        await http.aclose()


async def test_permit_and_credential_release_for_a_planned_tool(runtime: Runtime) -> None:
    provider, audit = Provider(), []
    await _run(runtime, {"owner": "acme", "repository": "widgets"}, provider, audit)

    assert len(provider.requests) == 1
    assert provider.requests[0].headers["authorization"] == f"Bearer {CONNECTOR_TOKEN}"
    assert provider.requests[0].url.path == "/repos/acme/widgets/pulls"
    assert audit == [OperationAudit("github.pulls.list", "allow", "success")]


async def test_tool_args_violating_args_schema_are_denied(runtime: Runtime) -> None:
    provider, audit = Provider(), []
    with pytest.raises(OperationDeniedError) as denied:
        await _run(
            runtime, {"owner": "acme", "repository": "widgets", "extra": "x"}, provider, audit
        )

    assert denied.value.reason_code == "operation_plan_invalid"
    assert CONNECTOR_TOKEN not in str(denied.value)
    assert provider.requests == []
    assert audit == [OperationAudit("github.pulls.list", "deny", "denied")]
