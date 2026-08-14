"""Every request envelope is strict (extra=\"forbid\") per the isolation
contract (design doc R3/E3): a forged tenant_id/user_id/owner field must be
rejected with a ValidationError, never silently dropped. This is the
schema-level half of the proof; the wire-level half lives in the API tests.
"""

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.schemas.auth import (
    ApiKeyCreateRequest,
    LoginRequest,
    LogoutRequest,
    MemberCreateRequest,
    MemberRoleUpdateRequest,
    RefreshRequest,
    RegisterRequest,
    TenantCreateRequest,
)
from app.schemas.collections import CollectionPermissionUpdateRequest

REQUEST_SCHEMAS: list[type[BaseModel]] = [
    RegisterRequest,
    LoginRequest,
    RefreshRequest,
    LogoutRequest,
    TenantCreateRequest,
    ApiKeyCreateRequest,
    MemberCreateRequest,
    MemberRoleUpdateRequest,
    CollectionPermissionUpdateRequest,
]


def _valid_payload(schema: type[BaseModel]) -> dict[str, Any]:
    if schema is RegisterRequest:
        return {"email": "a@example.com", "password": "password-123", "tenant_name": "acme"}
    if schema is LoginRequest:
        return {"email": "a@example.com", "password": "password-123"}
    if schema in (RefreshRequest, LogoutRequest):
        return {"refresh_token": "tok"}
    if schema is TenantCreateRequest:
        return {"name": "acme"}
    if schema is ApiKeyCreateRequest:
        return {"name": "ci"}
    if schema is MemberCreateRequest:
        return {"email": "m@example.com", "password": "password-123"}
    if schema is MemberRoleUpdateRequest:
        return {"role": "editor"}
    if schema is CollectionPermissionUpdateRequest:
        return {"user_id": "u-1", "role": "viewer"}
    raise AssertionError(f"unhandled schema: {schema}")


@pytest.mark.parametrize("schema", REQUEST_SCHEMAS, ids=[c.__name__ for c in REQUEST_SCHEMAS])
def test_request_schemas_reject_forged_fields(schema: type[BaseModel]) -> None:
    base = _valid_payload(schema)
    assert schema.model_validate(base)  # the valid envelope still validates

    # Fields that exist on NO request envelope — always forged: tenant_id /
    # owner_id (tenant scoping) and is_platform_admin (privilege escalation).
    # user_id and role are legitimate on some envelopes (the grantee, the
    # invited member's / api key's role), so they are forged only where the
    # schema does not declare them.
    declared = set(schema.model_fields)
    forged_fields: set[str] = {"tenant_id", "owner_id", "is_platform_admin"}
    for field in ("user_id", "role"):
        if field not in declared:
            forged_fields.add(field)
    for forged in forged_fields:
        with pytest.raises(ValidationError):
            schema.model_validate({**base, forged: "forged-value"})
