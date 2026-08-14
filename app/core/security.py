"""Password hashing, JWT issuance/verification, opaque-token hashing, Principal.

Password hashing uses bcrypt directly (not passlib, which is unmaintained and
incompatible with bcrypt >= 4.1). Keeping hashing behind these two functions
means a future swap (e.g. argon2) is a one-file change.

Access tokens are stateless JWTs (HS256) carrying user/tenant/role claims;
short TTL (~15 min). Refresh tokens are opaque, stored hashed (sha256) in
Postgres with rotation — never sent to the client twice. Access-token
revocation is a Postgres-backed jti deny-list (revoked_tokens) checked at
the auth boundary; Redis caching of that list lands in Phase 6 with the
rest of the Redis infrastructure.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from jwt import InvalidTokenError

from app.core.config import get_settings
from app.core.exceptions import AppError, ErrorCode


@dataclass(frozen=True)
class Principal:
    """Authenticated caller, derived server-side from the presented credential.

    Never constructed from client-supplied data: built from JWT claims or the
    API-key row the presented key hashes to.
    """

    tenant_id: str
    role: str
    user_id: str | None = None  # None for API-key principals (no user behind the key)
    is_platform_admin: bool = False
    api_key_id: str | None = None


@dataclass(frozen=True)
class DecodedAccessToken:
    """A verified access token: the Principal it carries plus its jti.

    jti is credential-specific (not identity), so it stays off Principal —
    the auth boundary needs it to check the revocation deny-list.
    """

    principal: Principal
    jti: str


# --- Passwords ---


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


# --- Access tokens (JWT) ---


def create_access_token(user_id: str, tenant_id: str, role: str, is_platform_admin: bool) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "tid": tenant_id,
        "role": role,
        "adm": is_platform_admin,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_ttl_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> DecodedAccessToken:
    settings = get_settings()
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AppError(
            ErrorCode.AUTH_TOKEN_EXPIRED, "Access token expired", status_code=401
        ) from exc
    except InvalidTokenError as exc:
        raise AppError(
            ErrorCode.AUTH_INVALID_CREDENTIALS, "Invalid access token", status_code=401
        ) from exc
    return DecodedAccessToken(
        principal=Principal(
            user_id=str(claims["sub"]),
            tenant_id=str(claims["tid"]),
            role=str(claims["role"]),
            is_platform_admin=bool(claims.get("adm", False)),
        ),
        jti=str(claims["jti"]),
    )


# --- Opaque tokens (refresh tokens, API keys) ---


def generate_refresh_token() -> str:
    """Opaque high-entropy token; only its sha256 hash is ever stored."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Return (plaintext_key, display_prefix).

    Plaintext is shown exactly once at creation; the DB stores only its hash
    plus the prefix for display in list views.
    """
    raw = secrets.token_urlsafe(32)
    plaintext = f"vhk_{raw}"
    return plaintext, plaintext[:12]


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
