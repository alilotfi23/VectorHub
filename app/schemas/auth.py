from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# --- Auth ---


class RegisterRequest(BaseModel):
    email: EmailStr = Field(min_length=3, max_length=255, examples=["alice@example.com"])
    password: str = Field(min_length=8, max_length=128, description="Minimum 8 characters")
    tenant_name: str = Field(
        min_length=1, max_length=255, description="New tenant created for this user"
    )


class LoginRequest(BaseModel):
    email: EmailStr = Field(min_length=3, max_length=255)
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


# --- Tenant members ---

# Users belong to exactly one tenant, so "invite" = an admin/owner provisions
# a member account inside the tenant with an initial password. True
# multi-membership (one user, several tenants) would require a membership
# table and is out of scope for v1.

MemberRole = Literal["owner", "admin", "editor", "viewer"]


class MemberCreateRequest(BaseModel):
    email: EmailStr = Field(min_length=3, max_length=255)
    password: str = Field(
        min_length=8, max_length=128, description="Initial password set by the inviting admin"
    )
    role: MemberRole = "viewer"  # least-privilege default


class MemberRoleUpdateRequest(BaseModel):
    role: MemberRole


class MemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    role: str
    created_at: datetime
