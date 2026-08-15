"""Layer 2 — service-layer tenant-routing suite (recording stub, no containers).

From the isolation design doc (§4): a VectorDBAdapter stub records every call
into an in-memory log — ``(operation, physical_name, tenant_id, payload)`` —
while real CollectionService/VectorService instances run against it through
the AdapterRegistry. This layer catches routing bugs in milliseconds,
container-free, and pins the contract the real adapters must honor:

- R1 — the tenant assertion fires *before* any adapter call (stub log empty).
- R2 — physical names resolve only from the principal's registry row
  (``col_<uuid>``), and the principal's tenant is what reaches the adapter.
- R3 — the record envelopes have no ``tenant_id``/owner field (forged ones
  are rejected at the schema; the wire-level half lives in the API suites).
- R4 — batch enqueue scoping (Phase 6): a foreign collection name 404s at
  enqueue before any staging, and an own-collection enqueue stages under
  ``{principal_tenant_id}/{job_id}.jsonl`` — no other tenant can address
  the object, and the worker receives only ``{job_id, payload_key}``.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import (
    BatchResult,
    CapabilityEntry,
    CollectionInfo,
    QueryResult,
    SparseVector,
    VectorDBAdapter,
    VectorRecord,
)
from app.adapters.registry import registry
from app.core.exceptions import AppError, ErrorCode
from app.core.security import Principal
from app.db.models import Tenant, User
from app.schemas.collections import CollectionCreateRequest
from app.schemas.vectors import QueryRequest, VectorRecordIn, VectorUpsertRequest
from app.services.collection_service import CollectionService
from app.services.job_service import JobService
from app.services.vector_service import VectorService


# A full VectorDBAdapter implementation that records calls instead of talking
# to a backend — the contract-pinning double from the isolation design.
class RecordingStubAdapter(VectorDBAdapter):
    backend_name = "stub"
    tenancy_model = "collection-per-tenant"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None, Any]] = []

    def _log(
        self, op: str, *, name: str | None = None, tenant_id: str | None = None, payload: Any = None
    ) -> None:
        self.calls.append((op, name, tenant_id, payload))

    async def health_check(self) -> None:
        return None

    async def create_collection(
        self,
        *,
        name: str,
        dimension: int,
        distance_metric: str,
        extras: dict[str, Any] | None = None,
    ) -> None:
        self._log(
            "create_collection",
            name=name,
            payload={"dimension": dimension, "metric": distance_metric},
        )

    async def delete_collection(self, *, name: str) -> None:
        self._log("delete_collection", name=name)

    async def list_collections(self) -> list[str]:
        return []

    async def get_collection_info(self, *, name: str) -> CollectionInfo | None:
        self._log("get_collection_info", name=name)
        return CollectionInfo(name=name, dimension=8, distance_metric="cosine")

    async def ensure_tenant(self, *, collection: str, tenant_id: str) -> None:
        self._log("ensure_tenant", name=collection, tenant_id=tenant_id)

    async def upsert_vectors(
        self,
        *,
        collection: str,
        tenant_id: str,
        records: list[VectorRecord],
        extras: dict[str, Any] | None = None,
    ) -> None:
        self._log(
            "upsert_vectors", name=collection, tenant_id=tenant_id, payload=[r.id for r in records]
        )

    async def delete_vectors(self, *, collection: str, tenant_id: str, ids: list[str]) -> None:
        self._log("delete_vectors", name=collection, tenant_id=tenant_id, payload=ids)

    async def fetch_vectors(
        self, *, collection: str, tenant_id: str, ids: list[str]
    ) -> list[VectorRecord]:
        self._log("fetch_vectors", name=collection, tenant_id=tenant_id, payload=ids)
        return []

    async def query(
        self,
        *,
        collection: str,
        tenant_id: str,
        vector: list[float],
        top_k: int,
        filters: dict[str, Any] | None = None,
        extras: dict[str, Any] | None = None,
    ) -> list[QueryResult]:
        self._log("query", name=collection, tenant_id=tenant_id, payload={"top_k": top_k})
        return []

    async def batch_upsert(
        self,
        *,
        collection: str,
        tenant_id: str,
        records: list[VectorRecord],
        chunk_size: int,
        extras: dict[str, Any] | None = None,
    ) -> BatchResult:
        self._log("batch_upsert", name=collection, tenant_id=tenant_id, payload=len(records))
        return BatchResult(ok=len(records))

    async def batch_delete(
        self, *, collection: str, tenant_id: str, ids: list[str], chunk_size: int
    ) -> BatchResult:
        self._log("batch_delete", name=collection, tenant_id=tenant_id, payload=ids)
        return BatchResult(ok=len(ids))

    async def create_index(
        self,
        *,
        collection: str,
        index_config: dict[str, Any],
        extras: dict[str, Any] | None = None,
    ) -> None:
        self._log("create_index", name=collection, payload=index_config)

    async def hybrid_search(
        self,
        *,
        collection: str,
        tenant_id: str,
        vector: list[float],
        sparse_vector: SparseVector | None,
        query_text: str | None,
        alpha: float,
        top_k: int,
        filters: dict[str, Any] | None = None,
        extras: dict[str, Any] | None = None,
    ) -> list[QueryResult]:
        raise NotImplementedError("stub has no hybrid support")

    def capability(self) -> CapabilityEntry:
        return CapabilityEntry(
            backend=self.backend_name,
            tenancy_model=self.tenancy_model,
            hybrid_mode=None,
            sparse_required=False,
            filtering=True,
            batch_async=True,
            quantization=False,
            multi_vector=False,
            sparse_vectors=False,
        )


@pytest.fixture
async def stub_backend() -> Any:
    stub = RecordingStubAdapter()
    registry.register("stub", stub)
    yield stub
    registry.unregister("stub")


async def _make_principal(db: AsyncSession, *, role: str = "owner") -> Principal:
    """A fresh tenant + owner user; the principal's tenant_id is the only
    tenant identity that may ever reach an adapter."""
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    db.add(Tenant(id=tenant_id, name=f"tenant-{uuid.uuid4().hex[:8]}"))
    db.add(
        User(
            id=user_id,
            tenant_id=tenant_id,
            email=f"{uuid.uuid4().hex[:10]}@example.com",
            password_hash="x",
            role=role,
        )
    )
    await db.commit()
    return Principal(user_id=user_id, tenant_id=tenant_id, role=role)


async def _create_products(db: AsyncSession, principal: Principal) -> str:
    collection = await CollectionService(db).create_collection(
        principal, name="products", backend="stub", dimension=8, distance_metric="cosine"
    )
    return collection.physical_name


# --- R1 — assertion fires before any adapter call ---


async def test_r1_tenant_assertion_fires_before_adapter_call(
    db: AsyncSession, stub_backend: RecordingStubAdapter
) -> None:
    principal_a = await _make_principal(db)
    principal_b = await _make_principal(db)
    await _create_products(db, principal_a)
    stub_backend.calls.clear()

    # B's ops on A's client-facing name fail tenant-scoped resolution with the
    # no-existence-oracle 404 — before any adapter call.
    for op in (
        lambda: CollectionService(db).delete_collection(principal_b, name="products"),
        lambda: CollectionService(db).get_collection_with_status(principal_b, name="products"),
        lambda: VectorService(db).upsert(
            principal_b,
            name="products",
            records=[VectorRecordIn(id="doc-1", vector=[0.1] * 8)],
        ),
        lambda: VectorService(db).fetch(principal_b, name="products", vector_id="doc-1"),
    ):
        with pytest.raises(AppError) as exc:
            await op()
        assert exc.value.code == ErrorCode.COLLECTION_NOT_FOUND
    assert stub_backend.calls == []  # nothing reached the adapter


# --- R2 — correct physical + tenant resolution ---


async def test_r2_physical_name_and_tenant_resolve_from_principal(
    db: AsyncSession, stub_backend: RecordingStubAdapter
) -> None:
    principal = await _make_principal(db)
    physical_name = await _create_products(db, principal)
    assert physical_name.startswith("col_")
    # The adapter saw the opaque physical name, never the client-facing one.
    create_call = stub_backend.calls[0]
    assert create_call[0] == "create_collection"
    assert create_call[1] == physical_name

    stub_backend.calls.clear()
    await VectorService(db).upsert(
        principal, name="products", records=[VectorRecordIn(id="doc-1", vector=[0.1] * 8)]
    )
    upsert_call = stub_backend.calls[0]
    assert upsert_call[0] == "upsert_vectors"
    assert upsert_call[1] == physical_name  # resolved from the principal's registry row
    assert upsert_call[2] == principal.tenant_id  # the principal's tenant, never a forged value
    assert upsert_call[3] == ["doc-1"]


async def test_r2_same_client_name_distinct_physical_names(
    db: AsyncSession, stub_backend: RecordingStubAdapter
) -> None:
    """Two tenants creating the same client-facing name map to distinct
    col_<uuid> physical objects — the Layer-2 half of e2e E1 (collision-free
    by construction)."""
    principal_a = await _make_principal(db)
    principal_b = await _make_principal(db)
    phys_a = await _create_products(db, principal_a)
    phys_b = await _create_products(db, principal_b)
    assert phys_a != phys_b
    ops = [c[0] for c in stub_backend.calls]
    assert ops == ["create_collection", "ensure_tenant", "create_collection", "ensure_tenant"]
    assert stub_backend.calls[0][1] == phys_a
    assert stub_backend.calls[2][1] == phys_b


# --- R3 — envelopes carry no tenant/owner identity ---


def test_r3_record_envelopes_have_no_tenant_or_owner_field() -> None:
    """The record/collection/query envelopes deliberately have no
    tenant_id/owner_id field — a forged value is rejected at the schema
    (422, extra="forbid"), so the principal's tenant is what reaches the
    adapter. The wire-level proof lives in the API suites and the unit
    schema forge-parametrize."""
    for schema in (CollectionCreateRequest, VectorUpsertRequest, QueryRequest, VectorRecordIn):
        declared = set(schema.model_fields)
        assert "tenant_id" not in declared
        assert "owner_id" not in declared
    # The unit schema suite (test_schemas.py) proves the 422 rejection for
    # every request envelope, including these.


# --- R4 — batch enqueue scoping (Phase 6 async-batch path) ---


class RecordingStorage:
    """Container-free stand-in for BatchStorage — R4's 'stub/JobService
    records the key'. Records staged keys instead of touching MinIO/S3;
    drains the stream so uploads behave like the real path."""

    bucket = "test-bucket"

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.uploads = 0

    async def ensure_bucket(self) -> None:
        return None

    async def upload_stream(self, key: str, chunks: AsyncIterator[bytes]) -> int:
        self.keys.append(key)
        self.uploads += 1
        total = 0
        async for chunk in chunks:
            total += len(chunk)
        return total

    async def delete(self, key: str) -> None:
        return None


async def _ndjson_line(rid: str = "doc-1") -> AsyncIterator[bytes]:
    yield (b'{"id": "' + rid.encode() + b'", "vector": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]}\n')


async def test_r4_batch_enqueue_foreign_collection_404s_before_staging(
    db: AsyncSession, stub_backend: RecordingStubAdapter
) -> None:
    """Enqueuing a batch against another tenant's collection fails at
    enqueue — COLLECTION_NOT_FOUND, the no-oracle 404 — before any byte is
    staged, before any arq handoff, and before any adapter call."""
    principal_a = await _make_principal(db)
    principal_b = await _make_principal(db)
    await _create_products(db, principal_a)
    stub_backend.calls.clear()

    storage = RecordingStorage()
    staged: list[str] = []

    async def fake_enqueue(job_id: str, payload_key_value: str) -> None:
        staged.append(payload_key_value)

    with pytest.raises(AppError) as exc:
        await JobService(db, storage=storage, enqueue=fake_enqueue).create_batch_ingest(
            principal_b, name="products", chunks=_ndjson_line()
        )
    assert exc.value.code == ErrorCode.COLLECTION_NOT_FOUND
    assert storage.keys == []  # nothing staged
    assert staged == []  # nothing enqueued
    assert stub_backend.calls == []  # nothing reached the adapter


async def test_r4_batch_staging_key_is_tenant_scoped(
    db: AsyncSession, stub_backend: RecordingStubAdapter
) -> None:
    """Own-collection enqueue stages at ``{tenant_id}/{job_id}.jsonl`` — the
    principal's tenant prefix, so no other tenant can address the object —
    and the worker receives only ``{job_id, payload_key}``, never the
    payload or any tenant-foreign identity."""
    principal = await _make_principal(db)
    await _create_products(db, principal)

    storage = RecordingStorage()
    handed: list[tuple[str, str]] = []

    async def fake_enqueue(job_id: str, payload_key_value: str) -> None:
        handed.append((job_id, payload_key_value))

    job = await JobService(db, storage=storage, enqueue=fake_enqueue).create_batch_ingest(
        principal, name="products", chunks=_ndjson_line()
    )
    (key,) = storage.keys
    assert key == f"{principal.tenant_id}/{job.id}.jsonl"
    assert handed == [(job.id, key)]  # the arq handoff is {job_id, payload_key} only
    assert job.tenant_id == principal.tenant_id
    assert storage.uploads == 1
