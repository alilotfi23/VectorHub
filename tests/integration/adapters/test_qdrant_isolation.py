"""Layer 1 — Qdrant adapter isolation suite (real backend via testcontainers).

The security-boundary acceptance gate from the isolation design doc (§3):
shared contract cases C1–C6 plus the per-backend mechanism tests. All data is
**indistinguishable** — identical IDs and identical dense vectors across
tenants, differing only in the ``_tenant_probe`` payload marker — so any
unscoped read returns both tenants' rows and a leak cannot hide behind
coincidentally-different data. The fail-closed contract is asserted
behaviorally: error or empty, never cross-tenant rows.

Qdrant's mechanism (``payload-partition`` — the native tenant API was removed
from server/SDK; see the adapter docstring): the tenant boundary is the
``_vhk_tenant_id`` keyword payload index with ``is_tenant=true``, and the
adapter ALWAYS applies the tenant filter. The mechanism tests therefore prove
exactly what is load-bearing: (1) the ``is_tenant`` index exists on the
physical collection, (2) a raw *unfiltered* query at the storage level
returns both tenants' identical-ID points — the leak that only the adapter's
always-on filter prevents, and (3) the physical collection name is the opaque
``col_<uuid>`` the service generates. Point ids are deterministic UUID5s, so
``doc-1`` under A and B are distinct backend points.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient

from app.adapters.base import RESERVED_PREFIX, SparseVector, VectorRecord
from app.adapters.qdrant_adapter import QdrantAdapter
from app.adapters.registry import registry

_TENANT_FIELD = f"{RESERVED_PREFIX}tenant_id"

DIM = 8
_QUERY = [1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0]


def _phys() -> str:
    return f"col_{uuid.uuid4().hex}"


def _rec(rid: str, probe: str, *, sparse: bool = False) -> VectorRecord:
    """Indistinguishable record: identical dense vector regardless of tenant,
    with a ``_tenant_probe`` marker. Qdrant hybrids need the sparse side, so
    C6 seeds carry identical sparse vectors too."""
    now = datetime.now(UTC)
    return VectorRecord(
        id=rid,
        vector=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
        sparse_vector=SparseVector(indices=[0, 2], values=[1.0, 2.0]) if sparse else None,
        metadata={"_tenant_probe": probe},
        tenant_id=f"tenant-{probe}",
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
async def iso(
    qdrant_backend: None,
) -> AsyncGenerator[tuple[QdrantAdapter, Any], None]:
    adapter = registry.get("qdrant")
    assert isinstance(adapter, QdrantAdapter)
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


async def test_c1_same_id_isolation(iso: tuple[QdrantAdapter, Any]) -> None:
    adapter, create = iso
    phys = await create()  # one physical collection, two tenants
    await adapter.ensure_tenant(collection=phys, tenant_id="tenant-A")
    await adapter.ensure_tenant(collection=phys, tenant_id="tenant-B")
    await adapter.upsert_vectors(
        collection=phys, tenant_id="tenant-A", records=[_rec("doc-1", "A")]
    )
    await adapter.upsert_vectors(
        collection=phys, tenant_id="tenant-B", records=[_rec("doc-1", "B")]
    )
    a = await adapter.fetch_vectors(collection=phys, tenant_id="tenant-A", ids=["doc-1"])
    b = await adapter.fetch_vectors(collection=phys, tenant_id="tenant-B", ids=["doc-1"])
    assert len(a) == len(b) == 1
    assert a[0].id == b[0].id == "doc-1"  # same platform id, no collision
    assert a[0].metadata["_tenant_probe"] == "A"
    assert b[0].metadata["_tenant_probe"] == "B"
    assert a[0].tenant_id == "tenant-A"
    assert b[0].tenant_id == "tenant-B"


# --- C2 — query scoping with oversized top_k ---


async def test_c2_query_scoping_oversized_top_k(iso: tuple[QdrantAdapter, Any]) -> None:
    adapter, create = iso
    phys = await create()
    for probe in ("A", "B"):
        await adapter.ensure_tenant(collection=phys, tenant_id=f"tenant-{probe}")
        await adapter.upsert_vectors(
            collection=phys,
            tenant_id=f"tenant-{probe}",
            records=[_rec(f"doc-{i}", probe) for i in range(5)],
        )
    a = await adapter.query(collection=phys, tenant_id="tenant-A", vector=_QUERY, top_k=10)
    b = await adapter.query(collection=phys, tenant_id="tenant-B", vector=_QUERY, top_k=10)
    # An unscoped query would return 10 rows; each tenant sees exactly its 5.
    assert len(a) == 5
    assert len(b) == 5
    assert {r.metadata["_tenant_probe"] for r in a} == {"A"}
    assert {r.metadata["_tenant_probe"] for r in b} == {"B"}
    assert {r.id for r in a} == {f"doc-{i}" for i in range(5)}


# --- C3 — delete scoping ---


async def test_c3_delete_scoping(iso: tuple[QdrantAdapter, Any]) -> None:
    adapter, create = iso
    phys = await create()
    for probe in ("A", "B"):
        await adapter.ensure_tenant(collection=phys, tenant_id=f"tenant-{probe}")
        await adapter.upsert_vectors(
            collection=phys,
            tenant_id=f"tenant-{probe}",
            records=[_rec("doc-1", probe), _rec("doc-2", probe)],
        )
    await adapter.delete_vectors(collection=phys, tenant_id="tenant-B", ids=["doc-1"])
    a = await adapter.fetch_vectors(collection=phys, tenant_id="tenant-A", ids=["doc-1"])
    b = await adapter.fetch_vectors(collection=phys, tenant_id="tenant-B", ids=["doc-1"])
    assert len(a) == 1 and a[0].metadata["_tenant_probe"] == "A"  # A's record intact
    assert len(b) == 0  # B's record gone


# --- C4 — fail-closed unscoped/mis-scoped ---


async def test_c4_fail_closed_misscoped(iso: tuple[QdrantAdapter, Any]) -> None:
    adapter, create = iso
    phys = await create()
    for probe in ("A", "B"):
        await adapter.ensure_tenant(collection=phys, tenant_id=f"tenant-{probe}")
        await adapter.upsert_vectors(
            collection=phys,
            tenant_id=f"tenant-{probe}",
            records=[_rec("doc-1", probe)],
        )
    # An unknown tenant id matches nothing (the always-on tenant filter) —
    # fail-closed: empty, never B's rows.
    results = await adapter.query(
        collection=phys, tenant_id="totally-unknown-tenant", vector=_QUERY, top_k=10
    )
    assert results == []
    fetched = await adapter.fetch_vectors(
        collection=phys, tenant_id="totally-unknown-tenant", ids=["doc-1"]
    )
    assert fetched == []
    # A mis-scoped delete (unknown tenant) touches nothing: both tenants'
    # records survive, because the delete's filter scopes by tenant.
    await adapter.delete_vectors(collection=phys, tenant_id="totally-unknown-tenant", ids=["doc-1"])
    a = await adapter.fetch_vectors(collection=phys, tenant_id="tenant-A", ids=["doc-1"])
    b = await adapter.fetch_vectors(collection=phys, tenant_id="tenant-B", ids=["doc-1"])
    assert len(a) == len(b) == 1


# --- C5 — ensure_tenant idempotency ---


async def test_c5_ensure_tenant_idempotent(iso: tuple[QdrantAdapter, Any]) -> None:
    adapter, create = iso
    phys = await create()
    # Qdrant's payload-partition model: the tenant boundary is the collection-
    # level is_tenant index, so ensure_tenant is a no-op — calling it twice
    # must not error, and the collection keeps working.
    await adapter.ensure_tenant(collection=phys, tenant_id="tenant-A")
    await adapter.ensure_tenant(collection=phys, tenant_id="tenant-A")
    assert await adapter.get_collection_info(name=phys) is not None
    await adapter.upsert_vectors(
        collection=phys, tenant_id="tenant-A", records=[_rec("doc-1", "A")]
    )
    results = await adapter.query(collection=phys, tenant_id="tenant-A", vector=_QUERY, top_k=10)
    assert len(results) == 1


# --- C6 — hybrid scoping (sparse+vector) ---


async def test_c6_hybrid_scoping(iso: tuple[QdrantAdapter, Any]) -> None:
    adapter, create = iso
    phys = await create()
    for probe in ("A", "B"):
        await adapter.ensure_tenant(collection=phys, tenant_id=f"tenant-{probe}")
        await adapter.upsert_vectors(
            collection=phys,
            tenant_id=f"tenant-{probe}",
            records=[_rec(f"doc-{i}", probe, sparse=True) for i in range(5)],
        )
    a = await adapter.hybrid_search(
        collection=phys,
        tenant_id="tenant-A",
        vector=_QUERY,
        sparse_vector=SparseVector(indices=[0, 2], values=[1.0, 2.0]),
        query_text=None,
        alpha=0.75,
        top_k=10,
    )
    b = await adapter.hybrid_search(
        collection=phys,
        tenant_id="tenant-B",
        vector=_QUERY,
        sparse_vector=SparseVector(indices=[0, 2], values=[1.0, 2.0]),
        query_text=None,
        alpha=0.75,
        top_k=10,
    )
    # An unscoped hybrid would return 10 rows; each tenant sees exactly its 5.
    assert len(a) == 5
    assert len(b) == 5
    assert {r.metadata["_tenant_probe"] for r in a} == {"A"}
    assert {r.metadata["_tenant_probe"] for r in b} == {"B"}


# --- Per-backend mechanism tests ---


async def test_mechanism_is_tenant_index_and_unfiltered_leak(
    iso: tuple[QdrantAdapter, Any], qdrant_url: str
) -> None:
    """Qdrant's isolation is the payload partition: (1) the physical
    collection carries the ``is_tenant`` keyword index on ``_vhk_tenant_id``,
    and (2) a raw *unfiltered* query returns BOTH tenants' identical-ID
    points — the exact leak the adapter's always-on tenant filter prevents.
    This pins the mechanism's load-bearing property and its honest caveat
    (not a separate-shard boundary)."""
    adapter, create = iso
    phys = await create()
    assert phys.startswith("col_")  # opaque physical name, never the platform name
    for probe in ("A", "B"):
        await adapter.ensure_tenant(collection=phys, tenant_id=f"tenant-{probe}")
        await adapter.upsert_vectors(
            collection=phys,
            tenant_id=f"tenant-{probe}",
            records=[_rec("doc-1", probe)],
        )
    raw = AsyncQdrantClient(url=qdrant_url, timeout=10)
    try:
        info = await raw.get_collection(collection_name=phys)
        schema = info.payload_schema or {}
        index = schema.get(_TENANT_FIELD)
        assert index is not None, "the _vhk_tenant_id payload index must exist"
        assert getattr(index.params, "is_tenant", False) is True
        # The unfiltered query leaks both tenants — the adapter's filter is
        # what closes this.
        leaked = await raw.query_points(
            collection_name=phys,
            query=_QUERY,
            using="dense",
            limit=10,
            with_payload=True,
        )
        probes = {(p.payload or {}).get("_tenant_probe") for p in leaked.points}
        assert probes == {"A", "B"}
    finally:
        await raw.close()


async def test_mechanism_scoped_query_never_crosses_tenants(
    iso: tuple[QdrantAdapter, Any],
) -> None:
    """The tenant filter is applied to query, fetch, delete and hybrid — a
    scoped operation on a collection holding both tenants' identical data
    returns only the caller's rows on every path."""
    adapter, create = iso
    phys = await create()
    for probe in ("A", "B"):
        await adapter.ensure_tenant(collection=phys, tenant_id=f"tenant-{probe}")
        await adapter.upsert_vectors(
            collection=phys,
            tenant_id=f"tenant-{probe}",
            records=[_rec("doc-1", probe, sparse=True)],
        )
    q = await adapter.query(collection=phys, tenant_id="tenant-A", vector=_QUERY, top_k=10)
    assert [r.metadata["_tenant_probe"] for r in q] == ["A"]
    h = await adapter.hybrid_search(
        collection=phys,
        tenant_id="tenant-A",
        vector=_QUERY,
        sparse_vector=SparseVector(indices=[0, 2], values=[1.0, 2.0]),
        query_text=None,
        alpha=0.75,
        top_k=10,
    )
    assert [r.metadata["_tenant_probe"] for r in h] == ["A"]
