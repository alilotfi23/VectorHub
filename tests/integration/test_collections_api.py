import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.rbac import Permission as Perm
from app.core.security import Principal
from app.db.models import Collection
from app.db.session import get_session
from app.main import app
from app.services.collection_service import CollectionService

API = "/api/v1"


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _unique_email(local: str = "user") -> str:
    # Suffix goes in the local part: EmailStr validates the domain strictly.
    return f"{local}-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _register(client: AsyncClient, tag: str = "user") -> dict[str, Any]:
    body = {
        "email": _unique_email(tag),
        "password": "password-123",
        "tenant_name": _unique(tag),
    }
    resp = await client.post(f"{API}/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    return cast(dict[str, Any], resp.json())


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_collection_permissions_route(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    tenant_id = reg["user"]["tenant_id"]

    # Provision a viewer member, and create the collection row directly (the
    # create-collection route lands in Phase 3).
    member_resp = await client.post(
        f"{API}/tenants/{tenant_id}/members",
        json={"email": _unique_email("coll-viewer"), "password": "password-123"},
        headers=headers,
    )
    assert member_resp.status_code == 201
    member = member_resp.json()

    async with session_factory() as session:
        collection = Collection(
            tenant_id=tenant_id,
            name="products",
            backend="chroma",
            dimension=8,
            distance_metric="cosine",
            physical_name=f"col_{uuid.uuid4().hex[:12]}",
        )
        session.add(collection)
        await session.commit()

    granted = await client.patch(
        f"{API}/collections/products/permissions",
        json={"user_id": member["id"], "role": "editor"},
        headers=headers,
    )
    assert granted.status_code == 200, granted.text
    body = granted.json()
    assert body["collection_name"] == "products"
    assert body["user_id"] == member["id"]
    assert body["role"] == "editor"

    # The member's editor grant now covers collection-scoped write checks.
    member_login = await client.post(
        f"{API}/auth/login", json={"email": member["email"], "password": "password-123"}
    )
    assert member_login.status_code == 200
    member_headers = _auth_headers(member_login.json()["access_token"])

    async with session_factory() as session:
        principal = Principal(user_id=member["id"], tenant_id=tenant_id, role="viewer")
        await CollectionService(session).check_access(
            principal, Perm.COLLECTION_WRITE, name="products"
        )

    # Upsert: re-PATCH with a lower role updates in place.
    re_granted = await client.patch(
        f"{API}/collections/products/permissions",
        json={"user_id": member["id"], "role": "viewer"},
        headers=headers,
    )
    assert re_granted.status_code == 200
    assert re_granted.json()["role"] == "viewer"

    # The member (editor on tenant, now viewer via grant) cannot manage grants.
    denied = await client.patch(
        f"{API}/collections/products/permissions",
        json={"user_id": member["id"], "role": "viewer"},
        headers=member_headers,
    )
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "AUTH_INSUFFICIENT_SCOPE"

    # Foreign collection: no existence oracle.
    other = await _register(client, "other")
    foreign_headers = _auth_headers(other["access_token"])
    foreign = await client.patch(
        f"{API}/collections/products/permissions",
        json={"user_id": member["id"], "role": "viewer"},
        headers=foreign_headers,
    )
    assert foreign.status_code == 404
    assert foreign.json()["error_code"] == "COLLECTION_NOT_FOUND"
