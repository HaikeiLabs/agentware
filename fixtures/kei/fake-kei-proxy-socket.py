#!/usr/bin/env python3
"""Fake kei-proxy socket daemon for local development and CI.

Serves the kei-proxy runtime socket protocol (HTTP/1.1 over a Unix socket)
with canned authorize decisions. Python 3 stdlib only -- no dependencies.

Usage:
    python3 fixtures/kei/fake-kei-proxy-socket.py SOCKET_PATH --audit AUDIT_FILE

Canned decisions (matched on the ``tool`` field of the authorize request):
    echo                     -> permit (policy_id fake-allow-echo)
    delete_everything        -> deny  (no_matching_policy + hint)
    subject_unlinked_tool    -> deny  (subject_unlinked + hint + enrollment)
    preflight_required_tool  -> deny  (preflight_required + hint)
    anything else            -> deny  (tool_not_registered + hint)

Every authorize appends one JSON audit line to AUDIT_FILE:
    {"tool": T, "decision": D, "reason_code": C or "", "session": X-Kei-Session}
"""

import argparse
import json
import re
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

SESSION_ID = "fake-session"
AUTHORIZE_SCHEMA = "kei.authorize-response/v1"
_AUDIT_LOCK = threading.Lock()
_SESSIONS: dict[str, tuple[str, str]] = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    audit_path: str = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def address_string(self) -> str:
        return "unix"

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _send(self, status: int, body: Any = None) -> None:
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, code: str) -> None:
        self._send(
            status, {"error": {"code": code, "message": code, "type": "runtime_error"}}
        )

    def _authed(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth.startswith("Bearer ") and auth[len("Bearer ") :].strip() != ""

    def _audit(self, tool: str, decision: str, reason_code: str, session: str) -> None:
        harness, invoking_user = _SESSIONS.get(session, ("", ""))
        line = json.dumps(
            {
                "tool": tool,
                "decision": decision,
                "reason_code": reason_code,
                "session": session,
                "harness": harness,
                "invoking_user": invoking_user,
            }
        )
        with _AUDIT_LOCK, open(self.audit_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _route(self) -> None:
        if not self._authed():
            return self._error(401, "invalid_runtime_token")
        body = self._body()
        route = (self.command, self.path)
        if route == ("PUT", "/v1/session"):
            _SESSIONS[SESSION_ID] = (
                str(body.get("source", "")),
                str(body.get("external_id", "")),
            )
            external_id = str(body.get("external_id", ""))
            return self._send(
                201,
                {
                    "schema": "kei.session/v2",
                    "session_id": SESSION_ID,
                    "user_id": "u-" + external_id,
                    "status": "linked",
                },
            )
        if route == ("DELETE", "/v1/session"):
            return self._send(204)
        if route == ("POST", "/v1/tools:register"):
            if body.get("schema") != "kei.tool-manifest/v3" or not isinstance(
                body.get("tools"), list
            ):
                return self._error(400, "invalid_tool_manifest")
            for idx, tool in enumerate(body["tools"]):
                if not isinstance(tool, dict):
                    return self._send(
                        400,
                        {
                            "error": {
                                "code": "invalid_manifest",
                                "message": f"invalid tool manifest: tools[{idx}]: not an object",
                                "type": "runtime_error",
                            }
                        },
                    )
                src = tool.get("source", "")
                if not isinstance(src, str) or not SOURCE_RE.match(src):
                    return self._send(
                        400,
                        {
                            "error": {
                                "code": "invalid_manifest",
                                "message": f"invalid tool manifest: tools[{idx}]: invalid source",
                                "type": "runtime_error",
                            }
                        },
                    )
            return self._send(200, {"registered": len(body["tools"])})
        if route == ("POST", "/v1/authorize"):
            tool = str(body.get("tool", ""))
            session = self.headers.get("X-Kei-Session", "")
            if tool == "echo":
                answer: dict[str, Any] = {
                    "schema": AUTHORIZE_SCHEMA,
                    "decision": "permit",
                    "policy_id": "fake-allow-echo",
                    "bundle_state": "fresh",
                }
                self._audit(tool, "permit", "", session)
            elif tool == "delete_everything":
                answer = {
                    "schema": AUTHORIZE_SCHEMA,
                    "decision": "deny",
                    "reason_code": "no_matching_policy",
                    "reason": "no policy permits this tool",
                    "hint": "Add a policy for this tool: kei policies create",
                    "bundle_state": "fresh",
                }
                self._audit(tool, "deny", "no_matching_policy", session)
            elif tool == "subject_unlinked_tool":
                answer = {
                    "schema": AUTHORIZE_SCHEMA,
                    "decision": "deny",
                    "reason_code": "subject_unlinked",
                    "reason": "the subject is not linked to this installation",
                    "hint": "Link this identity to the installation: open the enrollment link when the response carries one, or link the user on the console Users page.",
                    "enrollment": {"claim_url": "https://kei.example.test/claim/1"},
                    "bundle_state": "fresh",
                }
                self._audit(tool, "deny", "subject_unlinked", session)
            elif tool == "preflight_required_tool":
                answer = {
                    "schema": AUTHORIZE_SCHEMA,
                    "decision": "deny",
                    "reason_code": "preflight_required",
                    "reason": "no registered tool by this name",
                    "hint": "Register the tool so the active policy bundle lists it (POST /v1/tools:register on the runtime socket, or kei harness sync --harness custom --manifest tools.json), then wait for the next bundle refresh.",
                    "bundle_state": "fresh",
                }
                self._audit(tool, "deny", "preflight_required", session)
            else:
                answer = {
                    "schema": AUTHORIZE_SCHEMA,
                    "decision": "deny",
                    "reason_code": "tool_not_registered",
                    "reason": "no registered tool by this name",
                    "hint": "Register the harness tools: kei harness sync --harness custom --manifest tools.json",
                    "bundle_state": "fresh",
                }
                self._audit(tool, "deny", "tool_not_registered", session)
            return self._send(200, answer)
        return self._error(404, "not_found")

    do_GET = do_PUT = do_POST = do_DELETE = _route  # noqa: N815 (http.server hook names)


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Fake kei-proxy socket daemon.")
    parser.add_argument("socket_path", help="Unix socket path to listen on")
    parser.add_argument(
        "--audit",
        required=True,
        help="File to append one JSON audit line per authorize",
    )
    args = parser.parse_args()

    Path(args.audit).parent.mkdir(parents=True, exist_ok=True)
    Path(args.socket_path).unlink(missing_ok=True)
    Handler.audit_path = args.audit
    server = UnixServer(args.socket_path, Handler)
    print(f"fake kei-proxy socket daemon on {args.socket_path}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        Path(args.socket_path).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
