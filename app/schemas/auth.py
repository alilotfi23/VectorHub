from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Auth ---


# Lightweight email shape check (no email-validator dependency — PyPI was
# unreachable when this landed). Swap to pydantic EmailStr if/when the extra
# is installable; behavior (422 on malformed addresses) is identical.
_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class RegisterRequest(BaseModel):
    email: str = Field(
        min_length=3, max_length=255, pattern=_EMAIL_PATTERN, examples=["alice@example.com"]
    )
    password: str = Field(min_length=8, max_length=128, description="Minimum 8 characters")
    tenant_name: str = Field(
        min_length=1, max_length=255, description="New tenant created for this user"
    )


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255, pattern=_EMAIL_PATTERN)
    password: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    tenant_id: str
    role: str
    is_platform_admin: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # access-token TTL in seconds


class AuthResponse(TokenResponse):
    user: UserResponse


class MeResponse(BaseModel):
    id: str
    email: str
    tenant_id: str
    tenant_name: str
    role: str
    is_platform_admin: bool


# --- Tenants ---


class TenantCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class TenantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    created_at: datetime


# --- API keys ---


ApiKeyRole = Literal["owner", "admin", "editor", "viewer"]


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    role: ApiKeyRole = "editor"  # least-privilege default
    expires_at: datetime | None = None
    rate_limit_qps: int | None = Field(default=None, ge=1, le=1_000_000)


class ApiKeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    prefix: str
    role: str
    expires_at: datetime | None
    revoked: bool
    created_at: datetime


class ApiKeyCreatedResponse(ApiKeyResponse):
    key: str  # plaintext — shown exactly once, never retrievable again
