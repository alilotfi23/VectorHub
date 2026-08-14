"""Layer 1 — Milvus adapter isolation suite (real backend via testcontainers).

The security-boundary acceptance gate from the isolation design doc (§3):
shared contract cases C1–C6 plus the per-backend mechanism tests. All data is
**indistinguishable** — identical IDs and identical dense/sparse vectors
across tenants, differing only in the ``_tenant_probe`` metadata marker — so
any unscoped read returns both tenants' rows and a leak cannot hide behind
coincidentally-different data. The fail-closed contract is asserted
behaviorally: error or empty, never cross-tenant rows.

Milvus's mechanism (``partition-per-tenant``): inserts route by
``partition_name``; every read prunes to ``partition_names=[tenant_id]``; the
adapter has no unscoped path by construction. **Drift from the design doc,
verified against server 3.0.0:** an unscoped *raw* search does NOT return
nothing — it spans all partitions and returns both tenants' identical-ID
rows. What the mechanism tests therefore prove is exactly what is
load-bearing: (1) the ``_default`` partition is empty after tenant-partition
inserts (data routed to named partitions), (2) a raw unscoped search returns
both tenants' rows — the leak only the adapter's always-on ``partition_names``
scope prevents, (3) a nonexistent partition name errors (fail-closed), and
(4) insert into an uncreated partition errors. Point ids are deterministic
UUID5 VARCHAR primary keys, so ``doc-1`` under A and B are distinct rows.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from pymilvus import AsyncMilvusClient, exceptions

from app.adapters.base import SparseVector, VectorRecord
from app.adapters.milvus_adapter import MilvusAdapter
from app.adapters.registry import registry

DIM = 8
_QUERY = [1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0]


def _phys() -> str:
    return f"col_{uuid.uuid4().hex}"


def _rec(rid: str, probe: str, *, sparse: bool = False) -> VectorRecord:
    """Indistinguishable record: identical dense + sparse vectors regardless
    of tenant, with a ``_tenant_probe`` marker (the only difference)."""
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
    milvus_backend: None,
) -> AsyncGenerator[tuple[MilvusAdapter, Any], None]:
    adapter = registry.get("milvus")
    assert isinstance(adapter, MilvusAdapter)
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


async def test_c1_same_id_isolation(iso: tuple[MilvusAdapter, Any]) -> None:
    adapter, create = iso
    phys = await create()  # one physical collection, one partition per tenant
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


async def test_c2_query_scoping_oversized_top_k(iso: tuple[MilvusAdapter, Any]) -> None:
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
    # An unscoped search would return 10 rows; each tenant sees exactly its 5.
    assert len(a) == 5
    assert len(b) == 5
    assert {r.metadata["_tenant_probe"] for r in a} == {"A"}
    assert {r.metadata["_tenant_probe"] for r in b} == {"B"}
    assert {r.id for r in a} == {f"doc-{i}" for i in range(5)}


# --- C3 — delete scoping ---


async def test_c3_delete_scoping(iso: tuple[MilvusAdapter, Any]) -> None:
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


async def test_c4_fail_closed_misscoped(iso: tuple[MilvusAdapter, Any]) -> None:
    adapter, create = iso
    phys = await create()
    for probe in ("A", "B"):
        await adapter.ensure_tenant(collection=phys, tenant_id=f"tenant-{probe}")
        await adapter.upsert_vectors(
            collection=phys,
            tenant_id=f"tenant-{probe}",
            records=[_rec("doc-1", probe)],
        )
    # An unknown tenant partition doesn't exist — every scoped op errors
    # (fail-closed), never returning B's rows.
    with pytest.raises(exceptions.MilvusException):
        await adapter.query(
            collection=phys, tenant_id="totally-unknown-tenant", vector=_QUERY, top_k=10
        )
    with pytest.raises(exceptions.MilvusException):
        await adapter.fetch_vectors(
            collection=phys, tenant_id="totally-unknown-tenant", ids=["doc-1"]
        )  # A mis-scoped delete (unknown partition) errors — the backend refuses it
    # outright (fail-closed, stronger than an empty-match no-op): nothing is
    # deleted, and both tenants' records survive.
    with pytest.raises(exceptions.MilvusException):
        await adapter.delete_vectors(
            collection=phys, tenant_id="totally-unknown-tenant", ids=["doc-1"]
        )
    a = await adapter.fetch_vectors(collection=phys, tenant_id="tenant-A", ids=["doc-1"])
    b = await adapter.fetch_vectors(collection=phys, tenant_id="tenant-B", ids=["doc-1"])
    assert len(a) == len(b) == 1


# --- C5 — ensure_tenant idempotency ---


async def test_c5_ensure_tenant_idempotent(iso: tuple[MilvusAdapter, Any]) -> None:
    adapter, create = iso
    phys = await create()
    await adapter.ensure_tenant(collection=phys, tenant_id="tenant-A")
    await adapter.ensure_tenant(collection=phys, tenant_id="tenant-A")  # no-op, no error
    assert await adapter.get_collection_info(name=phys) is not None
    await adapter.upsert_vectors(
        collection=phys, tenant_id="tenant-A", records=[_rec("doc-1", "A")]
    )
    results = await adapter.query(collection=phys, tenant_id="tenant-A", vector=_QUERY, top_k=10)
    assert len(results) == 1


# --- C6 — hybrid scoping (sparse+vector) ---


async def test_c6_hybrid_scoping(iso: tuple[MilvusAdapter, Any]) -> None:
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


async def test_mechanism_default_partition_empty_and_unscoped_leak(
    iso: tuple[MilvusAdapter, Any], milvus_url: str
) -> None:
    """Milvus's isolation is partition routing: (1) after tenant-partition
    inserts the ``_default`` partition is EMPTY — the raw proof that inserts
    route to the named tenant partitions; (2) a raw *unscoped* search returns
    BOTH tenants' identical-ID rows — the exact leak the adapter's always-on
    ``partition_names`` scope prevents (the honest caveat, per the drift note
    in the adapter docstring: unscoped search spans all partitions on server
    3.0). Pins the mechanism's load-bearing property and the physical-name
    opacity in one pass."""
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
    raw = AsyncMilvusClient(uri=milvus_url, timeout=10)
    try:
        dflt = await raw.search(
            phys,
            data=[_QUERY],
            anns_field="dense",
            limit=10,
            output_fields=["metadata"],
            partition_names=["_default"],
        )
        assert dflt[0] == [], (
            "the _default partition must be empty (inserts route to named partitions)"
        )
        leaked = await raw.search(
            phys, data=[_QUERY], anns_field="dense", limit=10, output_fields=["metadata"]
        )
        probes = {
            (h.get("entity", {}).get("metadata") or {}).get("_tenant_probe") for h in leaked[0]
        }
        assert probes == {"A", "B"}
    finally:
        await raw.close()


async def test_mechanism_nonexistent_partition_fail_closed(
    iso: tuple[MilvusAdapter, Any], milvus_url: str
) -> None:
    """A raw search/get against a partition that was never created errors
    (code 1100) — fail-closed by the backend itself, so a typo'd or forged
    partition name can never silently fall back to another tenant's rows."""
    adapter, create = iso
    phys = await create()
    await adapter.ensure_tenant(collection=phys, tenant_id="tenant-A")
    await adapter.upsert_vectors(
        collection=phys, tenant_id="tenant-A", records=[_rec("doc-1", "A")]
    )
    raw = AsyncMilvusClient(uri=milvus_url, timeout=10)
    try:
        with pytest.raises(exceptions.MilvusException):
            await raw.search(
                phys, data=[_QUERY], anns_field="dense", limit=10, partition_names=["tenant-NOPE"]
            )
    finally:
        await raw.close()


async def test_mechanism_insert_into_uncreated_partition_errors(
    iso: tuple[MilvusAdapter, Any], milvus_url: str
) -> None:
    """Insert into a partition that was never created errors — the lazy
    provisioning contract (ensure_tenant must run first) is enforced by the
    backend, so a missed ensure_tenant cannot silently co-locate a tenant's
    data in the wrong partition."""
    adapter, create = iso
    phys = await create()
    raw = AsyncMilvusClient(uri=milvus_url, timeout=10)
    try:
        with pytest.raises(exceptions.MilvusException):
            await raw.insert(
                phys,
                data=[{"id": "x", "dense": [0.1] * DIM, "metadata": {}}],
                partition_name="tenant-NOPE",
            )
    finally:
        await raw.close()


async def test_mechanism_scoped_ops_never_cross_tenants(
    iso: tuple[MilvusAdapter, Any],
) -> None:
    """The partition_names scope is applied to query, fetch, delete and hybrid
    — a scoped operation on a collection holding both tenants' identical data
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
