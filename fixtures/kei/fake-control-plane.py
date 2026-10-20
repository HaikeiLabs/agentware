#!/usr/bin/env python3
"""Fake kei-proxy control-plane HTTP stub for CI.

Serves control-plane endpoints that kei-proxy serve calls during
startup and policy refresh.  Python 3 stdlib only -- no dependencies.

Endpoints:
    GET  /api/v1/runtime/whoami             -> identity JSON
    GET  /api/v1/runtime/policy-bundles/current -> v2 policy bundle + ETag
    POST /api/v1/runtime:heartbeat           -> 204 No Content
    PUT  /api/v1/runtime/tools               -> 200 ack

Usage:
    python3 fixtures/kei/fake-control-plane.py
    # prints the ephemeral port on stdout, then serves until killed
"""

import hashlib
import http.server
import json
import os
import socketserver
import sys
import time
from typing import Any

# ---------------------------------------------------------------------------
# canned responses
# ---------------------------------------------------------------------------

WHOAMI_BODY = {
    "id": "ci-installation",
    "org_id": "ci-org",
    "workspace_id": "ci-ws",
    "platform": "ci",
    "status": "active",
    "binding_status": "bound",
    "agent_id": "ci-agent",
}

_now = time.time()
_issued_at = time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime(_now)) + "000Z"
_not_after = time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime(_now + 3600)) + "000Z"

# v2 policy bundle shaped to match kei-connector-runtime's
# docs/contracts/policy-bundle-v2.schema.json (additionalProperties: false at
# all levels).  Only fields listed in that schema and in policy-set-v2.schema.json
# are present.
POLICY_BUNDLE: dict[str, Any] = {
    "schema": "kei.policy-bundle/v2",
    "bundle_id": "ci-bundle-001",
    "bundle_version": 1,
    "policy_revision": 1,
    "issuer": "kei-policy-catalog",
    "audience": {
        "installation_id": "ci-installation",
        "org_id": "ci-org",
        "workspace_id": "ci-ws",
    },
    "issued_at": _issued_at,
    "not_after": _not_after,
    "refresh": {
        "poll_interval_seconds": 120,
        "stale_after_seconds": 600,
    },
    "subject_resolution": {
        "mode": "bundle",
        "max_cache_seconds": 30,
    },
    "policy_set": {
        "schema": "kei.policy-set/v2",
        "org_id": "ci-org",
        "workspace_id": "ci-ws",
        "match_semantics": "kei.match/v2",
        "evaluation": {
            "precedence": [
                "priority_desc",
                "deny_before_permit",
                "policy_id_asc",
            ],
            "checks": "per_capability_first_match_and",
            "default": "deny",
        },
        "tools": [],
        "checks": [],
        "tool_policies": [
            {
                "policy_id": "policy-allow-echo",
                "name": "allow echo",
                "effect": "permit",
                "priority": 100,
                "scope": {
                    "org_id": "ci-org",
                    "workspace_id": "ci-ws",
                    "agent_id": None,
                },
                "selector": {
                    "source_principal": "*",
                    "tool": "echo",
                },
            },
            {
                "policy_id": "policy-deny-delete-everything",
                "name": "deny delete_everything",
                "effect": "deny",
                "priority": 100,
                "scope": {
                    "org_id": "ci-org",
                    "workspace_id": "ci-ws",
                    "agent_id": None,
                },
                "selector": {
                    "source_principal": "*",
                    "tool": "delete_everything",
                },
            },
        ],
        "harness_match_semantics": "kei.harness-match/v1",
        "harness_policies": [],
    },
    "agent_connector_bindings": [],
    "identities": [
        {
            "source": "ci",
            "external_id": "ci-user",
            "user_id": "u-ci-user",
            "status": "linked",
            "groups": ["everyone"],
        }
    ],
    "subject": {
        "user_id": "ci-installer",
        "email": "ci@example.com",
        "groups": ["everyone"],
    },
    "agent_model_profiles": [],
}

POLICY_BUNDLE_BYTES: bytes = json.dumps(POLICY_BUNDLE, indent=2).encode("utf-8")
POLICY_BUNDLE_ETAG = '"sha256:' + hashlib.sha256(POLICY_BUNDLE_BYTES).hexdigest() + '"'


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        """Log method + path + status to stderr.

        Called by the base class for every request including unsupported
        methods (501).  The default format is '"%s" %s %s' (requestline,
        status, size).  We override to capture the method name and path
        without leaking header values.
        """
        status = args[0] if args else "?"
        print(
            f"[{self.command}] {self.path} -> {status}",
            file=sys.stderr,
            flush=True,
        )

    def _read_body(self) -> bytes:
        """Read and return the request body bytes.

        Python's BaseHTTPRequestHandler does not read request bodies by
        default — the do_* handlers are responsible for that.  This method
        reads the body and returns it, handling both Content-Length and
        Transfer-Encoding: chunked.
        """
        content_length = self.headers.get("Content-Length")
        if content_length is not None:
            length = int(content_length)
            if length > 0:
                return self.rfile.read(length)
            return b""

        transfer_encoding = self.headers.get("Transfer-Encoding", "")
        if transfer_encoding.lower() == "chunked":
            chunks: list[bytes] = []
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                chunk_size_str = line.strip()
                if not chunk_size_str:
                    continue
                try:
                    chunk_size = int(chunk_size_str, 16)
                except ValueError:
                    break
                if chunk_size == 0:
                    # Read trailing CRLF (end of trailer section)
                    self.rfile.readline()
                    break
                # Read chunk data + trailing CRLF
                chunks.append(self.rfile.read(chunk_size))
                self.rfile.readline()  # trailing CRLF
            return b"".join(chunks)

        return b""

    def _consume_body(self) -> None:
        """Read and discard the request body.

        Python's BaseHTTPRequestHandler does not read request bodies by
        default — the do_* handlers are responsible for that.  If a handler
        returns a response without consuming the body, leftover data on the
        socket will corrupt the next request on a keep-alive connection.

        This method handles both Content-Length and Transfer-Encoding: chunked.
        Call it at the top of every do_* handler that receives a body.
        """
        self._read_body()

    def _accepts(self, media_type: str) -> bool:
        """Check if the request Accept header includes *media_type*."""
        accept = self.headers.get("Accept", "")
        for part in accept.split(","):
            mt = part.split(";")[0].strip()
            if mt == media_type:
                return True
        return False

    def _check_token(self) -> bool:
        """Return True if a non-empty token is present (Bearer or X-Kei-Token)."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and len(auth) > 7:
            return True
        if self.headers.get("X-Kei-Token", ""):
            return True
        return False

    def _send_json(
        self, status: int, body: Any, headers: dict[str, str] | None = None
    ) -> None:
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        out_headers = dict(headers or {})
        out_headers.setdefault("Content-Type", "application/json")
        self.send_response(status)
        for k, v in out_headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _send_raw(
        self, status: int, data: bytes, headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _send_empty(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_whoami(self) -> None:
        self._send_json(200, WHOAMI_BODY)

    def _handle_policy_bundles_current(self) -> None:
        inm = self.headers.get("If-None-Match", "")
        if inm and (
            inm == POLICY_BUNDLE_ETAG or inm.strip('"') == POLICY_BUNDLE_ETAG.strip('"')
        ):
            self._send_empty(304)
            return
        self._send_raw(
            200,
            POLICY_BUNDLE_BYTES,
            headers={
                "Content-Type": "application/vnd.kei.policy-bundle.v2+json",
                "ETag": POLICY_BUNDLE_ETAG,
                "Kei-Policy-Bundle-Version": "1",
                "Cache-Control": "no-store",
            },
        )

    def do_GET(self) -> None:
        if not self._check_token():
            return self._send_json(401, {"error": "invalid_runtime_token"})

        if self.path == "/api/v1/runtime/whoami":
            return self._handle_whoami()
        if self.path == "/api/v1/runtime/policy-bundles/current":
            return self._handle_policy_bundles_current()

        self._send_json(404, {"error": "not_found"})

    def do_HEAD(self) -> None:
        """HEAD responds like GET but omits the body."""
        if not self._check_token():
            self._send_empty(401)
            return

        if self.path == "/api/v1/runtime/whoami":
            self._send_empty(200, headers={"Content-Type": "application/json"})
            return
        if self.path == "/api/v1/runtime/policy-bundles/current":
            # Match GET: check etag, respond with same headers but no body
            inm = self.headers.get("If-None-Match", "")
            if inm and (
                inm == POLICY_BUNDLE_ETAG
                or inm.strip('"') == POLICY_BUNDLE_ETAG.strip('"')
            ):
                self._send_empty(304)
                return
            self._send_empty(
                200,
                headers={
                    "Content-Type": "application/vnd.kei.policy-bundle.v2+json",
                    "ETag": POLICY_BUNDLE_ETAG,
                    "Kei-Policy-Bundle-Version": "1",
                    "Cache-Control": "no-store",
                },
            )
            return

        self._send_empty(404)

    def do_POST(self) -> None:
        self._consume_body()
        if self.path == "/api/v1/runtime:heartbeat":
            return self._send_empty(204)
        if not self._check_token():
            return self._send_json(401, {"error": "invalid_runtime_token"})

        self._send_json(404, {"error": "not_found"})

    def do_PUT(self) -> None:
        body_bytes = self._read_body()

        if not self._check_token():
            return self._send_json(401, {"error": "invalid_runtime_token"})

        if self.path == "/api/v1/runtime/tools":
            return self._handle_tools_put(body_bytes)

        self._send_json(404, {"error": "not_found"})

    def _handle_tools_put(self, body_bytes: bytes) -> None:
        """Negotiated tool-manifest PUT."""
        try:
            manifest = json.loads(body_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._send_json(400, {"error": "invalid_json"})

        schema = manifest.get("schema", "")
        tools: list[Any] = manifest.get("tools", [])

        if schema == "kei.tool-manifest/v3":
            if not self._accepts("application/vnd.kei.tool-manifest.v3+json"):
                return self._send_json(
                    400,
                    {
                        "error": "missing_protocol_proposal",
                        "message": (
                            "Accept header must include the v3 protocol media type: "
                            "application/vnd.kei.tool-manifest.v3+json"
                        ),
                    },
                )
            self._send_raw(
                200,
                json.dumps(
                    {
                        "added": len(tools),
                        "updated": 0,
                        "removed": 0,
                        "unchanged": 0,
                    }
                ).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Kei-Tool-Manifest-Schema": "kei.tool-manifest/v3",
                    "X-Kei-Supported-Tool-Manifest-Schemas": (
                        "kei.tool-manifest/v2, kei.tool-manifest/v3"
                    ),
                },
            )
            return

        if schema == "kei.tool-manifest/v2":
            if not self._accepts("application/vnd.kei.tool-manifest.v2+json"):
                return self._send_json(
                    400,
                    {
                        "error": "missing_protocol_proposal",
                        "message": (
                            "Accept header must include the v2 protocol media type: "
                            "application/vnd.kei.tool-manifest.v2+json"
                        ),
                    },
                )
            self._send_raw(
                200,
                json.dumps(
                    {
                        "added": len(tools),
                        "updated": 0,
                        "removed": 0,
                        "unchanged": 0,
                    }
                ).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Kei-Tool-Manifest-Schema": "kei.tool-manifest/v2",
                    "X-Kei-Supported-Tool-Manifest-Schemas": (
                        "kei.tool-manifest/v2, kei.tool-manifest/v3"
                    ),
                },
            )
            return

        self._send_json(
            400,
            {
                "error": "unsupported_schema",
                "supported_schemas": [
                    "kei.tool-manifest/v2",
                    "kei.tool-manifest/v3",
                ],
                "message": f"Unrecognized manifest schema: {schema!r}",
            },
        )

    def do_OPTIONS(self) -> None:
        """CORS/connectivity preflight."""
        self._send_empty(204)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class ReusableTCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    port = int(os.environ.get("FAKE_CONTROL_PLANE_PORT", "0"))
    server = ReusableTCPServer(("127.0.0.1", port), Handler)
    print(server.server_address[1], flush=True)  # test reads this port
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
