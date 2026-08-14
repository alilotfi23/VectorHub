"""Layer 3 — e2e cross-tenant vector isolation (real platform, real Chroma).

From the isolation design doc (§5): the full HTTP surface with two real
principals per backend (JWT + API key, per the two-principal-types
requirement). Every case seeds **indistinguishable data** (same ids, identical
vectors, ``_tenant_probe`` payload markers) so a leak cannot hide behind
coincidentally-different data, and asserts behaviorally (error or empty, never
cross-tenant rows). Cross-tenant 404 probes flow through the shared no-oracle
helpers so the byte-identical contract is enforced by construction.

Cases E5 (async batch) and E6 (hybrid) land with their phases (6 and 4).
"""

from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from httpx import AsyncClient

from tests.e2e.helpers import (
    API,
    AUTH_STYLES,
    _assert_no_existence_oracle,
    _principal_headers,
    _register,
    _unique,
)

DIM = 8


def _vector(seed: int) -> list[float]:
    """Deterministic, non-colinear 8-dim vector from a seed."""
    return [float(((seed + i * 3) % 7) + 1) * 0.1 for i in range(DIM)]


def _record(rid: str, probe: str, seed: int = 0) -> dict[str, Any]:
    return {
        "id": rid,
        "vector": _vector(seed),
        "metadata": {"_tenant_probe": probe, "tag": f"{probe}-{rid}"},
    }


@pytest.fixture
async def principals(client: AsyncClient) -> AsyncGenerator[dict[str, dict[str, Any]], None]:
    reg_a = await _register(client, "iso-a")
    reg_b = await _register(client, "iso-b")
    yield {"a": {"reg": reg_a}, "b": {"reg": reg_b}}


async def _create_collection(
    client: AsyncClient, headers: dict[str, str], name: str, *, dim: int = DIM
) -> dict[str, Any]:
    resp = await client.post(
        f"{API}/collections",
        json={"name": name, "backend": "chroma", "dimension": dim, "distance_metric": "cosine"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return cast(dict[str, Any], resp.json())


async def _upsert(
    client: AsyncClient, headers: dict[str, str], name: str, records: list[dict[str, Any]]
) -> None:
    resp = await client.post(
        f"{API}/collections/{name}/vectors", json={"vectors": records}, headers=headers
    )
    assert resp.status_code == 200, resp.text


async def _query(
    client: AsyncClient, headers: dict[str, str], name: str, seed: int = 0, top_k: int = 10
) -> list[dict[str, Any]]:
    resp = await client.post(
        f"{API}/collections/{name}/query",
        json={"vector": _vector(seed), "top_k": top_k},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return cast(list[dict[str, Any]], resp.json()["results"])


@pytest.mark.parametrize("auth", AUTH_STYLES)
async def test_e1_collection_name_collision(
    client: AsyncClient, principals: dict[str, dict[str, Any]], auth: str
) -> None:
    """A and B both create `products`; identical ids/vectors written by both;
    each sees only its own data on every path (fetch + query with oversized
    top_k — an unscoped query would return 10 rows)."""
    headers_a = await _principal_headers(client, principals["a"]["reg"], auth)
    headers_b = await _principal_headers(client, principals["b"]["reg"], auth)
    await _create_collection(client, headers_a, "products")
    await _create_collection(client, headers_b, "products")
    await _upsert(client, headers_a, "products", [_record(f"doc-{i}", "A") for i in range(5)])
    await _upsert(client, headers_b, "products", [_record(f"doc-{i}", "B") for i in range(5)])

    fetched = await client.get(f"{API}/collections/products/vectors/doc-0", headers=headers_b)
    assert fetched.status_code == 200
    assert fetched.json()["metadata"]["_tenant_probe"] == "B"

    results = await _query(client, headers_b, "products", seed=3, top_k=10)
    assert len(results) == 5
    assert {r["metadata"]["_tenant_probe"] for r in results} == {"B"}
    assert {r["id"] for r in results} == {f"doc-{i}" for i in range(5)}


@pytest.mark.parametrize("auth", AUTH_STYLES)
async def test_e2_cross_tenant_collection_ops(
    client: AsyncClient, principals: dict[str, dict[str, Any]], auth: str
) -> None:
    """B's GET/DELETE/PATCH on A's distinctly-named collection: 404
    COLLECTION_NOT_FOUND, and A's data is untouched and still queryable."""
    headers_a = await _principal_headers(client, principals["a"]["reg"], auth)
    headers_b = await _principal_headers(client, principals["b"]["reg"], auth)
    await _create_collection(client, headers_a, "a-private")
    await _upsert(client, headers_a, "a-private", [_record(f"doc-{i}", "A") for i in range(5)])

    for method, path, body in (
        ("GET", f"{API}/collections/a-private", None),
        ("DELETE", f"{API}/collections/a-private", None),
        ("PATCH", f"{API}/collections/a-private/config", {"index_config": {"m": 8}}),
    ):
        resp = await client.request(method, path, json=body, headers=headers_b)
        assert resp.status_code == 404, f"{method} {path}: {resp.status_code} {resp.text}"
        assert resp.json()["error_code"] == "COLLECTION_NOT_FOUND"

    results = await _query(client, headers_a, "a-private", seed=1)
    assert len(results) == 5  # A's data survived B's attempts


@pytest.mark.parametrize("auth", AUTH_STYLES)
async def test_e3_forged_tenant_id(
    client: AsyncClient, principals: dict[str, dict[str, Any]], auth: str
) -> None:
    """A body carrying tenant_id (the other tenant's, or any value) is rejected
    at the schema (422) — data can never land under a forged tenant."""
    headers_a = await _principal_headers(client, principals["a"]["reg"], auth)
    headers_b = await _principal_headers(client, principals["b"]["reg"], auth)
    other_tenant = principals["b"]["reg"]["user"]["tenant_id"]

    forged_create = await client.post(
        f"{API}/collections",
        json={
            "name": "forged",
            "backend": "chroma",
            "dimension": DIM,
            "distance_metric": "cosine",
            "tenant_id": other_tenant,
        },
        headers=headers_a,
    )
    assert forged_create.status_code == 422, forged_create.text
    # Nothing was created under either tenant.
    listed = await client.get(f"{API}/collections", headers=headers_b)
    assert all(c["name"] != "forged" for c in listed.json()["items"])

    await _create_collection(client, headers_a, "legit")
    forged_upsert = await client.post(
        f"{API}/collections/legit/vectors",
        json={
            "vectors": [
                {"id": "x", "vector": _vector(2), "metadata": {"_tenant_probe": "A"}},
            ],
            "tenant_id": other_tenant,
        },
        headers=headers_a,
    )
    assert forged_upsert.status_code == 422, forged_upsert.text


@pytest.mark.parametrize("auth", AUTH_STYLES)
async def test_e4_vector_id_collision_via_api(
    client: AsyncClient, principals: dict[str, dict[str, Any]], auth: str
) -> None:
    """Both tenants upsert the same id with identical vectors; B's fetch and
    query return only B's payload."""
    headers_a = await _principal_headers(client, principals["a"]["reg"], auth)
    headers_b = await _principal_headers(client, principals["b"]["reg"], auth)
    await _create_collection(client, headers_a, "shared")
    await _create_collection(client, headers_b, "shared")
    await _upsert(client, headers_a, "shared", [_record("doc-1", "A")])
    await _upsert(client, headers_b, "shared", [_record("doc-1", "B")])

    fetched = await client.get(f"{API}/collections/shared/vectors/doc-1", headers=headers_b)
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["metadata"]["_tenant_probe"] == "B"
    # Identical vector (B's copy); chroma stores float32, so compare with
    # approximate equality against the float64 source.
    assert body["vector"] == pytest.approx(_vector(0))

    results = await _query(client, headers_b, "shared", top_k=10)
    assert len(results) == 1
    assert results[0]["metadata"]["_tenant_probe"] == "B"


@pytest.mark.parametrize("auth", AUTH_STYLES)
async def test_e7_tenant_scoped_listing(
    client: AsyncClient, principals: dict[str, dict[str, Any]], auth: str
) -> None:
    """Each tenant's collection listing contains only its own collections —
    even with identical names."""
    headers_a = await _principal_headers(client, principals["a"]["reg"], auth)
    headers_b = await _principal_headers(client, principals["b"]["reg"], auth)
    await _create_collection(client, headers_a, "products")
    await _create_collection(client, headers_b, "products")

    listed_a = await client.get(f"{API}/collections", headers=headers_a)
    listed_b = await client.get(f"{API}/collections", headers=headers_b)
    names_a = {c["name"] for c in listed_a.json()["items"]}
    names_b = {c["name"] for c in listed_b.json()["items"]}
    assert names_a == {"products"}
    assert names_b == {"products"}
    # Distinct physical objects behind the same client-facing name.
    phys_a = {c["backend"] for c in listed_a.json()["items"]}
    assert phys_a == {"chroma"}


@pytest.mark.parametrize("auth", AUTH_STYLES)
async def test_e8_negative_control_no_existence_oracle(
    client: AsyncClient, principals: dict[str, dict[str, Any]], auth: str
) -> None:
    """B has no collections; probing A's collection/vector and a name that
    exists nowhere must produce byte-identical fail-closed 404s — responses
    can't act as an existence oracle."""
    headers_a = await _principal_headers(client, principals["a"]["reg"], auth)
    headers_b = await _principal_headers(client, principals["b"]["reg"], auth)
    await _create_collection(client, headers_a, "secret")
    await _upsert(client, headers_a, "secret", [_record("doc-1", "A")])
    missing = _unique("missing")

    await _assert_no_existence_oracle(
        client,
        method="GET",
        real_path=f"{API}/collections/secret",
        missing_path=f"{API}/collections/{missing}",
        headers=headers_b,
        expected="COLLECTION_NOT_FOUND",
    )
    await _assert_no_existence_oracle(
        client,
        method="GET",
        real_path=f"{API}/collections/secret/vectors/doc-1",
        missing_path=f"{API}/collections/{missing}/vectors/doc-1",
        headers=headers_b,
        expected="COLLECTION_NOT_FOUND",
    )
    await _assert_no_existence_oracle(
        client,
        method="POST",
        real_path=f"{API}/collections/secret/query",
        missing_path=f"{API}/collections/{missing}/query",
        headers=headers_b,
        expected="COLLECTION_NOT_FOUND",
        body={"vector": _vector(1), "top_k": 5},
    )
