"""Vector write/read/query API tests against real Postgres + real Chroma.

Covers the sync upsert path (1–100 records), fetch/delete, query ordering with
real cosine distances, the normalized filter subset, the platform limits
(TOP_K_EXCEEDED / BATCH_SIZE_EXCEEDED / VECTOR_DIMENSION_EXCEEDED) with their
taxonomy codes, dimension-mismatch and reserved-key rejections, and the
tenant-scoped no-existence-oracle 404s.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import get_session
from app.main import app

API = "/api/v1"
DIM = 8
RESERVED = "_vhk_created_at"


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


async def _create_collection(
    client: AsyncClient, headers: dict[str, str], name: str, *, dim: int = DIM
) -> None:
    resp = await client.post(
        f"{API}/collections",
        json={"name": name, "backend": "chroma", "dimension": dim, "distance_metric": "cosine"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text


def _record(rid: str, seed: int = 0, tag: str = "x") -> dict[str, Any]:
    return {
        "id": rid,
        "vector": [float(((seed + i * 3) % 7) + 1) * 0.1 for i in range(DIM)],
        "metadata": {"tag": tag, "seed": seed},
    }


async def test_vector_upsert_fetch_delete_flow(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    await _create_collection(client, headers, "vectors")

    upserted = await client.post(
        f"{API}/collections/vectors/vectors",
        json={"vectors": [_record("doc-1"), _record("doc-2", seed=1)]},
        headers=headers,
    )
    assert upserted.status_code == 200, upserted.text
    assert upserted.json() == {"upserted": 2}

    fetched = await client.get(f"{API}/collections/vectors/vectors/doc-1", headers=headers)
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert body["id"] == "doc-1"
    # Chroma stores float32; the round-trip differs from float64 in the last
    # ulp, so compare approximately.
    assert body["vector"] == pytest.approx(_record("doc-1")["vector"])
    assert body["metadata"] == {"tag": "x", "seed": 0}
    assert body["created_at"] and body["updated_at"]

    # Idempotent upsert preserves created_at but refreshes updated_at.
    first_created = body["created_at"]
    await client.post(
        f"{API}/collections/vectors/vectors",
        json={"vectors": [_record("doc-1", seed=2)]},
        headers=headers,
    )
    refetched = await client.get(f"{API}/collections/vectors/vectors/doc-1", headers=headers)
    assert refetched.json()["metadata"]["seed"] == 2  # overwritten
    assert refetched.json()["created_at"] == first_created  # preserved

    deleted = await client.delete(f"{API}/collections/vectors/vectors/doc-1", headers=headers)
    assert deleted.status_code == 204
    gone = await client.get(f"{API}/collections/vectors/vectors/doc-1", headers=headers)
    assert gone.status_code == 404
    assert gone.json()["error_code"] == "VECTOR_NOT_FOUND"
    # Idempotent delete of the absent id is still a 204.
    again = await client.delete(f"{API}/collections/vectors/vectors/doc-1", headers=headers)
    assert again.status_code == 204


async def test_query_returns_nearest_first(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    await _create_collection(client, headers, "search")
    # Explicit, non-colinear vectors: `near` is the query's direction (cosine
    # distance 0), `far` is far away.
    records = [
        {
            "id": "near",
            "vector": [0.9, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "metadata": {"tag": "near"},
        },
        {
            "id": "far",
            "vector": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
            "metadata": {"tag": "far"},
        },
    ]
    await client.post(
        f"{API}/collections/search/vectors", json={"vectors": records}, headers=headers
    )

    query = {"vector": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], "top_k": 5}
    resp = await client.post(f"{API}/collections/search/query", json=query, headers=headers)
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert [r["id"] for r in results] == ["near", "far"]  # cosine distance ascending
    assert results[0]["score"] < results[1]["score"]
    # The record's metadata round-trips without reserved keys.
    assert set(results[0]["metadata"]) == {"tag"}


async def test_query_metadata_filters(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    await _create_collection(client, headers, "filtered")
    records = [
        {
            "id": f"doc-{i}",
            "vector": [float(i + 1) * 0.1] + [0.0] * (DIM - 1),
            "metadata": {"price": i * 10, "cat": "a" if i % 2 else "b"},
        }
        for i in range(6)
    ]
    await client.post(
        f"{API}/collections/filtered/vectors", json={"vectors": records}, headers=headers
    )
    q = {"vector": [0.5] + [0.0] * (DIM - 1), "top_k": 10}

    eq = await client.post(
        f"{API}/collections/filtered/query", json={**q, "filters": {"cat": "a"}}, headers=headers
    )
    assert {r["id"] for r in eq.json()["results"]} == {"doc-1", "doc-3", "doc-5"}

    gt = await client.post(
        f"{API}/collections/filtered/query",
        json={**q, "filters": {"price": {"$gt": 20}}},
        headers=headers,
    )
    assert {r["id"] for r in gt.json()["results"]} == {"doc-3", "doc-4", "doc-5"}

    both = await client.post(
        f"{API}/collections/filtered/query",
        json={**q, "filters": {"$and": [{"price": {"$gte": 20}}, {"cat": "a"}]}},
        headers=headers,
    )
    assert {r["id"] for r in both.json()["results"]} == {"doc-3", "doc-5"}

    # Malformed filter shape: 422 generic validation, before any backend call.
    bad = await client.post(
        f"{API}/collections/filtered/query", json={**q, "filters": {"$bogus": 1}}, headers=headers
    )
    assert bad.status_code == 422
    assert bad.json()["error_code"] == "VALIDATION_GENERIC"


async def test_vector_limits_and_codes(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    await _create_collection(client, headers, "limits")

    # top_k ceiling: TOP_K_EXCEEDED with the platform code.
    over_topk = await client.post(
        f"{API}/collections/limits/query",
        json={"vector": [0.1] * DIM, "top_k": 1001},
        headers=headers,
    )
    assert over_topk.status_code == 422
    assert over_topk.json()["error_code"] == "TOP_K_EXCEEDED"

    # sync batch ceiling: BATCH_SIZE_EXCEEDED with a hint to the async path.
    over_batch = await client.post(
        f"{API}/collections/limits/vectors",
        json={"vectors": [_record(f"doc-{i}") for i in range(101)]},
        headers=headers,
    )
    assert over_batch.status_code == 422
    body = over_batch.json()
    assert body["error_code"] == "BATCH_SIZE_EXCEEDED"
    assert "vectors/batch" in body["details"]["hint"]

    # vector dimension ceiling: VECTOR_DIMENSION_EXCEEDED.
    huge = await client.post(
        f"{API}/collections/limits/vectors",
        json={"vectors": [{"id": "big", "vector": [0.1] * 4097}]},
        headers=headers,
    )
    assert huge.status_code == 422
    assert huge.json()["error_code"] == "VECTOR_DIMENSION_EXCEEDED"

    # dimension mismatch against the collection: VECTOR_DIMENSION_MISMATCH.
    mismatch = await client.post(
        f"{API}/collections/limits/vectors",
        json={"vectors": [{"id": "small", "vector": [0.1] * 4}]},
        headers=headers,
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error_code"] == "VECTOR_DIMENSION_MISMATCH"

    # reserved metadata prefix rejected at the schema.
    reserved = await client.post(
        f"{API}/collections/limits/vectors",
        json={"vectors": [{"id": "r", "vector": [0.1] * DIM, "metadata": {RESERVED: "x"}}]},
        headers=headers,
    )
    assert reserved.status_code == 422
    assert reserved.json()["error_code"] == "VALIDATION_GENERIC"


async def test_vector_cross_tenant_no_oracle(client: AsyncClient) -> None:
    reg_a = await _register(client, "a")
    reg_b = await _register(client, "b")
    headers_a = _auth_headers(reg_a["access_token"])
    headers_b = _auth_headers(reg_b["access_token"])
    await _create_collection(client, headers_a, "secret")
    await client.post(
        f"{API}/collections/secret/vectors",
        json={"vectors": [_record("doc-1")]},
        headers=headers_a,
    )

    for method, path, body in (
        ("GET", f"{API}/collections/secret/vectors/doc-1", None),
        ("DELETE", f"{API}/collections/secret/vectors/doc-1", None),
        ("POST", f"{API}/collections/secret/query", {"vector": [0.1] * DIM, "top_k": 5}),
        ("POST", f"{API}/collections/secret/vectors", {"vectors": [_record("x")]}),
    ):
        resp = await client.request(method, path, json=body, headers=headers_b)
        assert resp.status_code == 404, f"{method} {path}: {resp.status_code}"
        assert resp.json()["error_code"] == "COLLECTION_NOT_FOUND"

    # A's data untouched.
    still = await client.get(f"{API}/collections/secret/vectors/doc-1", headers=headers_a)
    assert still.status_code == 200


async def test_vector_forged_tenant_id_422(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    await _create_collection(client, headers, "legit")
    resp = await client.post(
        f"{API}/collections/legit/vectors",
        json={"vectors": [{"id": "x", "vector": [0.1] * DIM}], "tenant_id": "hacker"},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
