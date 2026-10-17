#!/usr/bin/env python3
"""Minimal governed harness: one echo tool behind the kei-proxy socket client.

Runs a single authorize round-trip against the kei-proxy runtime socket and
prints the decision. With ``--fake`` it first starts the shared fake daemon
(``fixtures/kei/fake-kei-proxy-socket.py``) on a temp socket, so you can see a
governed allow and a governed deny in under a minute.
"""

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from haikeilabs.agentware.kei import (
    KeiProxySocketAuthorizeClient,
    KeiProxySocketIdentity,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FAKE_DAEMON = REPO_ROOT / "fixtures" / "kei" / "fake-kei-proxy-socket.py"
IDENTITY = KeiProxySocketIdentity("my-harness", "local")


def _start_fake(audit_path: str) -> tuple[subprocess.Popen[bytes], str]:
    directory = tempfile.mkdtemp(prefix="fakekei-", dir=REPO_ROOT)
    socket_path = str(Path(directory) / "runtime.sock")
    daemon = subprocess.Popen(
        [sys.executable, str(FAKE_DAEMON), socket_path, "--audit", audit_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if os.path.exists(socket_path):
            return daemon, socket_path
        if daemon.poll() is not None:
            raise RuntimeError("fake daemon exited before its socket appeared")
        time.sleep(0.02)
    raise TimeoutError("fake daemon socket did not appear")


def _print_decision(tool: str, answer: dict[str, object]) -> None:
    if answer.get("decision") in ("permit", "allow"):
        print(f"ALLOW tool={tool} policy_id={answer.get('policy_id', '')}")
    else:
        print(
            f"DENY tool={tool} reason_code={answer.get('reason_code', '')} hint={answer.get('hint', '')}"
        )


async def _run_demo() -> None:
    client = KeiProxySocketAuthorizeClient()
    session = await client.open_session(IDENTITY)
    try:
        _print_decision("echo", await client.authorize(session, tool="echo"))
        _print_decision(
            "delete_everything",
            await client.authorize(session, tool="delete_everything"),
        )
    finally:
        await client.close_session(session)


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal governed harness.")
    parser.add_argument(
        "--fake",
        action="store_true",
        help="start the shared fake kei-proxy socket daemon",
    )
    parser.add_argument(
        "--audit", default="", help="audit file for --fake (default: a temp file)"
    )
    args = parser.parse_args()

    daemon: subprocess.Popen[bytes] | None = None
    socket_dir = ""
    if args.fake:
        audit_path = args.audit or str(
            Path(tempfile.mkdtemp(prefix="fakekei-audit-", dir=REPO_ROOT))
            / "audit.jsonl"
        )
        daemon, socket_path = _start_fake(audit_path)
        socket_dir = str(Path(socket_path).parent)
        os.environ["KEI_RUNTIME_SOCKET_PATH"] = socket_path
        os.environ.setdefault("KEI_RUNTIME_TOKEN", "kei_rt_fake-local-token")
        print(f"audit={audit_path}", file=sys.stderr)
    elif not os.environ.get("KEI_RUNTIME_SOCKET_PATH"):
        parser.error(
            "KEI_RUNTIME_SOCKET_PATH is not set (run with --fake, or point it at a real kei-proxy)"
        )

    try:
        asyncio.run(_run_demo())
        return 0
    finally:
        if daemon is not None:
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
            Path(socket_dir, "runtime.sock").unlink(missing_ok=True)
            Path(socket_dir).rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
