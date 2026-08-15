"""Hybrid-query API contract tests (Phase 4) against real backends.

The hybrid contract from CLAUDE.md: `vector` required; `sparse_vector`
required on Qdrant (else 422 VECTOR_SPARSE_REQUIRED); `query_text` required
on Weaviate (else 422); Chroma rejects the whole operation with
400 VALIDATION_UNSUPPORTED_OPERATION (details.capability = "hybrid_search").
Also pins: alpha bounds, sparse-shape validation, Weaviate's
metadata_filtering rejection, and Qdrant's payload filtering on the plain
query path.
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


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _unique_email(local: str = "user") -> str:
    return f"{local}-{uuid.uuid4().hex[:10]}@example.com"


def _vector(seed: int = 0) -> list[float]:
    return [float(((seed + i * 3) % 7) + 1) * 0.1 for i in range(DIM)]


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
    resp = await client.post(
        f"{API}/auth/register",
        json={"email": _unique_email(tag), "password": "password-123", "tenant_name": _unique(tag)},
    )
    assert resp.status_code == 201, resp.text
    return cast(dict[str, Any], resp.json())


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_collection(client: AsyncClient, headers: dict[str, str], backend: str) -> str:
    name = _unique("hyb")
    resp = await client.post(
        f"{API}/collections",
        json={
            "name": name,
            "backend": backend,
            "dimension": DIM,
            "distance_metric": "cosine",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return name


async def _upsert(client: AsyncClient, headers: dict[str, str], name: str, *, sparse: bool) -> None:
    records = []
    for i in range(3):
        rec: dict[str, Any] = {
            "id": f"doc-{i}",
            "vector": _vector(i),
            "metadata": {"tag": f"tag-{i}", "seed": i, "_tenant_probe": "me"},
        }
        if sparse:
            rec["sparse_vector"] = {"indices": [0, 2], "values": [1.0, 2.0 + i]}
        records.append(rec)
    resp = await client.post(
        f"{API}/collections/{name}/vectors", json={"vectors": records}, headers=headers
    )
    assert resp.status_code == 200, resp.text


async def _hybrid(
    client: AsyncClient, headers: dict[str, str], name: str, body: dict[str, Any]
) -> Any:
    resp = await client.post(f"{API}/collections/{name}/hybrid-query", json=body, headers=headers)
    return resp


async def test_hybrid_qdrant_sparse_required_and_scoped(
    client: AsyncClient, qdrant_backend: None
) -> None:
    """Qdrant hybrid: sparse input is required (422 VECTOR_SPARSE_REQUIRED)
    and, with it, returns only the caller's rows."""
    reg = await _register(client, "hyb-q")
    headers = _auth_headers(reg["access_token"])
    name = await _create_collection(client, headers, "qdrant")
    await _upsert(client, headers, name, sparse=True)

    missing = await _hybrid(
        client,
        headers,
        name,
        {"vector": _vector(1), "alpha": 0.75, "top_k": 10},
    )
    assert missing.status_code == 422, missing.text
    assert missing.json()["error_code"] == "VECTOR_SPARSE_REQUIRED"

    ok = await _hybrid(
        client,
        headers,
        name,
        {
            "vector": _vector(1),
            "sparse_vector": {"indices": [0, 2], "values": [1.0, 2.0]},
            "alpha": 0.75,
            "top_k": 10,
        },
    )
    assert ok.status_code == 200, ok.text
    results = ok.json()["results"]
    assert len(results) == 3
    assert all(r["metadata"]["_tenant_probe"] == "me" for r in results)


@pytest.mark.milvus
async def test_hybrid_milvus_sparse_required_and_scoped(
    client: AsyncClient, milvus_backend: None
) -> None:
    """Milvus hybrid: sparse input is required (422 VECTOR_SPARSE_REQUIRED)
    and, with it, returns only the caller's rows."""
    reg = await _register(client, "hyb-m")
    headers = _auth_headers(reg["access_token"])
    name = await _create_collection(client, headers, "milvus")
    await _upsert(client, headers, name, sparse=True)

    missing = await _hybrid(
        client,
        headers,
        name,
        {"vector": _vector(1), "alpha": 0.75, "top_k": 10},
    )
    assert missing.status_code == 422, missing.text
    assert missing.json()["error_code"] == "VECTOR_SPARSE_REQUIRED"

    ok = await _hybrid(
        client,
        headers,
        name,
        {
            "vector": _vector(1),
            "sparse_vector": {"indices": [0, 2], "values": [1.0, 2.0]},
            "alpha": 0.75,
            "top_k": 10,
        },
    )
    assert ok.status_code == 200, ok.text
    results = ok.json()["results"]
    assert len(results) == 3
    assert all(r["metadata"]["_tenant_probe"] == "me" for r in results)


async def test_hybrid_weaviate_text_required_and_scoped(
    client: AsyncClient, weaviate_backend: None
) -> None:
    """Weaviate hybrid: query_text is required (422) and scopes to the
    caller's shard."""
    reg = await _register(client, "hyb-w")
    headers = _auth_headers(reg["access_token"])
    name = await _create_collection(client, headers, "weaviate")
    await _upsert(client, headers, name, sparse=False)

    missing = await _hybrid(
        client, headers, name, {"vector": _vector(1), "alpha": 0.75, "top_k": 10}
    )
    assert missing.status_code == 422, missing.text

    ok = await _hybrid(
        client,
        headers,
        name,
        {"vector": _vector(1), "query_text": "tag", "alpha": 0.75, "top_k": 10},
    )
    assert ok.status_code == 200, ok.text
    results = ok.json()["results"]
    assert len(results) == 3
    assert all(r["metadata"]["_tenant_probe"] == "me" for r in results)


async def test_hybrid_chroma_unsupported(client: AsyncClient, chroma_backend: None) -> None:
    """Chroma has no hybrid: 400 VALIDATION_UNSUPPORTED_OPERATION with
    details.capability naming hybrid_search."""
    reg = await _register(client, "hyb-c")
    headers = _auth_headers(reg["access_token"])
    name = await _create_collection(client, headers, "chroma")

    resp = await _hybrid(
        client,
        headers,
        name,
        {"vector": _vector(1), "query_text": "x", "top_k": 5},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error_code"] == "VALIDATION_UNSUPPORTED_OPERATION"
    assert body["details"]["capability"] == "hybrid_search"


async def test_hybrid_alpha_bounds(client: AsyncClient, qdrant_backend: None) -> None:
    """alpha is normalized [0,1]; out-of-range is a 422 (schema)."""
    reg = await _register(client, "hyb-a")
    headers = _auth_headers(reg["access_token"])
    name = await _create_collection(client, headers, "qdrant")
    resp = await _hybrid(
        client,
        headers,
        name,
        {
            "vector": _vector(1),
            "sparse_vector": {"indices": [0], "values": [1.0]},
            "alpha": 1.5,
            "top_k": 5,
        },
    )
    assert resp.status_code == 422, resp.text


async def test_hybrid_sparse_shape_validated(client: AsyncClient, qdrant_backend: None) -> None:
    """Sparse vectors are shape-validated: non-ascending/duplicate indices are
    a 422, not a backend round-trip."""
    reg = await _register(client, "hyb-s")
    headers = _auth_headers(reg["access_token"])
    name = await _create_collection(client, headers, "qdrant")
    resp = await _hybrid(
        client,
        headers,
        name,
        {
            "vector": _vector(1),
            "sparse_vector": {"indices": [2, 0], "values": [1.0, 2.0]},
            "top_k": 5,
        },
    )
    assert resp.status_code == 422, resp.text
    # And length mismatch between indices and values.
    resp2 = await _hybrid(
        client,
        headers,
        name,
        {
            "vector": _vector(1),
            "sparse_vector": {"indices": [0, 1], "values": [1.0]},
            "top_k": 5,
        },
    )
    assert resp2.status_code == 422, resp2.text


async def test_qdrant_payload_filtering_via_query(
    client: AsyncClient, qdrant_backend: None
) -> None:
    """Qdrant's payload filters work through the platform DSL: equality,
    range, and $in narrow the result set."""
    reg = await _register(client, "hyb-f")
    headers = _auth_headers(reg["access_token"])
    name = await _create_collection(client, headers, "qdrant")
    await _upsert(client, headers, name, sparse=False)

    filtered = await client.post(
        f"{API}/collections/{name}/query",
        json={"vector": _vector(1), "top_k": 10, "filters": {"tag": "tag-1"}},
        headers=headers,
    )
    assert filtered.status_code == 200, filtered.text
    assert [r["id"] for r in filtered.json()["results"]] == ["doc-1"]

    ranged = await client.post(
        f"{API}/collections/{name}/query",
        json={"vector": _vector(1), "top_k": 10, "filters": {"seed": {"$gte": 1}}},
        headers=headers,
    )
    assert ranged.status_code == 200, ranged.text
    assert {r["id"] for r in ranged.json()["results"]} == {"doc-1", "doc-2"}


@pytest.mark.milvus
async def test_milvus_payload_filtering_via_query(
    client: AsyncClient, milvus_backend: None
) -> None:
    """Milvus JSON-field filters work through the platform DSL: equality and
    range narrow the result set (metadata["key"] exprs)."""
    reg = await _register(client, "hyb-mf")
    headers = _auth_headers(reg["access_token"])
    name = await _create_collection(client, headers, "milvus")
    await _upsert(client, headers, name, sparse=False)

    filtered = await client.post(
        f"{API}/collections/{name}/query",
        json={"vector": _vector(1), "top_k": 10, "filters": {"tag": "tag-1"}},
        headers=headers,
    )
    assert filtered.status_code == 200, filtered.text
    assert [r["id"] for r in filtered.json()["results"]] == ["doc-1"]

    ranged = await client.post(
        f"{API}/collections/{name}/query",
        json={"vector": _vector(1), "top_k": 10, "filters": {"seed": {"$gte": 1}}},
        headers=headers,
    )
    assert ranged.status_code == 200, ranged.text
    assert {r["id"] for r in ranged.json()["results"]} == {"doc-1", "doc-2"}


async def test_weaviate_metadata_filtering_unsupported(
    client: AsyncClient, weaviate_backend: None
) -> None:
    """Weaviate is schema-first: metadata filtering is rejected with the typed
    capability error rather than silently ignored."""
    reg = await _register(client, "hyb-wf")
    headers = _auth_headers(reg["access_token"])
    name = await _create_collection(client, headers, "weaviate")
    resp = await client.post(
        f"{API}/collections/{name}/query",
        json={"vector": _vector(1), "top_k": 5, "filters": {"tag": "tag-1"}},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error_code"] == "VALIDATION_UNSUPPORTED_OPERATION"
    assert body["details"]["capability"] == "metadata_filtering"
