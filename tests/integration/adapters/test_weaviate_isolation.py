"""Layer 1 — Weaviate adapter isolation suite (real backend via testcontainers).

The security-boundary acceptance gate from the isolation design doc (§3):
shared contract cases C1–C6 plus the per-backend mechanism tests. All data is
**indistinguishable** — identical IDs and identical vectors across tenants,
differing only in the ``_tenant_probe`` payload marker — so any unscoped read
returns both tenants' rows and a leak cannot hide behind coincidentally-
different data. The fail-closed contract is asserted behaviorally: error or
empty, never cross-tenant rows.

Weaviate's mechanism (``native-tenant``): the physical class is
tenant-enabled (``multiTenancyConfig.enabled``) and every operation runs
through a tenant-scoped handle (one shard per tenant). The mechanism tests
prove the native boundary: an **unscoped** near_vector on the tenant-enabled
class **errors** (fail-closed), ``tenants.create`` is idempotent, and both
tenants' identical-ID objects live on the same physical class while each
tenant only ever sees its own shard.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from weaviate.exceptions import WeaviateQueryError

from app.adapters.base import VectorRecord
from app.adapters.registry import registry
from app.adapters.weaviate_adapter import WeaviateAdapter

DIM = 8
_QUERY = [1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0]


def _phys() -> str:
    return f"col_{uuid.uuid4().hex}"


def _rec(rid: str, probe: str) -> VectorRecord:
    """Indistinguishable record: identical vector regardless of tenant, with a
    ``_tenant_probe`` marker (and a ``tag`` value so the BM25 side of hybrid
    has text to match — the metadata text property is inverted-indexed)."""
    now = datetime.now(UTC)
    return VectorRecord(
        id=rid,
        vector=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
        metadata={"_tenant_probe": probe, "tag": f"keyword-{probe}"},
        tenant_id=f"tenant-{probe}",
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
async def iso(
    weaviate_backend: None,
) -> AsyncGenerator[tuple[WeaviateAdapter, Any], None]:
    adapter = registry.get("weaviate")
    assert isinstance(adapter, WeaviateAdapter)
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


async def test_c1_same_id_isolation(iso: tuple[WeaviateAdapter, Any]) -> None:
    adapter, create = iso
    phys = await create()
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


async def test_c2_query_scoping_oversized_top_k(iso: tuple[WeaviateAdapter, Any]) -> None:
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


async def test_c3_delete_scoping(iso: tuple[WeaviateAdapter, Any]) -> None:
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


async def test_c4_fail_closed_misscoped(iso: tuple[WeaviateAdapter, Any]) -> None:
    adapter, create = iso
    phys = await create()
    for probe in ("A", "B"):
        await adapter.ensure_tenant(collection=phys, tenant_id=f"tenant-{probe}")
        await adapter.upsert_vectors(
            collection=phys,
            tenant_id=f"tenant-{probe}",
            records=[_rec("doc-1", probe)],
        )
    # An unprovisioned tenant id: the tenant-scoped handle errors (the shard
    # doesn't exist) — fail-closed, never B's rows.
    with pytest.raises(WeaviateQueryError):
        await adapter.query(
            collection=phys, tenant_id="totally-unknown-tenant", vector=_QUERY, top_k=10
        )
    # B's data is untouched by the failed mis-scoped attempt.
    b = await adapter.fetch_vectors(collection=phys, tenant_id="tenant-B", ids=["doc-1"])
    assert len(b) == 1 and b[0].metadata["_tenant_probe"] == "B"


# --- C5 — ensure_tenant idempotency ---


async def test_c5_ensure_tenant_idempotent(iso: tuple[WeaviateAdapter, Any]) -> None:
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


# --- C6 — hybrid scoping (text+vector) ---


async def test_c6_hybrid_scoping(iso: tuple[WeaviateAdapter, Any]) -> None:
    adapter, create = iso
    phys = await create()
    for probe in ("A", "B"):
        await adapter.ensure_tenant(collection=phys, tenant_id=f"tenant-{probe}")
        await adapter.upsert_vectors(
            collection=phys,
            tenant_id=f"tenant-{probe}",
            records=[_rec(f"doc-{i}", probe) for i in range(5)],
        )
    a = await adapter.hybrid_search(
        collection=phys,
        tenant_id="tenant-A",
        vector=_QUERY,
        sparse_vector=None,
        query_text="keyword-A",  # BM25 matches only A's metadata text
        alpha=0.2,
        top_k=10,
    )
    b = await adapter.hybrid_search(
        collection=phys,
        tenant_id="tenant-B",
        vector=_QUERY,
        sparse_vector=None,
        query_text="keyword-B",
        alpha=0.2,
        top_k=10,
    )
    # An unscoped hybrid would return 10 rows; each tenant sees exactly its 5,
    # and the keyword side matches only the caller's own shard.
    assert len(a) == 5
    assert len(b) == 5
    assert {r.metadata["_tenant_probe"] for r in a} == {"A"}
    assert {r.metadata["_tenant_probe"] for r in b} == {"B"}


# --- Per-backend mechanism tests ---


async def test_mechanism_unscoped_query_errors_fail_closed(
    iso: tuple[WeaviateAdapter, Any], weaviate_url: tuple[str, int]
) -> None:
    """The native tenancy boundary: on a tenant-enabled class, a query WITHOUT
    tenant scoping errors (Weaviate refuses to serve a shardless read) — the
    fail-closed property of the mechanism itself, not just the adapter."""
    adapter, create = iso
    phys = await create()
    await adapter.ensure_tenant(collection=phys, tenant_id="tenant-A")
    await adapter.upsert_vectors(
        collection=phys, tenant_id="tenant-A", records=[_rec("doc-1", "A")]
    )
    import weaviate

    url, grpc_port = weaviate_url
    raw = weaviate.use_async_with_local(
        host="localhost", port=int(url.rsplit(":", 1)[1]), grpc_port=grpc_port
    )
    await raw.connect()
    try:
        col = raw.collections.get("Col_" + phys.removeprefix("col_"))
        with pytest.raises(WeaviateQueryError):
            await col.query.near_vector(near_vector=_QUERY, limit=10)
    finally:
        await raw.close()


async def test_mechanism_tenants_create_idempotent(
    iso: tuple[WeaviateAdapter, Any], weaviate_url: tuple[str, int]
) -> None:
    """tenants.create on an existing tenant is a no-op — ensure_tenant can
    call it unconditionally, and the same physical class holds both tenants'
    shards (verified by scoped queries returning disjoint identical data)."""
    adapter, create = iso
    phys = await create()
    import weaviate
    from weaviate.classes.tenants import Tenant

    url, grpc_port = weaviate_url
    raw = weaviate.use_async_with_local(
        host="localhost", port=int(url.rsplit(":", 1)[1]), grpc_port=grpc_port
    )
    await raw.connect()
    try:
        col = raw.collections.get("Col_" + phys.removeprefix("col_"))
        await col.tenants.create([Tenant(name="tenant-A")])
        await col.tenants.create([Tenant(name="tenant-A")])  # no error
        assert "tenant-A" in await col.tenants.get()
        await col.tenants.create([Tenant(name="tenant-B")])
    finally:
        await raw.close()
    await adapter.upsert_vectors(
        collection=phys, tenant_id="tenant-A", records=[_rec("doc-1", "A")]
    )
    await adapter.upsert_vectors(
        collection=phys, tenant_id="tenant-B", records=[_rec("doc-1", "B")]
    )
    a = await adapter.fetch_vectors(collection=phys, tenant_id="tenant-A", ids=["doc-1"])
    b = await adapter.fetch_vectors(collection=phys, tenant_id="tenant-B", ids=["doc-1"])
    assert a[0].metadata["_tenant_probe"] == "A"
    assert b[0].metadata["_tenant_probe"] == "B"


async def test_mechanism_opaque_physical_class_name(
    iso: tuple[WeaviateAdapter, Any],
) -> None:
    """The physical class is named from the opaque col_<uuid> registry name
    (Weaviate requires an uppercase first char -> Col_<uuid>), never the
    platform collection name. The public surface proves it: the adapter's own
    info/list calls resolve the derived name."""
    adapter, create = iso
    phys = await create()
    assert phys.startswith("col_")
    assert await adapter.get_collection_info(name=phys) is not None
    assert phys in await adapter.list_collections()
