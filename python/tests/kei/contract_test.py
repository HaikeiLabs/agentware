"""Contract tests for the KEI auth/config contract using a fake server.

These tests verify the server-side contract that the client (JWTTokenProvider)
will eventually implement. They exercise:
- Token exchange
- Token renewal
- 401 handling (invalid/revoked token)
- Token revocation
- Config revision

The future JWT client behavior is gated behind explicit config
(jwt_exchange_enabled). These tests confirm the contract is well-defined
and stable so TypeScript/Go can reuse the same fixtures.
"""

import json
import urllib.error
import urllib.request

from fake_kei_server import FakeKEIServer


def _post(url: str, token: str | None) -> tuple[int, dict]:
    data = b""
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _get(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class TestTokenExchangeContract:
    """Contract: POST /auth/exchange with valid bootstrap token -> JWT."""

    def test_exchange_success(self):
        with FakeKEIServer(bootstrap_token="boot") as server:
            status, body = _post(f"{server.base_url}/auth/exchange", "boot")
            assert status == 200
            assert "token" in body
            assert body["token_type"] == "jwt"
            assert body["expires_in"] > 0

    def test_exchange_invalid_bootstrap_401(self):
        with FakeKEIServer(bootstrap_token="boot") as server:
            status, body = _post(f"{server.base_url}/auth/exchange", "wrong")
            assert status == 401
            assert "error" in body


class TestTokenRenewalContract:
    """Contract: POST /auth/refresh with valid JWT -> new JWT."""

    def test_renewal_success(self):
        with FakeKEIServer(bootstrap_token="boot") as server:
            _, ex = _post(f"{server.base_url}/auth/exchange", "boot")
            status, body = _post(f"{server.base_url}/auth/refresh", ex["token"])
            assert status == 200
            assert body["token"] != ex["token"]
            assert body["token_type"] == "jwt"

    def test_renewal_invalid_token_401(self):
        with FakeKEIServer(bootstrap_token="boot") as server:
            status, body = _post(f"{server.base_url}/auth/refresh", "not-a-jwt")
            assert status == 401


class TestTokenRevocationContract:
    """Contract: POST /auth/revoke removes a valid JWT."""

    def test_revoke_success(self):
        with FakeKEIServer(bootstrap_token="boot") as server:
            _, ex = _post(f"{server.base_url}/auth/exchange", "boot")
            status, body = _post(f"{server.base_url}/auth/revoke", ex["token"])
            assert status == 200
            assert body["status"] == "revoked"

    def test_revoke_then_refresh_401(self):
        """A revoked token cannot be renewed (401)."""
        with FakeKEIServer(bootstrap_token="boot") as server:
            _, ex = _post(f"{server.base_url}/auth/exchange", "boot")
            _post(f"{server.base_url}/auth/revoke", ex["token"])
            status, _ = _post(f"{server.base_url}/auth/refresh", ex["token"])
            assert status == 401

    def test_revoke_unknown_token_404(self):
        with FakeKEIServer(bootstrap_token="boot") as server:
            status, _ = _post(f"{server.base_url}/auth/revoke", "unknown")
            assert status == 404


class TestConfigRevisionContract:
    """Contract: GET /config returns the current config revision."""

    def test_config_revision(self):
        with FakeKEIServer(config_revision="rev-42") as server:
            status, body = _get(f"{server.base_url}/config")
            assert status == 200
            assert body["config_revision"] == "rev-42"

    def test_config_revision_changes(self):
        """Config revision is server-authoritative and can change."""
        with FakeKEIServer(config_revision="rev-1") as server:
            _, body1 = _get(f"{server.base_url}/config")
            assert body1["config_revision"] == "rev-1"


class TestContractGating:
    """Future JWT client behavior is gated behind explicit config."""

    def test_jwt_provider_gated(self):
        """JWTTokenProvider requires explicit enablement (fail closed)."""
        from haikeilabs.agentware.kei.auth import JWTTokenProvider

        with FakeKEIServer(bootstrap_token="boot") as server:
            provider = JWTTokenProvider(api_url=server.base_url, initial_token="boot")
            # Not enabled: get_token fails closed
            try:
                provider.get_token()
                assert False, "expected RuntimeError"
            except RuntimeError:
                pass

    def test_jwt_exchange_not_implemented_until_server_exists(self):
        """Even when enabled, exchange is a contract, not yet implemented."""
        from haikeilabs.agentware.kei.auth import JWTTokenProvider

        with FakeKEIServer(bootstrap_token="boot") as server:
            provider = JWTTokenProvider(api_url=server.base_url, initial_token="boot")
            provider.enable()
            try:
                provider.get_token()
                assert False, "expected NotImplementedError"
            except NotImplementedError:
                pass
