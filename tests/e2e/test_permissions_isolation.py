"""Layer 3 of the cross-tenant isolation suite — collection permissions, e2e.

Two real principals (registered tenants) drive the full HTTP surface of the
resource-level grant API, each case exercised with BOTH principal types the
design doc requires — JWT (Bearer) and per-tenant API keys. Mirrors cases
E1/E2/E3/E8 of the isolation design doc, adapted to a control-plane surface:
indistinguishable data (identical collection names), fail-closed 404s,
schema-level rejection of forged `tenant_id`, and a byte-identical negative
control proving responses can't act as an existence oracle. See
docs/superpowers/specs/2026-08-14-tenant-isolation-tests-design.md.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Collection
from app.db.session import get_session
from app.main import app

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


AUTH_STYLES = ("jwt", "api_key")


async def _principal_headers(client: AsyncClient, reg: dict[str, Any], auth: str) -> dict[str, str]:
    """Per-tenant principal credentials: a Bearer token (JWT) or a per-tenant
    API key minted at owner rank (the grants surface requires TENANT_MANAGE)
    — the design doc's two-principal-types requirement."""
    if auth == "jwt":
        return _auth_headers(reg["access_token"])
    created = await client.post(
        f"{API}/api-keys",
        json={"name": f"iso-{uuid.uuid4().hex[:10]}", "role": "owner"},
        headers=_auth_headers(reg["access_token"]),
    )
    assert created.status_code == 201, created.text
    return {"X-API-Key": created.json()["key"]}


async def _seed_collection(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: str, name: str
) -> None:
    # The create-collection route lands in Phase 3; seed the registry row
    # directly like the integration API tests do.
    async with session_factory() as session:
        collection = Collection(
            tenant_id=tenant_id,
            name=name,
            backend="chroma",
            dimension=8,
            distance_metric="cosine",
            physical_name=f"col_{uuid.uuid4().hex[:12]}",
        )
        session.add(collection)
        await session.commit()


async def _provision_member(
    client: AsyncClient, headers: dict[str, str], tenant_id: str
) -> dict[str, Any]:
    resp = await client.post(
        f"{API}/tenants/{tenant_id}/members",
        json={"email": _unique_email("member"), "password": "password-123"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return cast(dict[str, Any], resp.json())


@pytest.mark.parametrize("auth", AUTH_STYLES)
async def test_e1_same_name_collision_grants_invisible_across_tenants(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], auth: str
) -> None:
    """A and B both have `products`; grants made in A are invisible from B on
    every path, and same-name ops never touch the other tenant's rows."""
    reg_a = await _register(client, "orga")
    reg_b = await _register(client, "orgb")
    headers_a = await _principal_headers(client, reg_a, auth)
    headers_b = await _principal_headers(client, reg_b, auth)
    tenant_a = reg_a["user"]["tenant_id"]
    tenant_b = reg_b["user"]["tenant_id"]

    await _seed_collection(session_factory, tenant_a, "products")
    await _seed_collection(session_factory, tenant_b, "products")
    member_a = await _provision_member(client, headers_a, tenant_a)
    member_b = await _provision_member(client, headers_b, tenant_b)

    # A grants editor to its member on `products`.
    granted = await client.patch(
        f"{API}/collections/products/permissions",
        json={"user_id": member_a["id"], "role": "editor"},
        headers=headers_a,
    )
    assert granted.status_code == 200, granted.text

    # B, with the identical name, sees nothing.
    b_list = await client.get(f"{API}/collections/products/permissions", headers=headers_b)
    assert b_list.status_code == 200
    body = b_list.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["next_cursor"] is None

    # B tries to revoke A's grantee by user_id on the same name: resolves to
    # B's own collection row, no-op 204, A's grant untouched.
    b_revoke = await client.delete(
        f"{API}/collections/products/permissions/{member_a['id']}", headers=headers_b
    )
    assert b_revoke.status_code == 204
    a_list = await client.get(f"{API}/collections/products/permissions", headers=headers_a)
    assert a_list.status_code == 200
    assert [g["user_id"] for g in a_list.json()["items"]] == [member_a["id"]]

    # B grants its own member on `products`: lands under B only.
    b_grant = await client.patch(
        f"{API}/collections/products/permissions",
        json={"user_id": member_b["id"], "role": "viewer"},
        headers=headers_b,
    )
    assert b_grant.status_code == 200
    b_list = await client.get(f"{API}/collections/products/permissions", headers=headers_b)
    assert [g["user_id"] for g in b_list.json()["items"]] == [member_b["id"]]
    a_list = await client.get(f"{API}/collections/products/permissions", headers=headers_a)
    assert [g["user_id"] for g in a_list.json()["items"]] == [member_a["id"]]


@pytest.mark.parametrize("auth", AUTH_STYLES)
async def test_e2_cross_tenant_ops_not_found_and_do_not_touch_data(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], auth: str
) -> None:
    """B's GET/PATCH/DELETE on A's distinctly-named collection all 404, and
    A's grants survive."""
    reg_a = await _register(client, "orga")
    reg_b = await _register(client, "orgb")
    headers_a = await _principal_headers(client, reg_a, auth)
    headers_b = await _principal_headers(client, reg_b, auth)
    tenant_a = reg_a["user"]["tenant_id"]

    await _seed_collection(session_factory, tenant_a, "a-only")
    member_a = await _provision_member(client, headers_a, tenant_a)
    granted = await client.patch(
        f"{API}/collections/a-only/permissions",
        json={"user_id": member_a["id"], "role": "editor"},
        headers=headers_a,
    )
    assert granted.status_code == 200

    # B's GET, PATCH, and DELETE on A's collection: all COLLECTION_NOT_FOUND.
    for method, path in (
        ("GET", f"{API}/collections/a-only/permissions"),
        ("PATCH", f"{API}/collections/a-only/permissions"),
        ("DELETE", f"{API}/collections/a-only/permissions/{member_a['id']}"),
    ):
        if method == "PATCH":
            resp = await client.patch(
                path, json={"user_id": member_a["id"], "role": "viewer"}, headers=headers_b
            )
        else:
            resp = await getattr(client, method.lower())(path, headers=headers_b)
        assert resp.status_code == 404, f"{method} {path}: {resp.status_code} {resp.text}"
        assert resp.json()["error_code"] == "COLLECTION_NOT_FOUND"

    # A's grant survives every cross-tenant attempt.
    a_list = await client.get(f"{API}/collections/a-only/permissions", headers=headers_a)
    assert a_list.status_code == 200
    assert [g["user_id"] for g in a_list.json()["items"]] == [member_a["id"]]


@pytest.mark.parametrize("auth", AUTH_STYLES)
async def test_e3_forged_tenant_id_rejected_at_schema(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], auth: str
) -> None:
    """A PATCH body carrying `tenant_id` is rejected with 422 (the envelope
    forbids the field) — the forged value never reaches the service and no
    grant is created."""
    reg_a = await _register(client, "orga")
    reg_b = await _register(client, "orgb")
    headers_a = await _principal_headers(client, reg_a, auth)
    tenant_a = reg_a["user"]["tenant_id"]
    tenant_b = reg_b["user"]["tenant_id"]
    assert tenant_a != tenant_b

    await _seed_collection(session_factory, tenant_a, "products")
    member_a = await _provision_member(client, headers_a, tenant_a)

    forged = await client.patch(
        f"{API}/collections/products/permissions",
        json={"user_id": member_a["id"], "role": "editor", "tenant_id": tenant_b},
        headers=headers_a,
    )
    assert forged.status_code == 422, forged.text

    # Fail-closed: nothing was written.
    a_list = await client.get(f"{API}/collections/products/permissions", headers=headers_a)
    assert a_list.status_code == 200
    assert a_list.json()["items"] == []


@pytest.mark.parametrize("auth", AUTH_STYLES)
async def test_e8_negative_control_no_existence_oracle(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], auth: str
) -> None:
    """A tenant with no collections probes a name that exists in another
    tenant and a random name: byte-identical 404s."""
    reg_a = await _register(client, "orga")
    reg_c = await _register(client, "orgc")
    headers_a = await _principal_headers(client, reg_a, auth)
    headers_c = await _principal_headers(client, reg_c, auth)
    tenant_a = reg_a["user"]["tenant_id"]

    await _seed_collection(session_factory, tenant_a, "products")

    exists_elsewhere = await client.get(
        f"{API}/collections/products/permissions", headers=headers_c
    )
    missing_everywhere = await client.get(
        f"{API}/collections/{uuid.uuid4().hex}/permissions", headers=headers_c
    )
    assert exists_elsewhere.status_code == 404
    assert missing_everywhere.status_code == 404
    # Byte-identical error bodies: no existence oracle.
    assert exists_elsewhere.json() == missing_everywhere.json()

    # And A, for whom the collection genuinely exists, sees grants fine.
    a_list = await client.get(f"{API}/collections/products/permissions", headers=headers_a)
    assert a_list.status_code == 200
