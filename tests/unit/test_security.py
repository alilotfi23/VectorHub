from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.exceptions import AppError, ErrorCode
from app.core.security import (
    Principal,
    create_access_token,
    decode_access_token,
    generate_api_key,
    generate_refresh_token,
    hash_api_key,
    hash_password,
    hash_refresh_token,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password", hashed)


def test_password_hashes_are_salted() -> None:
    assert hash_password("same-password") != hash_password("same-password")


def test_access_token_roundtrip_claims() -> None:
    token = create_access_token("user-1", "tenant-1", "admin", is_platform_admin=False)
    principal = decode_access_token(token).principal
    assert principal.user_id == "user-1"
    assert principal.tenant_id == "tenant-1"
    assert principal.role == "admin"
    assert principal.is_platform_admin is False
    assert principal.api_key_id is None


def test_access_token_carries_platform_admin() -> None:
    token = create_access_token("user-1", "tenant-1", "owner", is_platform_admin=True)
    assert decode_access_token(token).principal.is_platform_admin is True


def test_access_token_jti_is_exposed_and_distinct() -> None:
    """Every issued token carries its own jti (needed at the auth boundary
    for the revocation deny-list), and a fresh token gets a fresh one — so
    revoking one session never touches another."""
    token_a = create_access_token("user-1", "tenant-1", "admin", is_platform_admin=False)
    token_b = create_access_token("user-1", "tenant-1", "admin", is_platform_admin=False)
    decoded_a = decode_access_token(token_a)
    decoded_b = decode_access_token(token_b)
    assert decoded_a.jti
    assert decoded_a.jti != decoded_b.jti
    assert decoded_a.principal.user_id == "user-1"


def test_expired_token_rejected() -> None:
    settings = get_settings()
    payload = {
        "sub": "user-1",
        "tid": "tenant-1",
        "role": "viewer",
        "adm": False,
        "jti": "x",
        "iat": datetime.now(UTC) - timedelta(hours=1),
        "exp": datetime.now(UTC) - timedelta(minutes=1),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(AppError) as exc:
        decode_access_token(token)
    assert exc.value.code == ErrorCode.AUTH_TOKEN_EXPIRED


def test_malformed_token_rejected() -> None:
    with pytest.raises(AppError) as exc:
        decode_access_token("not-a-jwt")
    assert exc.value.code == ErrorCode.AUTH_INVALID_CREDENTIALS


def test_tampered_token_rejected() -> None:
    token = create_access_token("user-1", "tenant-1", "admin", is_platform_admin=False)
    with pytest.raises(AppError) as exc:
        decode_access_token(token[:-2] + ("xx" if token[-2:] != "xx" else "yy"))
    assert exc.value.code == ErrorCode.AUTH_INVALID_CREDENTIALS


def test_refresh_token_generation_and_hashing() -> None:
    t1 = generate_refresh_token()
    t2 = generate_refresh_token()
    assert t1 != t2
    assert len(t1) >= 32
    h = hash_refresh_token(t1)
    assert h == hash_refresh_token(t1)
    assert h != t1  # never store the raw token
    assert len(h) == 64  # sha256 hexdigest


def test_api_key_generation_and_hashing() -> None:
    plaintext, prefix = generate_api_key()
    assert plaintext.startswith("vhk_")
    assert prefix.startswith("vhk_")
    assert len(prefix) == 12  # vhk_ + 8 chars, display-only
    assert hash_api_key(plaintext) != plaintext
    assert hash_api_key(plaintext) == hash_api_key(plaintext)


def test_principal_frozen() -> None:
    p = Principal(user_id="u", tenant_id="t", role="viewer")
    with pytest.raises(FrozenInstanceError):
        p.role = "admin"  # type: ignore[misc]
