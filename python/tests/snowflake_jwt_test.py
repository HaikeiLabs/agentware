"""Snowflake key-pair JWT helper (fixtures/snowflake/cases.v1.json).

The same cases run in the Go and TypeScript ports.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("cryptography")

from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa  # noqa: E402

from haikeilabs.agentware.connectors.snowflake import (  # noqa: E402
    MAX_TTL,
    EncryptedPrivateKeyError,
    InvalidAccountIdentifierError,
    InvalidKeyError,
    InvalidTTLError,
    InvalidUserError,
    SnowflakeJWTError,
    normalize_account_identifier,
    public_key_fingerprint,
    snowflake_jwt,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "snowflake"
CASES: dict[str, Any] = json.loads((FIXTURES / "cases.v1.json").read_text())
KEYS: dict[str, str] = CASES["keys"]


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _public_key() -> rsa.RSAPublicKey:
    key = serialization.load_pem_public_key(_fixture(KEYS["public"]))
    assert isinstance(key, rsa.RSAPublicKey)
    return key


def _b64url(segment: str) -> bytes:
    assert "=" not in segment
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _split(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    parts = token.split(".")
    assert len(parts) == 3
    header = json.loads(_b64url(parts[0]))
    claims = json.loads(_b64url(parts[1]))
    return header, claims, f"{parts[0]}.{parts[1]}".encode(), _b64url(parts[2])


def _verify(pub: rsa.RSAPublicKey, signed: bytes, sig: bytes) -> None:
    pub.verify(sig, signed, padding.PKCS1v15(), hashes.SHA256())


def _pkcs8(key: Any) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


NOW = datetime.fromtimestamp(1700000000, tz=timezone.utc)


def test_fingerprint_matches_openssl() -> None:
    assert public_key_fingerprint(_public_key()) == CASES["expected_fingerprint"]


@pytest.mark.parametrize("key_file", [KEYS["pkcs8"], KEYS["pkcs1"]])
@pytest.mark.parametrize("case", CASES["claims"], ids=lambda c: c["name"])
def test_claims_exact(key_file: str, case: dict[str, Any]) -> None:
    token = snowflake_jwt(
        case["account"],
        case["user"],
        _fixture(key_file),
        datetime.fromtimestamp(case["now_unix"], tz=timezone.utc),
        timedelta(seconds=case["ttl_seconds"]),
    )
    header, claims, signed, sig = _split(token)
    assert header == {"alg": "RS256", "typ": "JWT"}
    assert claims == {
        "iss": case["iss"],
        "sub": case["sub"],
        "iat": case["iat"],
        "exp": case["exp"],
    }
    _verify(_public_key(), signed, sig)


def test_max_ttl_is_one_hour() -> None:
    assert MAX_TTL == timedelta(hours=1)


def test_sub_second_now_is_truncated() -> None:
    now = datetime.fromtimestamp(1700000000.999, tz=timezone.utc)
    _, claims, _, _ = _split(
        snowflake_jwt("xy12345", "u", _fixture(KEYS["pkcs8"]), now, timedelta(minutes=1))
    )
    assert (claims["iat"], claims["exp"]) == (1700000000, 1700000060)


def test_naive_now_is_refused() -> None:
    with pytest.raises(ValueError):
        snowflake_jwt(
            "xy12345", "u", _fixture(KEYS["pkcs8"]), datetime(2026, 1, 1), timedelta(minutes=1)
        )


def test_signature_verifies_with_generated_key() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = snowflake_jwt(
        "myorg-myaccount", "svc", _pkcs8(key), datetime.now(timezone.utc), timedelta(minutes=10)
    )
    _, claims, signed, sig = _split(token)
    _verify(key.public_key(), signed, sig)
    assert claims["iss"] == "MYORG-MYACCOUNT.SVC." + public_key_fingerprint(key.public_key())

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidSignature):
        _verify(other.public_key(), signed, sig)


@pytest.mark.parametrize("seconds", CASES["invalid_ttl_seconds"])
def test_invalid_ttl(seconds: int) -> None:
    with pytest.raises(InvalidTTLError):
        snowflake_jwt("xy12345", "u", _fixture(KEYS["pkcs8"]), NOW, timedelta(seconds=seconds))


@pytest.mark.parametrize("key_file", [KEYS["encrypted_pkcs8"], KEYS["encrypted_pkcs1"]])
def test_encrypted_pem_rejected(key_file: str) -> None:
    with pytest.raises(EncryptedPrivateKeyError) as info:
        snowflake_jwt("xy12345", "u", _fixture(key_file), NOW, timedelta(minutes=1))
    assert "encrypted" in str(info.value)
    assert "passphrase" in str(info.value)


def _invalid_keys() -> dict[str, bytes]:
    small = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    ec_key = ec.generate_private_key(ec.SECP256R1())
    return {
        "empty": b"",
        "not pem": b"not a key",
        "public key": _fixture(KEYS["public"]),
        "rsa 1024": _pkcs8(small),
        "ec p256": _pkcs8(ec_key),
        "garbage der": b"-----BEGIN PRIVATE KEY-----\nanVuaw==\n-----END PRIVATE KEY-----\n",
    }


def test_invalid_keys_rejected() -> None:
    for name, pem in _invalid_keys().items():
        with pytest.raises(InvalidKeyError):
            snowflake_jwt("xy12345", "u", pem, NOW, timedelta(minutes=1))
        assert name


def test_errors_never_carry_key_material() -> None:
    key = _fixture(KEYS["pkcs8"])
    body = b"".join(line for line in key.splitlines() if not line.startswith(b"-----"))
    corrupt = bytearray(base64.b64decode(body))
    corrupt[len(corrupt) // 2] ^= 0xFF
    bad = (
        b"-----BEGIN PRIVATE KEY-----\n"
        + base64.encodebytes(bytes(corrupt))
        + b"-----END PRIVATE KEY-----\n"
    )
    attempts: list[tuple[str, str, bytes, timedelta]] = [
        ("xy12345", "u", bad, timedelta(minutes=1)),
        ("xy12345", "u", key[: len(key) // 2], timedelta(minutes=1)),
        ("xy12345", "u", _fixture(KEYS["encrypted_pkcs8"]), timedelta(minutes=1)),
        ("", "u", key, timedelta(minutes=1)),
        ("xy12345", "u", key, timedelta(0)),
    ]
    text = body.decode()
    for account, user, pem, ttl in attempts:
        with pytest.raises(SnowflakeJWTError) as info:
            snowflake_jwt(account, user, pem, NOW, ttl)
        exc = info.value
        rendered = f"{exc!s} {exc!r}"
        assert "BEGIN" not in rendered and "PRIVATE" not in rendered
        assert text[:32] not in rendered and text[len(text) // 2 : len(text) // 2 + 32] not in rendered
        # No parser exception is chained, so tracebacks cannot echo key bytes either.
        assert exc.__cause__ is None
        assert exc.__context__ is None or exc.__suppress_context__


@pytest.mark.parametrize("case", CASES["accounts"], ids=lambda c: repr(c["in"]))
def test_normalize_account(case: dict[str, str]) -> None:
    assert normalize_account_identifier(case["in"]) == case["out"]


@pytest.mark.parametrize("value", CASES["invalid_accounts"])
def test_invalid_account(value: str) -> None:
    with pytest.raises(InvalidAccountIdentifierError):
        normalize_account_identifier(value)


@pytest.mark.parametrize("case", CASES["users"], ids=lambda c: repr(c["in"]))
def test_user_normalized(case: dict[str, str]) -> None:
    token = snowflake_jwt("xy12345", case["in"], _fixture(KEYS["pkcs8"]), NOW, timedelta(minutes=1))
    assert _split(token)[1]["sub"] == "XY12345." + case["out"]


@pytest.mark.parametrize("value", CASES["invalid_users"])
def test_invalid_user(value: str) -> None:
    with pytest.raises(InvalidUserError):
        snowflake_jwt("xy12345", value, _fixture(KEYS["pkcs8"]), NOW, timedelta(minutes=1))


def test_errors_are_value_errors() -> None:
    for cls in (
        EncryptedPrivateKeyError,
        InvalidAccountIdentifierError,
        InvalidKeyError,
        InvalidTTLError,
        InvalidUserError,
    ):
        assert issubclass(cls, SnowflakeJWTError)
        assert issubclass(cls, ValueError)
