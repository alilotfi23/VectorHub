"""Weaviate adapter — native multi-tenancy (CLAUDE.md Tenancy Matrix).

**Tenancy model (``native-tenant``):** the physical class is created with
``multiTenancyConfig.enabled: true`` and every tenant gets its own dedicated
shard; all operations run through a tenant-scoped collection handle
(``collection.with_tenant(...)``). Verified against a live 1.28 server:
queries without tenant scoping on a tenant-enabled class **error** (fail-
closed), and ``tenants.create`` on an existing tenant is a no-op — so
``ensure_tenant`` is get-then-create-missing for belt-and-braces.

**Class naming:** Weaviate class names must match ``^[A-Z][A-Za-z0-9_]*$``,
so the opaque ``col_<uuid>`` physical name is mapped to ``Col_<uuid>`` (a
deterministic, reversible derivation) — the adapter never sees client-facing
names and the registry never sees class names.

**Record-schema exceptions (documented):** Weaviate is schema-first — every
property must be declared, and object-typed properties require declared
nested properties, so an *arbitrary* metadata dict can't be a filterable
object. This adapter therefore stores the full user metadata JSON-serialized
in a ``metadata`` TEXT property (arbitrary keys round-trip; values are
BM25-indexable text), and its CapabilityMatrix row declares ``filtering:
False`` — metadata filters on a weaviate collection raise
``VALIDATION_UNSUPPORTED_OPERATION`` (capability ``metadata_filtering``).
``created_at``/``updated_at``/``tenant_id``/platform ``id`` are stored in
declared ``_vhk_*`` TEXT properties and recovered on read.

**Point ids:** objects get the deterministic ``point_uuid`` (UUID5) for the
(tenant, platform id) pair; the platform id round-trips via ``_vhk_id``.

**Hybrid (``text+vector``):** ``query_text`` drives the BM25 side against
the inverted index (the ``metadata`` text, which carries client-supplied
values); ``alpha`` is passed through natively (0.0 = pure keyword,
1.0 = pure dense).

**Score semantics:** near_vector returns Weaviate **distance** — lower is
more similar (same convention as Chroma).

**Client lifecycle:** one lazy ``WeaviateAsyncClient`` per adapter instance
(``use_async_with_local``; gRPC on 50051 by default), process-lifetime.
"""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlparse

import weaviate
import weaviate.classes.config as wc
import weaviate.classes.tenants as wt
from weaviate.classes.query import MetadataQuery

from app.adapters.base import (
    RESERVED_PREFIX,
    BatchResult,
    CapabilityEntry,
    CollectionInfo,
    QueryResult,
    SparseVector,
    VectorDBAdapter,
    VectorRecord,
    point_uuid,
)
from app.core.config import get_settings
from app.core.exceptions import AppError, ErrorCode

# Platform metric -> Weaviate hnsw VectorDistances enum. euclidean is
# Weaviate's L2_SQUARED (squared L2 distance).
_METRIC_TO_DISTANCE: dict[str, wc.VectorDistances] = {
    "cosine": wc.VectorDistances.COSINE,
    "euclidean": wc.VectorDistances.L2_SQUARED,
    "dot": wc.VectorDistances.DOT,
}
_DISTANCE_TO_METRIC: dict[str, str] = {d.name.lower(): k for k, d in _METRIC_TO_DISTANCE.items()}

_TENANT_PROP = f"{RESERVED_PREFIX}tenant_id"
_ID_PROP = f"{RESERVED_PREFIX}id"
_CREATED_PROP = f"{RESERVED_PREFIX}created_at"
_UPDATED_PROP = f"{RESERVED_PREFIX}updated_at"
_METADATA_PROP = "metadata"

_MUTABLE_CONFIG: frozenset[str] = frozenset()  # index config is creation-time on Weaviate


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _class_name(physical_name: str) -> str:
    """col_<uuid> -> Col_<uuid> (Weaviate class names must start uppercase)."""
    return "Col_" + physical_name.removeprefix("col_")


class WeaviateAdapter(VectorDBAdapter):
    backend_name: ClassVar[str] = "weaviate"
    tenancy_model: ClassVar[str] = "native-tenant"

    def __init__(self, url: str | None = None, grpc_port: int | None = None) -> None:
        """Weaviate needs both the HTTP port and the gRPC port (defaults from
        settings; test fixtures pass the container's mapped gRPC port)."""
        self._url = url or get_settings().weaviate_url
        parsed = urlparse(self._url)
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or 8080
        self._grpc_port = grpc_port or get_settings().weaviate_grpc_port
        self._client: weaviate.WeaviateAsyncClient | None = None

    async def _get_client(self) -> weaviate.WeaviateAsyncClient:
        if self._client is None:
            client = weaviate.use_async_with_local(
                host=self._host, port=self._port, grpc_port=self._grpc_port
            )
            await client.connect()
            self._client = client
        return self._client

    async def _collection(self, physical_name: str) -> Any:
        """Resolve the physical class, or fail loud on drift (a missing class
        behind an existing registry row is a backend/registry inconsistency,
        not a client mistake)."""
        client = await self._get_client()
        name = _class_name(physical_name)
        if not await client.collections.exists(name):
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Physical collection '{physical_name}' does not exist on weaviate "
                "(registry/backend drift?)",
                details={"backend": "weaviate", "physical_name": physical_name},
                status_code=503,
            )
        return client.collections.get(name)

    # --- health ---

    async def health_check(self) -> None:
        client = await self._get_client()
        await client.is_ready()

    # --- collection lifecycle ---

    async def create_collection(
        self,
        *,
        name: str,
        dimension: int,
        distance_metric: str,
        extras: dict[str, Any] | None = None,
    ) -> None:
        distance = _METRIC_TO_DISTANCE.get(distance_metric)
        if distance is None:
            raise AppError(
                ErrorCode.VALIDATION_UNSUPPORTED_OPERATION,
                f"Distance metric '{distance_metric}' is not supported on weaviate",
                details={"backend": "weaviate", "capability": f"distance_metric:{distance_metric}"},
                status_code=400,
            )
        client = await self._get_client()
        await client.collections.create(
            name=_class_name(name),
            vector_config=wc.Configure.Vectors.custom(
                module_name="none",
                vector_index_config=wc.Configure.VectorIndex.hnsw(distance_metric=distance),
            ),
            multi_tenancy_config=wc.Configure.multi_tenancy(enabled=True),
            properties=[
                # metadata round-trips as JSON text (see module docstring —
                # arbitrary dicts can't be filterable object properties here).
                wc.Property(name=_METADATA_PROP, data_type=wc.DataType.TEXT),
                wc.Property(name=_TENANT_PROP, data_type=wc.DataType.TEXT),
                wc.Property(name=_ID_PROP, data_type=wc.DataType.TEXT),
                wc.Property(name=_CREATED_PROP, data_type=wc.DataType.TEXT),
                wc.Property(name=_UPDATED_PROP, data_type=wc.DataType.TEXT),
            ],
        )

    async def delete_collection(self, *, name: str) -> None:
        client = await self._get_client()
        class_name = _class_name(name)
        if not await client.collections.exists(class_name):
            return  # tolerant: nothing to hard-delete
        await client.collections.delete(class_name)

    async def list_collections(self) -> list[str]:
        client = await self._get_client()
        names = await client.collections.list_all(simple=True)
        # Reverse of _class_name: Col_<hex> -> col_<hex> (the platform's
        # opaque physical name).
        return ["col_" + n.removeprefix("Col_") for n in names if n.startswith("Col_")]

    async def get_collection_info(self, *, name: str) -> CollectionInfo | None:
        client = await self._get_client()
        class_name = _class_name(name)
        if not await client.collections.exists(class_name):
            return None
        config = await client.collections.get(class_name).config.get(simple=True)
        metric: str | None = None
        try:
            raw_config: Any = config.vector_config
            if isinstance(raw_config, dict):
                raw_config = raw_config.get("default", raw_config)
            raw_metric = raw_config.vector_index_config.distance_metric
            raw_metric = raw_metric.value if hasattr(raw_metric, "value") else raw_metric
            metric = _DISTANCE_TO_METRIC.get(str(raw_metric).lower())
        except Exception:
            metric = None
        # Weaviate doesn't store dimension in the schema (it derives it from
        # vectors), so dimension is None — the registry row is authoritative.
        return CollectionInfo(name=name, dimension=None, distance_metric=metric)

    async def ensure_tenant(self, *, collection: str, tenant_id: str) -> None:
        """Idempotent tenant provisioning: list existing tenants, create the
        missing one. Weaviate's tenants.create is a no-op for existing names
        (verified live), but get-then-create-missing is the documented pattern
        and version-safe."""
        col = await self._collection(collection)
        existing = await col.tenants.get()
        if tenant_id not in existing:
            await col.tenants.create([wt.Tenant(name=tenant_id)])

    # --- vectors ---

    def _properties(
        self, record: VectorRecord, tenant_id: str, created_at: datetime
    ) -> dict[str, Any]:
        return {
            _METADATA_PROP: _json.dumps(record.metadata, default=str, sort_keys=True),
            _TENANT_PROP: tenant_id,
            _ID_PROP: record.id,
            _CREATED_PROP: _iso(created_at),
            _UPDATED_PROP: _iso(record.updated_at),
        }

    @staticmethod
    def _object_uuid(tenant_id: str, platform_id: str) -> str:
        return point_uuid(tenant_id, platform_id)

    @staticmethod
    def _vector_list(vector: Any) -> list[float]:
        """Normalize the SDK's vector return shapes to floats: a
        _WeaviateVector (``.vector``), a dict of named vectors (take the
        single entry), or a bare list."""
        if vector is None:
            return []
        raw = getattr(vector, "vector", None)
        if raw is None and isinstance(vector, dict):
            raw = next(iter(vector.values()), None)
        if raw is None:
            raw = vector
        if isinstance(raw, (list, tuple)):
            return [float(x) for x in raw]
        if hasattr(raw, "tolist"):  # numpy arrays
            return [float(x) for x in raw.tolist()]
        return []

    def _to_record(self, properties: dict[str, Any], vector: list[float]) -> VectorRecord | None:
        platform_id = properties.get(_ID_PROP)
        if not isinstance(platform_id, str):
            return None
        created = _parse_ts(properties.get(_CREATED_PROP)) or _utcnow()
        raw_metadata = properties.get(_METADATA_PROP)
        try:
            metadata = _json.loads(raw_metadata) if isinstance(raw_metadata, str) else {}
        except ValueError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return VectorRecord(
            id=platform_id,
            vector=vector,
            metadata=metadata,
            tenant_id=str(properties.get(_TENANT_PROP) or ""),
            created_at=created,
            updated_at=_parse_ts(properties.get(_UPDATED_PROP)) or created,
        )

    async def upsert_vectors(
        self,
        *,
        collection: str,
        tenant_id: str,
        records: list[VectorRecord],
        extras: dict[str, Any] | None = None,
    ) -> None:
        col = await self._collection(collection)
        tenant_col = col.with_tenant(tenant_id)
        # Preserve each existing record's created_at across idempotent upserts
        # (only updated_at refreshes): one bulk fetch, then insert the new ids
        # and update the existing ones (Weaviate insert on an existing uuid
        # errors; update is a PATCH that keeps unset fields).
        uuids = [point_uuid(tenant_id, r.id) for r in records]
        existing = await tenant_col.query.fetch_objects_by_ids(uuids)
        # Normalize to strings: the SDK returns _WeaviateUUIDInt objects that
        # don't compare equal to the plain UUID strings point_uuid produces.
        existing_ids = {str(o.uuid) for o in existing.objects}
        for record, uid in zip(records, uuids, strict=True):
            props = self._properties(
                record,
                tenant_id,
                record.created_at,  # existing rows keep their created_at via update
            )
            if uid in existing_ids:
                await tenant_col.data.update(uuid=uid, properties=props, vector=record.vector)
            else:
                await tenant_col.data.insert(properties=props, vector=record.vector, uuid=uid)

    async def delete_vectors(self, *, collection: str, tenant_id: str, ids: list[str]) -> None:
        col = await self._collection(collection)
        tenant_col = col.with_tenant(tenant_id)
        for rid in ids:
            await tenant_col.data.delete_by_id(point_uuid(tenant_id, rid))  # tolerates missing

    async def fetch_vectors(
        self, *, collection: str, tenant_id: str, ids: list[str]
    ) -> list[VectorRecord]:
        col = await self._collection(collection)
        tenant_col = col.with_tenant(tenant_id)
        fetched = await tenant_col.query.fetch_objects_by_ids(
            [point_uuid(tenant_id, rid) for rid in ids], include_vector=True
        )
        records: list[VectorRecord] = []
        for obj in fetched.objects:
            record = self._to_record(dict(obj.properties or {}), self._vector_list(obj.vector))
            if record is not None:
                records.append(record)
        return records

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
        if filters:
            raise AppError(
                ErrorCode.VALIDATION_UNSUPPORTED_OPERATION,
                "Metadata filtering is not supported on weaviate (schema-first backend; "
                "metadata round-trips as JSON text)",
                details={"backend": "weaviate", "capability": "metadata_filtering"},
                status_code=400,
            )
        col = await self._collection(collection)
        tenant_col = col.with_tenant(tenant_id)
        result = await tenant_col.query.near_vector(
            near_vector=vector, limit=top_k, return_metadata=MetadataQuery(distance=True)
        )
        return [
            self._query_result(dict(o.properties or {}), getattr(o.metadata, "distance", None))
            for o in result.objects
        ]

    @staticmethod
    def _query_result(properties: dict[str, Any], distance: Any) -> QueryResult:
        created = _parse_ts(properties.get(_CREATED_PROP)) or _utcnow()
        raw_metadata = properties.get(_METADATA_PROP)
        try:
            metadata = _json.loads(raw_metadata) if isinstance(raw_metadata, str) else {}
        except ValueError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return QueryResult(
            id=str(properties.get(_ID_PROP) or ""),
            score=float(distance) if isinstance(distance, (int, float)) else 0.0,
            metadata=metadata,
            tenant_id=str(properties.get(_TENANT_PROP) or None),
            created_at=_parse_ts(properties.get(_CREATED_PROP)),
            updated_at=_parse_ts(properties.get(_UPDATED_PROP)) or created,
        )

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
        if filters:
            raise AppError(
                ErrorCode.VALIDATION_UNSUPPORTED_OPERATION,
                "Metadata filtering is not supported on weaviate (schema-first backend)",
                details={"backend": "weaviate", "capability": "metadata_filtering"},
                status_code=400,
            )
        col = await self._collection(collection)
        tenant_col = col.with_tenant(tenant_id)
        result = await tenant_col.query.hybrid(
            query=query_text or "",
            vector=vector,
            alpha=alpha,
            limit=top_k,
            return_metadata=MetadataQuery(distance=True),
        )
        return [
            self._query_result(dict(o.properties or {}), getattr(o.metadata, "distance", None))
            for o in result.objects
        ]

    # --- batch ---

    async def batch_upsert(
        self,
        *,
        collection: str,
        tenant_id: str,
        records: list[VectorRecord],
        chunk_size: int,
        extras: dict[str, Any] | None = None,
    ) -> BatchResult:
        # Per-backend sizing contract: Weaviate ~1k records per request (the
        # server batches internally). Chunk = one fetch + insert/update fan-out;
        # a failed chunk aborts — retries are safe (idempotent upserts).
        for start in range(0, len(records), chunk_size):
            await self.upsert_vectors(
                collection=collection,
                tenant_id=tenant_id,
                records=records[start : start + chunk_size],
                extras=extras,
            )
        return BatchResult(ok=len(records))

    async def batch_delete(
        self,
        *,
        collection: str,
        tenant_id: str,
        ids: list[str],
        chunk_size: int,
    ) -> BatchResult:
        for start in range(0, len(ids), chunk_size):
            await self.delete_vectors(
                collection=collection, tenant_id=tenant_id, ids=ids[start : start + chunk_size]
            )
        return BatchResult(ok=len(ids))

    # --- index / config ---

    async def create_index(
        self,
        *,
        collection: str,
        index_config: dict[str, Any],
        extras: dict[str, Any] | None = None,
    ) -> None:
        # Weaviate index config is creation-time only (mutable_config empty),
        # so PATCH /config 409s before reaching here. Resolving the class
        # keeps a drifted registry row loud.
        await self._collection(collection)

    # --- introspection ---

    def capability(self) -> CapabilityEntry:
        return CapabilityEntry(
            backend=self.backend_name,
            tenancy_model=self.tenancy_model,
            hybrid_mode="text+vector",
            sparse_required=False,
            filtering=False,
            batch_async=True,
            quantization=False,
            multi_vector=False,
            sparse_vectors=False,
            mutable_config=_MUTABLE_CONFIG,
            default_batch_chunk_size=1000,  # Weaviate ~1k per request (server-side batching)
            notes=(
                "tenancy: native multi-tenancy (multiTenancyConfig.enabled, one shard per "
                "tenant); unscoped queries on a tenant-enabled class error (fail-closed)",
                "class names are Col_<uuid> (Weaviate requires an uppercase first char); the "
                "registry's col_<uuid> physical name maps 1:1",
                "metadata is schema-first: arbitrary dicts round-trip as JSON text in a "
                "metadata property; metadata FILTERING is unsupported "
                "(capability=metadata_filtering)",
                "hybrid: query_text drives BM25 against the inverted index; alpha passed natively",
                "score = weaviate distance; lower is more similar",
                "delete is immediate at the backend level",
            ),
        )
