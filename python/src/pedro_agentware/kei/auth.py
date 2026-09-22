"""KEI authentication provider interface and implementations.

This module provides a pluggable AuthProvider interface supporting:
1. Current opaque token calls
2. Future short-lived JWT exchange (gated behind explicit config)

Security contract:
- The bootstrap secret is loaded ONLY from the KEI_RUNTIME_TOKEN
  environment variable or an injected secret provider. It is never read
  from the manifest, never logged, and never written to disk.
- The JWT exchange contract is defined here but its behavior is gated
  behind explicit config (jwt_exchange_enabled). The server endpoint is
  NOT claimed to exist yet; enabling JWT exchange before the server
  supports it fails closed.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol, runtime_checkable

import httpx


class TokenType(str, Enum):
    """Supported token types."""

    OPAQUE = "opaque"
    JWT = "jwt"


# Canonical name of the bootstrap secret environment variable.
BOOTSTRAP_TOKEN_ENV = "KEI_RUNTIME_TOKEN"
_OLD_BOOTSTRAP_TOKEN_ENV = "KEI_HARNESS_TOKEN"


def _deprecated_env_check() -> None:
    """Reject the old env var name with a clear error (ADR-020)."""
    if os.environ.get(_OLD_BOOTSTRAP_TOKEN_ENV) and not os.environ.get(BOOTSTRAP_TOKEN_ENV):
        raise ValueError(
            f"{_OLD_BOOTSTRAP_TOKEN_ENV} is deprecated and no longer accepted; "
            f"set {BOOTSTRAP_TOKEN_ENV} instead"
        )


@dataclass
class TokenInfo:
    """Information about an authentication token.

    The token value is never included in __repr__ or str to avoid
    accidental logging.
    """

    token: str = field(repr=False)
    token_type: TokenType
    expires_at: datetime | None = None
    refresh_token: str | None = field(default=None, repr=False)
    issued_at: datetime | None = None

    def is_expired(self) -> bool:
        """Check if token is expired."""
        if self.expires_at is None:
            return False
        return datetime.now() >= self.expires_at

    def is_expiring_soon(self, buffer: timedelta = timedelta(minutes=5)) -> bool:
        """Check if token is expiring soon."""
        if self.expires_at is None:
            return False
        return datetime.now() >= (self.expires_at - buffer)


class SecretProvider(Protocol):
    """Protocol for secret sources (environment, vault, etc.)."""

    def get_secret(self, name: str) -> str | None:
        """Return the secret value for name, or None if absent."""
        ...


class EnvSecretProvider:
    """Secret provider backed by environment variables."""

    def get_secret(self, name: str) -> str | None:
        """Return the environment variable value, or None if absent."""
        return os.environ.get(name)


@runtime_checkable
class AuthProvider(Protocol):
    """Protocol for authentication providers.

    Implementations must provide:
    - get_token(): Get current valid token, renewing if necessary
    - invalidate(): Invalidate current token (e.g., on 401)
    - get_token_type(): Return the token type being used
    """

    def get_token(self) -> TokenInfo:
        """Get current valid token, renewing if necessary."""
        ...

    def invalidate(self) -> None:
        """Invalidate current token (e.g., on 401 response)."""
        ...

    def get_token_type(self) -> TokenType:
        """Return the type of token this provider issues."""
        ...


class OpaqueTokenProvider:
    """Auth provider for opaque tokens (current implementation).

    Loads the bootstrap secret ONLY from the KEI_RUNTIME_TOKEN environment
    variable or an injected SecretProvider. Opaque tokens do not support
    automatic renewal; invalidation fails closed.
    """

    def __init__(
        self,
        token: str | None = None,
        secret_provider: SecretProvider | None = None,
    ) -> None:
        """Initialize provider.

        Args:
            token: Optional explicit token value (e.g., injected by a
                   harness runtime). Takes precedence.
            secret_provider: Optional secret provider consulted for
                   KEI_RUNTIME_TOKEN when token is not given.

        Raises:
            ValueError: If no token is available from any source.
        """
        resolved = token
        if resolved is None and secret_provider is not None:
            resolved = secret_provider.get_secret(BOOTSTRAP_TOKEN_ENV)
        if resolved is None:
            _deprecated_env_check()
            resolved = os.environ.get(BOOTSTRAP_TOKEN_ENV)
        if not resolved:
            raise ValueError(
                f"No bootstrap token available: set {BOOTSTRAP_TOKEN_ENV} "
                f"or provide a SecretProvider"
            )
        self._token_info = TokenInfo(
            token=resolved,
            token_type=TokenType.OPAQUE,
            issued_at=datetime.now(),
        )

    def get_token(self) -> TokenInfo:
        """Get current token (opaque tokens do not auto-renew)."""
        return self._token_info

    def invalidate(self) -> None:
        """Invalidate token. Opaque tokens cannot be renewed: fail closed."""
        raise httpx.HTTPStatusError(
            "Opaque token invalidated; renewal requires a new bootstrap "
            f"secret via {BOOTSTRAP_TOKEN_ENV}",
            request=httpx.Request("GET", "internal://invalidate"),
            response=httpx.Response(401),
        )

    def get_token_type(self) -> TokenType:
        """Return opaque token type."""
        return TokenType.OPAQUE


class JWTTokenProvider:
    """Auth provider for short-lived JWT tokens (future implementation).

    Defines the contract for future JWT exchange support: token exchange,
    renewal, 401 handling, and revocation. The behavior is gated behind
    explicit config and fails closed until the server supports it.

    NOTE: The KEI server endpoints for exchange/refresh/revocation are NOT
    claimed to exist yet. This provider defines the client-side contract
    only.
    """

    def __init__(
        self,
        api_url: str,
        initial_token: str | None = None,
        secret_provider: SecretProvider | None = None,
        exchange_endpoint: str = "/auth/exchange",
        refresh_endpoint: str = "/auth/refresh",
        revoke_endpoint: str = "/auth/revoke",
    ) -> None:
        """Initialize JWT provider.

        Args:
            api_url: Base URL for KEI API
            initial_token: Optional bootstrap token for exchange
            secret_provider: Optional secret provider for bootstrap token
            exchange_endpoint: Contract path for JWT exchange (future)
            refresh_endpoint: Contract path for token renewal (future)
            revoke_endpoint: Contract path for token revocation (future)

        Raises:
            ValueError: If no bootstrap token is available.
        """
        self._api_url = api_url.rstrip("/")
        self._exchange_endpoint = exchange_endpoint
        self._refresh_endpoint = refresh_endpoint
        self._revoke_endpoint = revoke_endpoint
        self._token_info: TokenInfo | None = None
        self._enabled = False

        resolved = initial_token
        if resolved is None and secret_provider is not None:
            resolved = secret_provider.get_secret(BOOTSTRAP_TOKEN_ENV)
        if resolved is None:
            _deprecated_env_check()
            resolved = os.environ.get(BOOTSTRAP_TOKEN_ENV)
        if not resolved:
            raise ValueError(
                f"Cannot initialize JWT provider without bootstrap token: "
                f"set {BOOTSTRAP_TOKEN_ENV} or provide a SecretProvider"
            )
        self._bootstrap_token = resolved

    def enable(self) -> None:
        """Explicitly enable JWT exchange (required before use)."""
        self._enabled = True

    def _ensure_enabled(self) -> None:
        """Ensure JWT exchange is explicitly enabled (fail closed)."""
        if not self._enabled:
            raise RuntimeError(
                "JWT exchange is not enabled; enable it explicitly via config before use"
            )

    def get_token(self) -> TokenInfo:
        """Get current valid token, exchanging/renewing if needed.

        Raises:
            RuntimeError: If JWT exchange is not explicitly enabled.
            NotImplementedError: If the server contract is not yet
                implemented (current state).
        """
        self._ensure_enabled()

        if self._token_info is None:
            return self._exchange()
        if self._token_info.is_expiring_soon():
            return self._renew()
        return self._token_info

    def _exchange(self) -> TokenInfo:
        """Exchange bootstrap token for a short-lived JWT.

        Contract (server not yet claimed to exist):
            POST {api_url}{exchange_endpoint}
            Authorization: Bearer <bootstrap>
            -> {"token": "<jwt>", "expires_at": <iso8601>}
        """
        raise NotImplementedError(
            "JWT exchange contract not implemented; server endpoint "
            f"{self._api_url}{self._exchange_endpoint} not claimed to exist"
        )

    def _renew(self) -> TokenInfo:
        """Renew an expiring JWT.

        Contract (server not yet claimed to exist):
            POST {api_url}{refresh_endpoint}
            Authorization: Bearer <current jwt>
            -> {"token": "<jwt>", "expires_at": <iso8601>}
        """
        raise NotImplementedError(
            "JWT renewal contract not implemented; server endpoint "
            f"{self._api_url}{self._refresh_endpoint} not claimed to exist"
        )

    def invalidate(self) -> None:
        """Invalidate current token (e.g., after a 401 response).

        Drops the cached token so the next get_token() re-exchanges.
        Revocation of the server-side token is a separate contract:
            POST {api_url}{revoke_endpoint}
        """
        self._ensure_enabled()
        self._token_info = None

    def get_token_type(self) -> TokenType:
        """Return JWT token type."""
        return TokenType.JWT


class AuthProviderFactory:
    """Factory for creating AuthProvider instances."""

    @staticmethod
    def create(
        api_url: str,
        jwt_exchange_enabled: bool = False,
        secret_provider: SecretProvider | None = None,
        token: str | None = None,
    ) -> AuthProvider:
        """Create an AuthProvider based on explicit configuration.

        The bootstrap secret is read only from the provided token, the
        secret provider, or the KEI_RUNTIME_TOKEN environment variable —
        never from the manifest.

        Args:
            api_url: KEI API base URL
            jwt_exchange_enabled: Explicit gate for JWT exchange (default
                false; future behavior stays off unless enabled)
            secret_provider: Optional secret source
            token: Optional explicit bootstrap token

        Returns:
            AuthProvider instance (OpaqueTokenProvider or JWTTokenProvider)
        """
        if jwt_exchange_enabled:
            provider = JWTTokenProvider(
                api_url=api_url,
                initial_token=token,
                secret_provider=secret_provider,
            )
            provider.enable()
            return provider

        return OpaqueTokenProvider(
            token=token,
            secret_provider=secret_provider,
        )


def get_auth_provider(
    api_url: str,
    jwt_exchange_enabled: bool = False,
    secret_provider: SecretProvider | None = None,
    token: str | None = None,
) -> AuthProvider:
    """Convenience function to get an AuthProvider.

    Args:
        api_url: KEI API base URL
        jwt_exchange_enabled: Explicit gate for JWT exchange (default false)
        secret_provider: Optional secret source
        token: Optional explicit bootstrap token

    Returns:
        AuthProvider instance
    """
    return AuthProviderFactory.create(
        api_url=api_url,
        jwt_exchange_enabled=jwt_exchange_enabled,
        secret_provider=secret_provider,
        token=token,
    )
