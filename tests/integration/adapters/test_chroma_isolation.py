"""Layer 1 — Chroma adapter isolation suite (real backend via testcontainers).

The security-boundary acceptance gate from the isolation design doc (§3):
shared contract cases C1–C5 plus the per-backend mechanism tests. All data is
**indistinguishable** — identical IDs and identical vectors across tenants,
differing only in the ``_tenant_probe`` payload marker and the tenant the
record was written as — so any unscoped read returns both tenants' rows and a
leak cannot hide behind coincidentally-different data. The fail-closed
contract is asserted behaviorally: error or empty, never cross-tenant rows.

Chroma's native mechanism (``collection-per-tenant``): the physical
collection IS the tenant boundary, so the mechanism tests prove that distinct
physical objects (as the service generates them — ``col_<uuid>``) hold
disjoint data even with identical ids/vectors, and that deleting one physical
collection leaves the other's data intact.
"""

import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from app.adapters.base import VectorRecord
from app.adapters.chroma_adapter import ChromaAdapter
from app.adapters.registry import registry

DIM = 8
# A non-colinear probe vector so cosine distances are meaningful (all-colinear
# seeds are required for the identical-data proofs, but would make ordering
# assertions vacuous).
_QUERY = [1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0]


def _phys() -> str:
    return f"col_{uuid.uuid4().hex}"


def _rec(rid: str, probe: str) -> VectorRecord:
    """Indistinguishable record: identical vector regardless of tenant, with
    a ``_tenant_probe`` marker for visibility."""
    now = datetime.now(UTC)
    return VectorRecord(
        id=rid,
        vector=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
        metadata={"_tenant_probe": probe},
        tenant_id=f"tenant-{probe}",
        created_at=now,
        updated_at=now,
    )


Create = Callable[..., Awaitable[str]]


@pytest.fixture
async def iso(
    chroma_backend: None,
) -> AsyncGenerator[tuple[ChromaAdapter, Create], None]:
    """The container-backed adapter plus a tracking ``create`` helper whose
    created physical collections are cleaned up after each test (the container
    is session-scoped and shared, so tests must never leak objects)."""
    adapter = registry.get("chroma")
    assert isinstance(adapter, ChromaAdapter)
    created: list[str] = []

    async def create(*, dimension: int = DIM, metric: str = "cosine") -> str:
        name = _phys()
        await adapter.create_collection(name=name, dimension=dimension, distance_metric=metric)
        created.append(name)
        return name

    yield adapter, create
    for name in created:
        await adapter.delete_collection(name=name)


# --- C1 — same-ID isolation ---


async def test_c1_same_id_isolation(
    iso: tuple[ChromaAdapter, Any],
) -> None:
    adapter, create = iso
    phys_a, phys_b = await create(), await create()
    await adapter.upsert_vectors(
        collection=phys_a, tenant_id="tenant-A", records=[_rec("doc-1", "A")]
    )
    await adapter.upsert_vectors(
        collection=phys_b, tenant_id="tenant-B", records=[_rec("doc-1", "B")]
    )
    a = await adapter.fetch_vectors(collection=phys_a, tenant_id="tenant-A", ids=["doc-1"])
    b = await adapter.fetch_vectors(collection=phys_b, tenant_id="tenant-B", ids=["doc-1"])
    assert len(a) == len(b) == 1
    assert a[0].id == b[0].id == "doc-1"  # same id, no collision
    assert a[0].metadata["_tenant_probe"] == "A"
    assert b[0].metadata["_tenant_probe"] == "B"
    # The record contract round-trips tenant_id + timestamps.
    assert a[0].tenant_id == "tenant-A"
    assert b[0].tenant_id == "tenant-B"


# --- C2 — query scoping with oversized top_k ---


async def test_c2_query_scoping_oversized_top_k(iso: tuple[ChromaAdapter, Any]) -> None:
    adapter, create = iso
    phys_a, phys_b = await create(), await create()
    for phys, probe in ((phys_a, "A"), (phys_b, "B")):
        await adapter.upsert_vectors(
            collection=phys,
            tenant_id=f"tenant-{probe}",
            records=[_rec(f"doc-{i}", probe) for i in range(5)],
        )
    a = await adapter.query(collection=phys_a, tenant_id="tenant-A", vector=_QUERY, top_k=10)
    b = await adapter.query(collection=phys_b, tenant_id="tenant-B", vector=_QUERY, top_k=10)
    # An unscoped query would return 10 rows; each tenant sees exactly its 5.
    assert len(a) == 5
    assert len(b) == 5
    assert {r.metadata["_tenant_probe"] for r in a} == {"A"}
    assert {r.metadata["_tenant_probe"] for r in b} == {"B"}
    assert {r.id for r in a} == {f"doc-{i}" for i in range(5)}


# --- C3 — delete scoping ---


async def test_c3_delete_scoping(iso: tuple[ChromaAdapter, Any]) -> None:
    adapter, create = iso
    phys_a, phys_b = await create(), await create()
    await adapter.upsert_vectors(
        collection=phys_a, tenant_id="tenant-A", records=[_rec("doc-1", "A"), _rec("doc-2", "A")]
    )
    await adapter.upsert_vectors(
        collection=phys_b, tenant_id="tenant-B", records=[_rec("doc-1", "B"), _rec("doc-2", "B")]
    )
    await adapter.delete_vectors(collection=phys_b, tenant_id="tenant-B", ids=["doc-1"])
    a = await adapter.fetch_vectors(collection=phys_a, tenant_id="tenant-A", ids=["doc-1"])
    b = await adapter.fetch_vectors(collection=phys_b, tenant_id="tenant-B", ids=["doc-1"])
    assert len(a) == 1 and a[0].metadata["_tenant_probe"] == "A"  # A's record intact
    assert len(b) == 0  # B's record gone


# --- C4 — fail-closed unscoped/mis-scoped ---


async def test_c4_fail_closed_misscoped(iso: tuple[ChromaAdapter, Any]) -> None:
    adapter, create = iso
    phys_a, phys_b = await create(), await create()
    await adapter.upsert_vectors(
        collection=phys_a, tenant_id="tenant-A", records=[_rec("doc-1", "A")]
    )
    await adapter.upsert_vectors(
        collection=phys_b, tenant_id="tenant-B", records=[_rec("doc-1", "B")]
    )
    # A foreign/unknown tenant id against A's physical collection: the scope is
    # the physical collection, so the result is A's data — and never B's
    # (fail-closed: no cross-tenant rows under any tenant-id value).
    results = await adapter.query(
        collection=phys_a, tenant_id="totally-unknown-tenant", vector=_QUERY, top_k=10
    )
    assert results and all(r.metadata["_tenant_probe"] == "A" for r in results)
    fetched = await adapter.fetch_vectors(
        collection=phys_a, tenant_id="totally-unknown-tenant", ids=["doc-1"]
    )
    assert len(fetched) == 1 and fetched[0].metadata["_tenant_probe"] == "A"
    # A mis-scoped delete touches only A's physical collection: B's data is
    # never affected.
    await adapter.delete_vectors(
        collection=phys_a, tenant_id="totally-unknown-tenant", ids=["doc-1"]
    )
    b = await adapter.fetch_vectors(collection=phys_b, tenant_id="tenant-B", ids=["doc-1"])
    assert len(b) == 1 and b[0].metadata["_tenant_probe"] == "B"
    a = await adapter.fetch_vectors(collection=phys_a, tenant_id="tenant-A", ids=["doc-1"])
    assert len(a) == 0


# --- C5 — ensure_tenant idempotency ---


async def test_c5_ensure_tenant_idempotent(iso: tuple[ChromaAdapter, Any]) -> None:
    adapter, create = iso
    phys = await create()
    await adapter.ensure_tenant(collection=phys, tenant_id="tenant-A")
    await adapter.ensure_tenant(collection=phys, tenant_id="tenant-A")  # no-op, no error
    assert await adapter.get_collection_info(name=phys) is not None
    assert (await adapter.list_collections()).count(phys) == 1  # no duplicate
    # An unprovisioned tenant (new physical collection) is created lazily.
    fresh = _phys()
    assert await adapter.get_collection_info(name=fresh) is None
    await adapter.ensure_tenant(collection=fresh, tenant_id="tenant-C")
    try:
        assert await adapter.get_collection_info(name=fresh) is not None
    finally:
        await adapter.delete_collection(name=fresh)


# --- Per-backend mechanism tests ---


async def test_mechanism_distinct_physical_collections_hold_disjoint_data(
    iso: tuple[ChromaAdapter, Any],
) -> None:
    """Chroma's native mechanism: (tenant, platform collection) pairs are
    distinct physical objects. Two platform collections with the same
    client-facing name for two tenants resolve to distinct ``col_<uuid>``
    physical names (the service generates them; Layer 2 R2 and e2e E1 prove
    the full resolution chain) — identical ids and vectors never mix, and
    deleting one physical object leaves the other's data intact."""
    adapter, create = iso
    phys_a = await create()  # (tenant A, "products")
    phys_b = await create()  # (tenant B, "products")
    assert phys_a != phys_b
    assert phys_a.startswith("col_") and phys_b.startswith("col_")
    await adapter.upsert_vectors(
        collection=phys_a, tenant_id="tenant-A", records=[_rec("doc-1", "A")]
    )
    await adapter.upsert_vectors(
        collection=phys_b, tenant_id="tenant-B", records=[_rec("doc-1", "B")]
    )
    # Both physical objects exist on the backend.
    assert await adapter.get_collection_info(name=phys_a) is not None
    assert await adapter.get_collection_info(name=phys_b) is not None
    await adapter.delete_collection(name=phys_a)
    # B's physical collection and data survive A's delete.
    assert await adapter.get_collection_info(name=phys_b) is not None
    b = await adapter.fetch_vectors(collection=phys_b, tenant_id="tenant-B", ids=["doc-1"])
    assert len(b) == 1 and b[0].metadata["_tenant_probe"] == "B"
    assert await adapter.get_collection_info(name=phys_a) is None
