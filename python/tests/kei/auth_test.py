"""Tests for KEI auth module."""

import os
from datetime import datetime, timedelta

import httpx
import pytest

from pedro_agentware.kei.auth import (
    BOOTSTRAP_TOKEN_ENV,
    AuthProvider,
    EnvSecretProvider,
    JWTTokenProvider,
    OpaqueTokenProvider,
    TokenInfo,
    TokenType,
    get_auth_provider,
)


class FakeSecretProvider:
    """In-memory secret provider for tests."""

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets

    def get_secret(self, name: str) -> str | None:
        return self._secrets.get(name)


class TestOpaqueTokenProvider:
    """Tests for OpaqueTokenProvider."""

    def test_token_from_env(self):
        """Bootstrap token loaded from KEI_RUNTIME_TOKEN env."""
        os.environ[BOOTSTRAP_TOKEN_ENV] = "env-token"
        try:
            provider = OpaqueTokenProvider()
            info = provider.get_token()
            assert info.token == "env-token"
            assert provider.get_token_type() == TokenType.OPAQUE
        finally:
            del os.environ[BOOTSTRAP_TOKEN_ENV]

    def test_token_from_secret_provider(self):
        """Bootstrap token loaded from injected secret provider."""
        os.environ.pop(BOOTSTRAP_TOKEN_ENV, None)
        provider = OpaqueTokenProvider(
            secret_provider=FakeSecretProvider({BOOTSTRAP_TOKEN_ENV: "vault-token"})
        )
        assert provider.get_token().token == "vault-token"

    def test_explicit_token_takes_precedence(self):
        """Explicit token wins over env and provider."""
        os.environ[BOOTSTRAP_TOKEN_ENV] = "env-token"
        try:
            provider = OpaqueTokenProvider(
                token="explicit-token",
                secret_provider=FakeSecretProvider({BOOTSTRAP_TOKEN_ENV: "vault-token"}),
            )
            assert provider.get_token().token == "explicit-token"
        finally:
            del os.environ[BOOTSTRAP_TOKEN_ENV]

    def test_missing_token_fails_closed(self):
        """No token available: fail closed with a clear error."""
        os.environ.pop(BOOTSTRAP_TOKEN_ENV, None)
        with pytest.raises(ValueError, match="bootstrap token"):
            OpaqueTokenProvider()

    def test_invalidate_fails_closed(self):
        """Opaque token invalidation cannot renew: raises."""
        provider = OpaqueTokenProvider(token="opaque")
        with pytest.raises(httpx.HTTPStatusError):
            provider.invalidate()

    def test_token_not_in_repr(self):
        """Token value never appears in repr (no accidental logging)."""
        provider = OpaqueTokenProvider(token="super-secret")
        assert "super-secret" not in repr(provider.get_token())


class TestJWTTokenProvider:
    """Tests for JWTTokenProvider (future contract, gated)."""

    def test_gated_until_explicitly_enabled(self):
        """get_token before enable() fails closed."""
        provider = JWTTokenProvider(api_url="https://kei.example.com/api/v1", initial_token="boot")
        with pytest.raises(RuntimeError, match="not enabled"):
            provider.get_token()

    def test_enable_requires_bootstrap_token(self):
        """Cannot enable without a bootstrap secret."""
        os.environ.pop(BOOTSTRAP_TOKEN_ENV, None)
        with pytest.raises(ValueError, match="bootstrap token"):
            JWTTokenProvider(api_url="https://kei.example.com/api/v1")

    def test_exchange_contract_not_implemented(self):
        """Exchange is a defined contract, not yet implemented: raises."""
        provider = JWTTokenProvider(api_url="https://kei.example.com/api/v1", initial_token="boot")
        provider.enable()
        with pytest.raises(NotImplementedError, match="not claimed to exist"):
            provider.get_token()

    def test_renewal_contract_not_implemented(self):
        """Renewal is a defined contract, not yet implemented: raises."""
        provider = JWTTokenProvider(api_url="https://kei.example.com/api/v1", initial_token="boot")
        provider.enable()
        provider._token_info = TokenInfo(
            token="jwt",
            token_type=TokenType.JWT,
            expires_at=datetime.now() + timedelta(seconds=10),
        )
        with pytest.raises(NotImplementedError, match="not claimed to exist"):
            provider.get_token()

    def test_invalidate_drops_cached_token(self):
        """Invalidate drops the cached token (re-exchange on next call)."""
        provider = JWTTokenProvider(api_url="https://kei.example.com/api/v1", initial_token="boot")
        provider.enable()
        provider._token_info = TokenInfo(token="jwt", token_type=TokenType.JWT)
        provider.invalidate()
        assert provider._token_info is None

    def test_invalidate_gated(self):
        """Invalidate also requires explicit enablement."""
        provider = JWTTokenProvider(api_url="https://kei.example.com/api/v1", initial_token="boot")
        with pytest.raises(RuntimeError, match="not enabled"):
            provider.invalidate()

    def test_token_type(self):
        """JWT provider reports JWT token type."""
        provider = JWTTokenProvider(api_url="https://kei.example.com/api/v1", initial_token="boot")
        assert provider.get_token_type() == TokenType.JWT


class TestAuthProviderFactory:
    """Tests for AuthProviderFactory and get_auth_provider."""

    def test_default_is_opaque(self):
        """Default (jwt_exchange_enabled=False) yields opaque provider."""
        provider = get_auth_provider("https://kei.example.com", token="opaque")
        assert isinstance(provider, OpaqueTokenProvider)
        assert provider.get_token_type() == TokenType.OPAQUE

    def test_jwt_gated_behind_explicit_config(self):
        """JWT provider only created when explicitly enabled."""
        provider = get_auth_provider(
            "https://kei.example.com", jwt_exchange_enabled=True, token="boot"
        )
        assert isinstance(provider, JWTTokenProvider)
        assert provider.get_token_type() == TokenType.JWT

    def test_provider_satisfies_protocol(self):
        """Both providers satisfy the AuthProvider protocol."""
        assert isinstance(OpaqueTokenProvider(token="x"), AuthProvider)
        assert isinstance(
            JWTTokenProvider(api_url="https://kei.example.com", initial_token="x"),
            AuthProvider,
        )

    def test_env_secret_provider_integration(self):
        """EnvSecretProvider feeds the bootstrap token."""
        os.environ[BOOTSTRAP_TOKEN_ENV] = "env-fed"
        try:
            provider = get_auth_provider(
                "https://kei.example.com", secret_provider=EnvSecretProvider()
            )
            assert provider.get_token().token == "env-fed"
        finally:
            del os.environ[BOOTSTRAP_TOKEN_ENV]
