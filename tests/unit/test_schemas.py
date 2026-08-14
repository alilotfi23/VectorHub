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

    # tenant_id / owner_id exist on NO request envelope — rejected everywhere.
    for forged in ("tenant_id", "owner_id"):
        with pytest.raises(ValidationError):
            schema.model_validate({**base, forged: "forged-value"})

    # user_id is a legitimate grantee field ONLY on the grant envelope; on
    # every other schema it is forged and must be rejected.
    if schema is not CollectionPermissionUpdateRequest:
        with pytest.raises(ValidationError):
            schema.model_validate({**base, "user_id": "forged-value"})
