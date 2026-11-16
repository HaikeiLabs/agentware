"""Snowflake key-pair JWT for the SQL API, minted on the harness side.

The Kei runtime releases the Snowflake service user's private key (a PEM,
delivered as the released credential's access token) for one call. The
harness turns it into a short-lived JWT with :func:`snowflake_jwt` and calls
Snowflake itself. This module makes no network calls, never logs, and never
puts key material or the token in an exception; parser errors are not chained.

Token shape (Snowflake key-pair authentication)::

    header  {"alg":"RS256","typ":"JWT"}
    iss     <ACCOUNT>.<USER>.SHA256:<base64 SHA-256 of the public key DER>
    sub     <ACCOUNT>.<USER>
    iat     now, in Unix seconds
    exp     iat + ttl, capped at one hour

Send it as ``Authorization: Bearer <token>``; the optional
``X-Snowflake-Authorization-Token-Type: KEYPAIR_JWT`` header names the type.

Needs the ``cryptography`` package: ``pip install "haikeilabs-agentware[snowflake]"``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

__all__ = [
    "MAX_TTL",
    "MIN_RSA_BITS",
    "EncryptedPrivateKeyError",
    "InvalidAccountIdentifierError",
    "InvalidKeyError",
    "InvalidTTLError",
    "InvalidUserError",
    "SnowflakeJWTError",
    "normalize_account_identifier",
    "public_key_fingerprint",
    "snowflake_jwt",
]

MAX_TTL = timedelta(hours=1)
"""The longest lifetime Snowflake honours for a key-pair JWT; longer is capped."""

MIN_RSA_BITS = 2048
"""The smallest RSA modulus accepted."""

_HEADER = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
_ACCOUNT_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


class SnowflakeJWTError(ValueError):
    """Base class. Messages are fixed and never carry key material."""


class InvalidAccountIdentifierError(SnowflakeJWTError):
    """The account identifier is empty, a host name, or has invalid characters."""

    def __init__(self) -> None:
        super().__init__("snowflake: invalid account identifier")


class InvalidUserError(SnowflakeJWTError):
    """The user is empty or contains control characters."""

    def __init__(self) -> None:
        super().__init__("snowflake: invalid user")


class InvalidTTLError(SnowflakeJWTError):
    """The requested lifetime is zero or negative."""

    def __init__(self) -> None:
        super().__init__("snowflake: ttl must be positive")


class InvalidKeyError(SnowflakeJWTError):
    """Not an unencrypted RSA private key of at least MIN_RSA_BITS bits."""

    def __init__(self) -> None:
        super().__init__("snowflake: invalid RSA key")


class EncryptedPrivateKeyError(SnowflakeJWTError):
    """The PEM is passphrase-encrypted; this helper takes no passphrase."""

    def __init__(self) -> None:
        super().__init__(
            "snowflake: private key is encrypted; passphrase-protected keys are not supported"
        )


def normalize_account_identifier(value: str) -> str:
    """Return the account identifier in the form the JWT claims need.

    Trims and upper-cases the input. An account locator with region and cloud
    suffixes (``xy12345.us-east-1.aws``) is cut at the first ``.``, leaving the
    locator. An identifier containing ``.global`` has every ``.`` replaced by
    ``-``. It is never cut at ``-``, so the organization form is kept whole:
    pass it as ``ORG-ACCOUNT``, not ``ORG.ACCOUNT`` (the dotted form would be
    read as locator.region). A host name (``...snowflakecomputing.com``) is
    refused.
    """
    s = value.strip().upper()
    if "SNOWFLAKECOMPUTING" in s:
        raise InvalidAccountIdentifierError()
    if ".GLOBAL" in s:
        s = s.replace(".", "-")
    else:
        s = s.split(".", 1)[0]
    if not s or not set(s) <= _ACCOUNT_CHARS:
        raise InvalidAccountIdentifierError()
    return s


def _normalize_user(user: str) -> str:
    s = user.strip().upper()
    if not s or any(not ch.isprintable() for ch in s):
        raise InvalidUserError()
    return s


def public_key_fingerprint(public_key: RSAPublicKey) -> str:
    """Return ``SHA256:`` plus the base64 SHA-256 of the key's DER SubjectPublicKeyInfo.

    Equals Snowflake's ``RSA_PUBLIC_KEY_FP`` and
    ``openssl rsa -pubout -outform DER | openssl dgst -sha256 -binary | base64``.
    """
    from cryptography.hazmat.primitives import serialization

    der = public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()


def _load_private_key(private_key_pem: bytes | str) -> RSAPrivateKey:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    data = private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem
    if b"-----BEGIN ENCRYPTED PRIVATE KEY-----" in data or b"Proc-Type: 4,ENCRYPTED" in data:
        raise EncryptedPrivateKeyError()
    if (
        b"-----BEGIN PRIVATE KEY-----" not in data
        and b"-----BEGIN RSA PRIVATE KEY-----" not in data
    ):
        raise InvalidKeyError()
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except TypeError:
        raise EncryptedPrivateKeyError() from None
    except Exception:  # any parser failure is an invalid key; never chain it
        raise InvalidKeyError() from None
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < MIN_RSA_BITS:
        raise InvalidKeyError()
    return key


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def snowflake_jwt(
    account_identifier: str,
    user: str,
    private_key_pem: bytes | str,
    now: datetime,
    ttl: timedelta,
) -> str:
    """Mint a Snowflake key-pair JWT for ``user`` in ``account_identifier``.

    ``private_key_pem`` is an unencrypted PKCS#8 or PKCS#1 RSA key. ``now``
    must be timezone-aware; ``iat`` is ``now`` truncated to whole seconds and
    ``exp`` is ``iat + ttl``, capped at :data:`MAX_TTL`. A ttl of zero or less
    is refused. See :func:`normalize_account_identifier` for the account
    rules; the user is trimmed and upper-cased.

    The returned token is a credential: never log it.
    """
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            'snowflake_jwt needs the cryptography package: pip install "haikeilabs-agentware[snowflake]"'
        ) from None

    account = normalize_account_identifier(account_identifier)
    subject_user = _normalize_user(user)
    if now.tzinfo is None or now.utcoffset() is None:
        raise SnowflakeJWTError("snowflake: now must be timezone-aware")
    if ttl <= timedelta(0):
        raise InvalidTTLError()
    ttl = min(ttl, MAX_TTL)
    key = _load_private_key(private_key_pem)

    iat = math.floor(now.timestamp())
    subject = f"{account}.{subject_user}"
    claims = {
        "iss": f"{subject}.{public_key_fingerprint(key.public_key())}",
        "sub": subject,
        "iat": iat,
        "exp": iat + int(ttl.total_seconds()),
    }
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{_HEADER}.{payload}"
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"
