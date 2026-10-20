"""Tests for fake-control-plane.py — a stdlib HTTP stub for kei-proxy CI."""

import http.client
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
STUB = HERE / "fake-control-plane.py"

# Dummy token accepted by the stub (any non-empty value).
DUMMY_TOKEN = "kh_live_test_dummy_token_value"

# ---------------------------------------------------------------------------
# Allowed fields from kei-connector-runtime policy-bundle-v2.schema.json
# (additionalProperties: false at every level).
# ---------------------------------------------------------------------------

# Top-level fields allowed in kei.policy-bundle/v2
ALLOWED_BUNDLE_FIELDS = {
    "schema",
    "bundle_id",
    "bundle_version",
    "policy_revision",
    "issuer",
    "audience",
    "issued_at",
    "not_after",
    "refresh",
    "subject_resolution",
    "policy_set",
    "agent_connector_bindings",
    "agent_model_profiles",
    "harnesses",
    "harness_match_semantics",
    "subject",
    "identities",
}

# Fields allowed inside policy_set (kei.policy-set/v2)
ALLOWED_POLICY_SET_FIELDS = {
    "schema",
    "org_id",
    "workspace_id",
    "match_semantics",
    "evaluation",
    "tools",
    "checks",
    "tool_policies",
    "harness_match_semantics",
    "harness_policies",
    "connectors",
}

# Fields allowed inside each tool_policy item (check schema)
ALLOWED_CHECK_FIELDS = {
    "policy_id",
    "name",
    "effect",
    "priority",
    "scope",
    "selector",
}

# Fields allowed inside selector
ALLOWED_SELECTOR_FIELDS = {
    "source_principal",
    "source",
    "connector_id",
    "tool",
    "capability",
    "resource_type",
    "resource_id",
    "resource_parent",
}

# Fields allowed inside scope
ALLOWED_SCOPE_FIELDS = {
    "org_id",
    "workspace_id",
    "agent_id",
}

# Fields allowed inside evaluation
ALLOWED_EVALUATION_FIELDS = {
    "precedence",
    "checks",
    "default",
}

# Fields allowed inside audience
ALLOWED_AUDIENCE_FIELDS = {
    "installation_id",
    "org_id",
    "workspace_id",
}

# Fields allowed inside refresh
ALLOWED_REFRESH_FIELDS = {
    "poll_interval_seconds",
    "stale_after_seconds",
}

# Fields allowed inside subject_resolution
ALLOWED_SUBJECT_RESOLUTION_FIELDS = {
    "mode",
    "max_cache_seconds",
}

# Fields allowed inside identities items
ALLOWED_IDENTITY_FIELDS = {
    "source",
    "external_id",
    "user_id",
    "status",
    "groups",
    "org_roles",
    "subject_token",
    "expire_time",
}

# Fields allowed inside subject
ALLOWED_SUBJECT_FIELDS = {
    "user_id",
    "email",
    "groups",
}

# Fields allowed inside agent_model_profiles items
ALLOWED_MODEL_PROFILE_FIELDS = {
    "agent_id",
    "profile_id",
    "status",
}


def _headers_with_token() -> dict[str, str]:
    return {"Authorization": f"Bearer {DUMMY_TOKEN}"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_whoami_returns_identity() -> None:
    """GET /api/v1/runtime/whoami returns 200 with a non-empty identity."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/v1/runtime/whoami", headers=_headers_with_token())
        resp = conn.getresponse()
        body = json.loads(resp.read())
        assert resp.status == 200, f"expected 200, got {resp.status}"
        assert body["id"], "id must be non-empty"
        assert body["org_id"], "org_id must be non-empty"
        assert body["platform"], "platform must be non-empty"
        assert isinstance(body.get("workspace_id"), str), (
            "workspace_id must be a string"
        )
    finally:
        _stop_stub(proc)


def test_whoami_rejects_missing_token() -> None:
    """GET /api/v1/runtime/whoami without token returns 401."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/v1/runtime/whoami")
        resp = conn.getresponse()
        body = json.loads(resp.read())
        assert resp.status == 401, f"expected 401, got {resp.status}"
        assert "error" in body
    finally:
        _stop_stub(proc)


def test_whoami_accepts_x_kei_token() -> None:
    """GET /api/v1/runtime/whoami accepts X-Kei-Token header."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET", "/api/v1/runtime/whoami", headers={"X-Kei-Token": DUMMY_TOKEN}
        )
        resp = conn.getresponse()
        assert resp.status == 200, f"expected 200, got {resp.status}"
    finally:
        _stop_stub(proc)


def test_policy_bundles_current_returns_bundle() -> None:
    """GET /api/v1/runtime/policy-bundles/current returns 200 + bundle."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers=_headers_with_token(),
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())
        assert resp.status == 200, f"expected 200, got {resp.status}"
        assert (
            resp.getheader("Content-Type")
            == "application/vnd.kei.policy-bundle.v2+json"
        )
        assert resp.getheader("ETag", "").startswith('"sha256:')
        assert int(resp.getheader("Kei-Policy-Bundle-Version", "0")) >= 1
        assert body["schema"] == "kei.policy-bundle/v2"
        assert body["bundle_version"] >= 1
    finally:
        _stop_stub(proc)


def test_heartbeat_returns_204() -> None:
    """POST /api/v1/runtime:heartbeat returns 204 No Content (no token needed)."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/v1/runtime:heartbeat")
        resp = conn.getresponse()
        resp.read()  # drain
        assert resp.status == 204, f"expected 204, got {resp.status}"
    finally:
        _stop_stub(proc)


def test_policy_bundle_etag_matches_body() -> None:
    """ETag header is sha256 of the raw body bytes."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers=_headers_with_token(),
        )
        resp = conn.getresponse()
        raw = resp.read()
        etag = resp.getheader("ETag", "")
        import hashlib

        expected = '"sha256:' + hashlib.sha256(raw).hexdigest() + '"'
        assert etag == expected, f"ETag {etag} != {expected}"
    finally:
        _stop_stub(proc)


def test_policy_bundle_not_modified() -> None:
    """Sending If-None-Match with the current ETag returns 304."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers=_headers_with_token(),
        )
        resp = conn.getresponse()
        resp.read()
        etag = resp.getheader("ETag")

        conn2 = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn2.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers={**_headers_with_token(), "If-None-Match": etag},
        )
        resp2 = conn2.getresponse()
        resp2.read()
        assert resp2.status == 304, f"expected 304, got {resp2.status}"
    finally:
        _stop_stub(proc)


def test_put_tools_v3_returns_counts() -> None:
    """PUT /api/v1/runtime/tools with v3 Accept+body returns 200 with counts and negotiation headers."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        tools = [{"name": "echo"}, {"name": "list_files"}]
        body = json.dumps({"schema": "kei.tool-manifest/v3", "tools": tools})
        conn.request(
            "PUT",
            "/api/v1/runtime/tools",
            body=body,
            headers={
                **_headers_with_token(),
                "Content-Type": "application/json",
                "Accept": "application/json, application/vnd.kei.tool-manifest.v3+json",
            },
        )
        resp = conn.getresponse()
        resp_body = json.loads(resp.read())

        assert resp.status == 200, f"expected 200, got {resp.status}"
        assert resp.getheader("Content-Type") == "application/json"
        assert resp.getheader("X-Kei-Tool-Manifest-Schema") == "kei.tool-manifest/v3"
        assert resp.getheader("X-Kei-Supported-Tool-Manifest-Schemas") is not None
        # The supported-schemas header must advertise both v2 and v3
        supported = resp.getheader("X-Kei-Supported-Tool-Manifest-Schemas")
        assert "kei.tool-manifest/v2" in supported
        assert "kei.tool-manifest/v3" in supported

        assert resp_body.get("added") == 2
        assert resp_body.get("updated") == 0
        assert resp_body.get("removed") == 0
        assert resp_body.get("unchanged") == 0
    finally:
        _stop_stub(proc)


def test_put_tools_v3_rejects_missing_accept() -> None:
    """PUT /api/v1/runtime/tools with v3 body but no v3 Accept returns 400."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"schema": "kei.tool-manifest/v3", "tools": []})
        conn.request(
            "PUT",
            "/api/v1/runtime/tools",
            body=body,
            headers={
                **_headers_with_token(),
                "Content-Type": "application/json",
                # Accept does NOT include the v3 media type
                "Accept": "application/json",
            },
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 400, f"expected 400, got {resp.status}"
    finally:
        _stop_stub(proc)


def test_put_tools_v3_rejects_missing_schema_field() -> None:
    """PUT /api/v1/runtime/tools with v3 Accept but missing schema field returns 400."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"tools": []})  # no "schema" key
        conn.request(
            "PUT",
            "/api/v1/runtime/tools",
            body=body,
            headers={
                **_headers_with_token(),
                "Content-Type": "application/json",
                "Accept": "application/json, application/vnd.kei.tool-manifest.v3+json",
            },
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 400, f"expected 400, got {resp.status}"
    finally:
        _stop_stub(proc)


def test_chunked_put_tools_returns_200() -> None:
    """PUT /api/v1/runtime/tools with Transfer-Encoding: chunked returns 200."""
    proc, port = _start_stub()
    try:
        raw = _build_chunked_put(
            "/api/v1/runtime/tools",
            {"schema": "kei.tool-manifest/v3", "tools": []},
            f"127.0.0.1:{port}",
        )
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("127.0.0.1", port))
        s.sendall(raw)
        time.sleep(0.3)

        resp = _recv_raw_response(s)
        s.close()

        assert resp.status == 200, (
            f"expected 200, got {resp.status}\nbody: {resp.body!r}"
        )
        data = json.loads(resp.body)
        assert data.get("added") == 0, f"unexpected body: {data}"
        assert data.get("updated") == 0
        assert resp.headers.get("X-Kei-Tool-Manifest-Schema") == "kei.tool-manifest/v3"
    finally:
        _stop_stub(proc)


def test_chunked_put_tools_rejects_missing_token() -> None:
    """Chunked PUT without token returns 401."""
    proc, port = _start_stub()
    try:
        request_line = b"PUT /api/v1/runtime/tools HTTP/1.1\r\n"
        headers = (
            f"Host: 127.0.0.1:{port}\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Content-Type: application/json\r\n"
            "Accept: application/json, application/vnd.kei.tool-manifest.v3+json\r\n"
            "\r\n"
        ).encode("ascii")
        chunked = _build_chunk_body(
            json.dumps({"schema": "kei.tool-manifest/v3", "tools": []})
        )

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("127.0.0.1", port))
        s.sendall(request_line + headers + chunked)
        time.sleep(0.3)

        resp = _recv_raw_response(s)
        s.close()

        assert resp.status == 401, (
            f"expected 401, got {resp.status}\nbody: {resp.body!r}"
        )
    finally:
        _stop_stub(proc)


def test_chunked_put_tools_on_keepalive_no_leak() -> None:
    """Chunked PUT must not leak body bytes into the next request on the same connection."""
    proc, port = _start_stub()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("127.0.0.1", port))

        # Send chunked PUT
        raw = _build_chunked_put(
            "/api/v1/runtime/tools",
            {"schema": "kei.tool-manifest/v3", "tools": []},
            f"127.0.0.1:{port}",
        )
        s.sendall(raw)
        time.sleep(0.3)
        resp1 = _recv_raw_response(s)
        assert resp1.status == 200, f"first request: expected 200, got {resp1.status}"

        # Send a second request on the same connection (Content-Length PUT)
        s.sendall(
            _build_put_request(
                "/api/v1/runtime/tools",
                json.dumps(
                    {"schema": "kei.tool-manifest/v3", "tools": [{"name": "extra"}]}
                ),
                f"127.0.0.1:{port}",
            )
        )
        time.sleep(0.3)
        resp2 = _recv_raw_response(s)
        s.close()

        assert resp2.status == 200, (
            f"second request: expected 200, got {resp2.status}\nbody: {resp2.body!r}"
        )
    finally:
        _stop_stub(proc)


def test_put_tools_rejects_missing_token() -> None:
    """PUT /api/v1/runtime/tools without token returns 401."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"schema": "kei.tool-manifest/v3", "tools": []})
        conn.request(
            "PUT",
            "/api/v1/runtime/tools",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, application/vnd.kei.tool-manifest.v3+json",
            },
        )
        resp = conn.getresponse()
        assert resp.status == 401, f"expected 401, got {resp.status}"
    finally:
        _stop_stub(proc)


def test_bundle_has_no_unknown_top_level_fields() -> None:
    """The stub's policy bundle must not contain fields outside the v2 schema."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers=_headers_with_token(),
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())

        extra = set(body.keys()) - ALLOWED_BUNDLE_FIELDS
        assert not extra, f"bundle has unknown top-level fields: {extra}"
    finally:
        _stop_stub(proc)


def test_bundle_policy_set_has_no_unknown_fields() -> None:
    """The stub's policy_set must not contain fields outside the v2 policy-set schema."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers=_headers_with_token(),
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())

        ps = body.get("policy_set", {})
        extra = set(ps.keys()) - ALLOWED_POLICY_SET_FIELDS
        assert not extra, f"policy_set has unknown fields: {extra}"
    finally:
        _stop_stub(proc)


def test_bundle_tool_policies_have_no_unknown_fields() -> None:
    """Each tool_policy item must only use check-schema fields."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers=_headers_with_token(),
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())

        for tpol in body.get("policy_set", {}).get("tool_policies", []):
            extra = set(tpol.keys()) - ALLOWED_CHECK_FIELDS
            assert not extra, f"tool_policy item has unknown fields: {extra}"
            # nested: selector
            sel = tpol.get("selector", {})
            extra_sel = set(sel.keys()) - ALLOWED_SELECTOR_FIELDS
            assert not extra_sel, f"selector has unknown fields: {extra_sel}"
            # nested: scope
            scp = tpol.get("scope", {})
            extra_scp = set(scp.keys()) - ALLOWED_SCOPE_FIELDS
            assert not extra_scp, f"scope has unknown fields: {extra_scp}"
    finally:
        _stop_stub(proc)


def test_bundle_evaluation_has_no_unknown_fields() -> None:
    """The evaluation block must only use schema fields."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers=_headers_with_token(),
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())

        ev = body.get("policy_set", {}).get("evaluation", {})
        extra = set(ev.keys()) - ALLOWED_EVALUATION_FIELDS
        assert not extra, f"evaluation has unknown fields: {extra}"
    finally:
        _stop_stub(proc)


def test_bundle_audience_has_no_unknown_fields() -> None:
    """The audience block must only use schema fields."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers=_headers_with_token(),
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())

        aud = body.get("audience", {})
        extra = set(aud.keys()) - ALLOWED_AUDIENCE_FIELDS
        assert not extra, f"audience has unknown fields: {extra}"
    finally:
        _stop_stub(proc)


def test_bundle_refresh_has_no_unknown_fields() -> None:
    """The refresh block must only use schema fields."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers=_headers_with_token(),
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())

        ref = body.get("refresh", {})
        extra = set(ref.keys()) - ALLOWED_REFRESH_FIELDS
        assert not extra, f"refresh has unknown fields: {extra}"
    finally:
        _stop_stub(proc)


def test_bundle_subject_resolution_has_no_unknown_fields() -> None:
    """The subject_resolution block must only use schema fields."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers=_headers_with_token(),
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())

        sr = body.get("subject_resolution", {})
        extra = set(sr.keys()) - ALLOWED_SUBJECT_RESOLUTION_FIELDS
        assert not extra, f"subject_resolution has unknown fields: {extra}"
    finally:
        _stop_stub(proc)


def test_bundle_identity_has_no_unknown_fields() -> None:
    """Each identity item must only use schema fields."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers=_headers_with_token(),
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())

        for ident in body.get("identities", []):
            extra = set(ident.keys()) - ALLOWED_IDENTITY_FIELDS
            assert not extra, f"identity item has unknown fields: {extra}"
    finally:
        _stop_stub(proc)


def test_bundle_subject_block_has_no_unknown_fields() -> None:
    """The subject block must only use schema fields."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers=_headers_with_token(),
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())

        subj = body.get("subject", {})
        extra = set(subj.keys()) - ALLOWED_SUBJECT_FIELDS
        assert not extra, f"subject has unknown fields: {extra}"
    finally:
        _stop_stub(proc)


def test_bundle_tools_must_contain_echo_and_delete_everything() -> None:
    """The served bundle's policy_set.tools must include echo and delete_everything with harness_executor."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers=_headers_with_token(),
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())

        tools = body.get("policy_set", {}).get("tools", [])
        tool_names = [t["name"] for t in tools]
        assert tool_names == ["echo", "delete_everything"], (
            f"expected tools ['echo', 'delete_everything'], got {tool_names}"
        )
        for t in tools:
            route = t.get("route", {})
            assert "harness_executor" in route, (
                f"tool {t['name']} is missing route.harness_executor"
            )
    finally:
        _stop_stub(proc)


def test_bundle_identities_include_all_example_pairs() -> None:
    """The served bundle's identities must include all three example pairs plus the ci-user."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers=_headers_with_token(),
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())

        identities = body.get("identities", [])
        pairs = [(i["source"], i["external_id"]) for i in identities]

        # The existing ci-user must be present
        assert ("ci", "ci-user") in pairs, "missing identity (ci, ci-user)"
        # Go example: source=minimal-harness, external_id=user-123
        assert ("minimal-harness", "user-123") in pairs, (
            f"missing identity (minimal-harness, user-123) in {pairs}"
        )
        # Python example: source=my-harness, external_id=local
        assert ("my-harness", "local") in pairs, (
            f"missing identity (my-harness, local) in {pairs}"
        )
        # TypeScript example: source=example, external_id=local-user
        assert ("example", "local-user") in pairs, (
            f"missing identity (example, local-user) in {pairs}"
        )
    finally:
        _stop_stub(proc)


def test_bundle_model_profiles_have_no_unknown_fields() -> None:
    """Each agent_model_profiles item must only use schema fields."""
    proc, port = _start_stub()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/v1/runtime/policy-bundles/current",
            headers=_headers_with_token(),
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())

        for profile in body.get("agent_model_profiles", []):
            extra = set(profile.keys()) - ALLOWED_MODEL_PROFILE_FIELDS
            assert not extra, f"agent_model_profiles item has unknown fields: {extra}"
    finally:
        _stop_stub(proc)


# ---------------------------------------------------------------------------
# helpers — raw socket request builders and response parser
# ---------------------------------------------------------------------------


def _build_chunk_body(payload: str) -> bytes:
    """Build the chunked-encoding body section for a JSON payload.

    Returns the chunk-size line in hex, the data, and the terminating chunk.
    Example for payload '{"tools":[]}'::

        b"10\\r\\n{\\"tools\\":[]}\\r\\n0\\r\\n\\r\\n"
    """
    data = payload.encode("utf-8")
    chunk_size = hex(len(data))[2:].encode("ascii")
    return chunk_size + b"\r\n" + data + b"\r\n0\r\n\r\n"


def _build_chunked_put(path: str, body: object, host: str) -> bytes:
    """Build a raw HTTP/1.1 PUT request with Transfer-Encoding: chunked."""
    request_line = f"PUT {path} HTTP/1.1\r\n".encode("ascii")
    headers = (
        f"Host: {host}\r\n"
        f"Authorization: Bearer {DUMMY_TOKEN}\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Content-Type: application/json\r\n"
        "Accept: application/json, application/vnd.kei.tool-manifest.v3+json\r\n"
        "\r\n"
    ).encode("ascii")
    return request_line + headers + _build_chunk_body(json.dumps(body))


def _build_put_request(path: str, payload: str, host: str) -> bytes:
    """Build a raw HTTP/1.1 PUT with Content-Length (no chunking)."""
    data = payload.encode("utf-8")
    request_line = f"PUT {path} HTTP/1.1\r\n".encode("ascii")
    headers = (
        f"Host: {host}\r\n"
        f"Authorization: Bearer {DUMMY_TOKEN}\r\n"
        f"Content-Length: {len(data)}\r\n"
        "Content-Type: application/json\r\n"
        "Accept: application/json, application/vnd.kei.tool-manifest.v3+json\r\n"
        "\r\n"
    ).encode("ascii")
    return request_line + headers + data


class _RawResponse:
    """Minimal HTTP/1.1 response parsed from a raw socket."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body


def _recv_raw_response(s: socket.socket) -> _RawResponse:
    """Read one HTTP/1.1 response from a raw socket.

    Handles Content-Length for body delimiting.  If the socket contains
    additional data beyond Content-Length (e.g. a second response), that
    data is left on the socket.
    """
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk

    header_part, _, rest = buf.partition(b"\r\n\r\n")
    status_line = header_part.split(b"\r\n")[0].decode("utf-8", errors="replace")
    parts = status_line.split(" ", 2)
    status = int(parts[1]) if len(parts) >= 2 else 0

    headers: dict[str, str] = {}
    for line in header_part.split(b"\r\n")[1:]:  # skip status line
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.decode("utf-8", errors="replace").strip()] = v.decode(
                "utf-8", errors="replace"
            ).strip()

    content_length = headers.get("Content-Length")
    if content_length is not None:
        cl = int(content_length)
        body = rest[:cl]
        remaining = cl - len(body)
        while remaining > 0:
            chunk = s.recv(4096)
            if not chunk:
                break
            body += chunk
            remaining -= len(chunk)
        return _RawResponse(status, headers, body)

    # No Content-Length — read until connection closes
    body = rest
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            body += chunk
    except socket.timeout:
        pass

    return _RawResponse(status, headers, body)


# ---------------------------------------------------------------------------
# helpers — stub lifecycle
# ---------------------------------------------------------------------------


def _start_stub() -> tuple[subprocess.Popen, int]:
    """Start fake-control-plane.py on an ephemeral port, return (proc, port)."""
    proc = subprocess.Popen(
        [sys.executable, str(STUB)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        out, err = proc.communicate(timeout=2)
        raise RuntimeError(f"stub exited early\nstdout:{out}\nstderr:{err}")
    port = int(line.strip())
    return proc, port


def _stop_stub(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
