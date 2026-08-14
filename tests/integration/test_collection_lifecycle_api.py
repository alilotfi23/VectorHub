"""Collection lifecycle API tests against real Postgres + real Chroma.

Covers create/list/get/delete, the read-path ``backend_status`` drift field,
the PATCH config 409 REQUIRES_REINDEX contract, the reindex 501 stub, the
tenant-scoped no-oracle 404s, and the extra="forbid" forge rejection.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.chroma_adapter import ChromaAdapter
from app.adapters.registry import registry
from app.db.session import get_session
from app.main import app

API = "/api/v1"


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _unique_email(local: str = "user") -> str:
    return f"{local}-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    chroma_backend: None,
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
    resp = await client.post(
        f"{API}/auth/register",
        json={"email": _unique_email(tag), "password": "password-123", "tenant_name": _unique(tag)},
    )
    assert resp.status_code == 201, resp.text
    return cast(dict[str, Any], resp.json())


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create(client: AsyncClient, headers: dict[str, str], name: str, **overrides: Any) -> Any:
    body = {
        "name": name,
        "backend": "chroma",
        "dimension": 8,
        "distance_metric": "cosine",
        **overrides,
    }
    return await client.post(f"{API}/collections", json=body, headers=headers)


async def test_collection_lifecycle(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])

    created = await _create(client, headers, "products")
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "products"
    assert body["backend"] == "chroma"
    assert body["dimension"] == 8
    assert body["distance_metric"] == "cosine"
    assert body["backend_status"] == "exists"

    fetched = await client.get(f"{API}/collections/products", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["backend_status"] == "exists"
    assert fetched.json()["name"] == "products"

    listed = await client.get(f"{API}/collections", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["name"] == "products"
    assert listed.json()["items"][0]["backend_status"] == "exists"

    deleted = await client.delete(f"{API}/collections/products", headers=headers)
    assert deleted.status_code == 204
    after = await client.get(f"{API}/collections/products", headers=headers)
    assert after.status_code == 404
    assert after.json()["error_code"] == "COLLECTION_NOT_FOUND"


async def test_collection_duplicate_name_409(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    assert (await _create(client, headers, "products")).status_code == 201
    dup = await _create(client, headers, "products")
    assert dup.status_code == 409
    assert dup.json()["error_code"] == "COLLECTION_ALREADY_EXISTS"


async def test_collection_unregistered_backend_503(client: AsyncClient) -> None:
    """A schema-valid backend that isn't registered is 503
    COLLECTION_BACKEND_UNAVAILABLE, not a silent fallback. All four backends
    are built-ins as of Phase 5, so the test unregisters one deterministically
    (and restores it) — never depending on what other suites left registered."""
    from app.adapters.milvus_adapter import MilvusAdapter

    registry.unregister("milvus")
    try:
        reg = await _register(client)
        headers = _auth_headers(reg["access_token"])
        resp = await _create(client, headers, "milvus-coll", backend="milvus")
    finally:
        registry.register("milvus", MilvusAdapter)
    assert resp.status_code == 503
    assert resp.json()["error_code"] == "COLLECTION_BACKEND_UNAVAILABLE"


async def test_collection_backend_status_missing(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Delete the physical object behind a registry row (drift) -> the read
    path reports `missing` without failing the request."""
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    assert (await _create(client, headers, "drifted")).status_code == 201

    adapter = registry.get("chroma")
    assert isinstance(adapter, ChromaAdapter)
    from sqlalchemy import select

    from app.db.models import Collection

    async with session_factory() as session:
        physical = await session.scalar(
            select(Collection.physical_name).where(Collection.name == "drifted")
        )
    assert physical is not None
    await adapter.delete_collection(name=physical)

    fetched = await client.get(f"{API}/collections/drifted", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["backend_status"] == "missing"
    listed = await client.get(f"{API}/collections", headers=headers)
    assert listed.json()["items"][0]["backend_status"] == "missing"


async def test_collection_cross_tenant_no_oracle(client: AsyncClient) -> None:
    reg_a = await _register(client, "a")
    reg_b = await _register(client, "b")
    headers_a = _auth_headers(reg_a["access_token"])
    headers_b = _auth_headers(reg_b["access_token"])
    assert (await _create(client, headers_a, "a-only")).status_code == 201

    foreign = await client.get(f"{API}/collections/a-only", headers=headers_b)
    missing = await client.get(f"{API}/collections/{_unique('none')}", headers=headers_b)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()  # no existence oracle
    assert foreign.json()["error_code"] == "COLLECTION_NOT_FOUND"


async def test_collection_config_patch_requires_reindex(client: AsyncClient) -> None:
    """Chroma's index config is creation-time: any PATCH /config is a 409
    REQUIRES_REINDEX with a stated next_step, never a silent no-op."""
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    assert (await _create(client, headers, "cfg")).status_code == 201

    resp = await client.patch(
        f"{API}/collections/cfg/config", json={"index_config": {"m": 32}}, headers=headers
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error_code"] == "REQUIRES_REINDEX"
    assert body["details"]["next_step"] == f"POST {API}/collections/cfg/reindex"
    assert body["details"]["mutable"] == []
    assert body["details"]["requested"] == ["m"]


async def test_collection_reindex_stub_501(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    assert (await _create(client, headers, "reidx")).status_code == 201

    resp = await client.post(f"{API}/collections/reidx/reindex", headers=headers)
    assert resp.status_code == 501, resp.text
    assert resp.json()["error_code"] == "REINDEX_NOT_IMPLEMENTED"

    # Cross-tenant: the stub still 404s before the 501 (no existence oracle).
    reg_b = await _register(client, "b")
    foreign = await client.post(
        f"{API}/collections/reidx/reindex", headers=_auth_headers(reg_b["access_token"])
    )
    assert foreign.status_code == 404
    assert foreign.json()["error_code"] == "COLLECTION_NOT_FOUND"


async def test_collection_forged_tenant_id_422(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    resp = await _create(client, headers, "forged", tenant_id="hacker-tenant")
    assert resp.status_code == 422, resp.text


async def test_collection_list_pagination(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    for i in range(3):
        assert (await _create(client, headers, f"coll-{i}")).status_code == 201

    page1 = await client.get(f"{API}/collections", params={"limit": 2}, headers=headers)
    assert page1.status_code == 200
    assert page1.json()["total"] == 3
    assert len(page1.json()["items"]) == 2
    cursor = page1.json()["next_cursor"]
    assert cursor is not None
    page2 = await client.get(
        f"{API}/collections", params={"limit": 2, "cursor": cursor}, headers=headers
    )
    names = [c["name"] for c in page1.json()["items"]] + [c["name"] for c in page2.json()["items"]]
    assert sorted(names) == ["coll-0", "coll-1", "coll-2"]
    assert page2.json()["next_cursor"] is None
