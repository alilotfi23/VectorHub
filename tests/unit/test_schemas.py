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
from app.schemas.collections import (
    CollectionConfigUpdateRequest,
    CollectionCreateRequest,
    CollectionPermissionUpdateRequest,
)
from app.schemas.vectors import (
    RESERVED_METADATA_PREFIX,
    QueryRequest,
    VectorRecordIn,
    VectorUpsertRequest,
)

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
    CollectionCreateRequest,
    CollectionConfigUpdateRequest,
    VectorUpsertRequest,
    QueryRequest,
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
    if schema is CollectionCreateRequest:
        return {
            "name": "products",
            "backend": "chroma",
            "dimension": 8,
            "distance_metric": "cosine",
        }
    if schema is CollectionConfigUpdateRequest:
        return {"index_config": {"m": 16}}
    if schema is VectorUpsertRequest:
        return {"vectors": [{"id": "doc-1", "vector": [0.1, 0.2]}]}
    if schema is QueryRequest:
        return {"vector": [0.1, 0.2], "top_k": 5}
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


def test_query_filter_shape_validation() -> None:
    """The normalized filter subset rejects unknown operators and malformed
    nesting while accepting the documented shapes (equality shorthand,
    comparison operators, $and/$or/$not)."""
    QueryRequest(vector=[0.1], filters={"status": "active"})  # equality shorthand
    QueryRequest(vector=[0.1], filters={"price": {"$gt": 5, "$lt": 10}})
    QueryRequest(
        vector=[0.1], filters={"$and": [{"a": 1}, {"$or": [{"b": {"$in": [1, 2]}}, {"c": "x"}]}]}
    )
    QueryRequest(vector=[0.1], filters={"$not": {"tag": "x"}})
    with pytest.raises(ValidationError):
        QueryRequest(vector=[0.1], filters={"$bogus": 1})
    with pytest.raises(ValidationError):
        QueryRequest(vector=[0.1], filters={"field": {"$bogus": 1}})
    with pytest.raises(ValidationError):
        QueryRequest(vector=[0.1], filters={"$and": []})
    with pytest.raises(ValidationError):
        QueryRequest(vector=[0.1], filters={"field": [1, 2]})  # list is not a value form
    with pytest.raises(ValidationError):
        QueryRequest(vector=[0.1], filters={"tags": {"$in": "not-a-list"}})


def test_vector_metadata_reserved_prefix_rejected() -> None:
    """The _vhk_ prefix is reserved for the platform's internal storage keys
    (Chroma folds timestamps/tenant into metadata) — user payloads must not
    collide with it."""
    with pytest.raises(ValidationError):
        VectorRecordIn(
            id="doc-1", vector=[0.1], metadata={f"{RESERVED_METADATA_PREFIX}created_at": "x"}
        )
    VectorRecordIn(id="doc-1", vector=[0.1], metadata={"regular": "fine"})
