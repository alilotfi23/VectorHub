"""Milvus adapter — partition-per-tenant (CLAUDE.md Tenancy Matrix).

**Tenancy model (``partition-per-tenant``):** one physical collection per
platform collection, with one partition per tenant. Inserts route by
``partition_name``; every read (search/hybrid/get/query) prunes to
``partition_names=[tenant_id]``; deletes scope by ``partition_name``. There is
no unscoped code path through the adapter by construction. ``ensure_tenant``
is an idempotent ``create_partition`` (create if absent, no-op if present).

**Honest caveat (drift from the original design doc, verified against server
3.0.0):** the isolation design doc predicted "search without
``partition_names`` must return nothing" because data would sit in named
partitions with an empty default partition. On Milvus 3.0 that is **false**:
an unscoped search spans *all* partitions and returns every tenant's
indistinguishable points (probed live — the ``_default`` partition is empty,
proving inserts route, but a search with no ``partition_names`` scans the
named partitions too). The adapter therefore **always** passes
``partition_names=[tenant_id]`` — the same always-scoped discipline Qdrant
needs — and the mechanism tests prove it: scoped search returns only the
tenant's rows, ``partition_names=["_default"]`` returns nothing, and a
nonexistent partition name errors (code 1100, fail-closed).

**Point ids:** Milvus primary keys are unique per physical collection, so
each (tenant, platform id) pair maps to a deterministic ``point_uuid`` (UUID5)
VARCHAR primary key; the platform id is stored in the metadata JSON under
``_vhk_id`` and recovered on read. Two tenants' ``doc-1`` are distinct rows by
construction.

**Record timestamps:** Milvus has no native per-record ``created_at``/
``updated_at``, so like ``ChromaAdapter`` this adapter folds them (and
``tenant_id``/``_vhk_id``) into the ``metadata`` JSON field under reserved
``_vhk_*`` keys — never dropped (documented exception per the ABC).

**Hybrid (``sparse+vector``):** the collection carries a ``dense``
FLOAT_VECTOR field and a ``sparse`` SPARSE_FLOAT_VECTOR field (no migration
needed). Hybrid is two ``AnnSearchRequest``s (dense + sparse) fused by
``WeightedRanker(alpha, 1-alpha)`` — the normalized platform ``alpha`` maps
directly onto Milvus's weighted ranker (RRFRanker is available but ignores
alpha; the platform contract is alpha-faithful). ``partition_names`` scopes
the whole fusion.

**Consistency:** collections are created with ``Strong`` consistency by
default — the platform's hard-delete contract (a delete must be immediately
visible, per the NFR "deletes are destructive and immediate") and the
isolation suite's delete-then-fetch determinism depend on it; on a single-node
standalone there is no latency cost vs Bounded (no replicas to sync).
Overridable per collection via ``extras={"consistency_level": ...}`` (e.g.
``Bounded`` for multi-replica deployments where read scaling matters more).

**pymilvus async note (CLAUDE.md):** pymilvus 3.0.1 ships a first-class async
client (``AsyncMilvusClient``) — every method here is natively ``async def``,
no thread wrapping needed. This supersedes the earlier concern that the SDK
might still be sync-only.

**Index/config:** dense = HNSW (metric per the platform's distance metric),
sparse = ``SPARSE_INVERTED_INDEX`` (IP). Milvus can hot-alter HNSW params on a
live collection (index rebuild happens server-side in the background), so the
``mutable_config`` subset is ``{m, ef_construction, ef}`` (translated to the
native ``M``/``efConstruction``/``ef``); a metric change is not in the subset
(PATCH /config 409s with REQUIRES_REINDEX before reaching the adapter).

**Client lifecycle:** one lazy ``AsyncMilvusClient`` per adapter instance,
process-lifetime (singleton-per-backend contract). Constructed lazily so
registration never connects.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar, cast

from pymilvus import (
    AnnSearchRequest,
    AsyncMilvusClient,
    DataType,
    WeightedRanker,
    exceptions,
)

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

# Platform metric -> Milvus metric type.
_METRIC_TO_MILVUS: dict[str, str] = {
    "cosine": "COSINE",
    "euclidean": "L2",
    "dot": "IP",
}
_MILVUS_TO_METRIC: dict[str, str] = {v: k for k, v in _METRIC_TO_MILVUS.items()}

_ID_FIELD = f"{RESERVED_PREFIX}id"
_TENANT_FIELD = f"{RESERVED_PREFIX}tenant_id"
_CREATED_FIELD = f"{RESERVED_PREFIX}created_at"
_UPDATED_FIELD = f"{RESERVED_PREFIX}updated_at"

# Milvus's fixed default partition name — never used for tenant data; probing
# it proves inserts route to the named tenant partitions.
_DEFAULT_PARTITION = "_default"

# HNSW params Milvus can hot-alter on a live collection (server rebuilds the
# index in the background) — the PATCH /config mutable subset. Platform keys
# map to Milvus's native param names in create_index.
_MUTABLE_CONFIG = frozenset({"m", "ef_construction", "ef"})

_ID_MAX_LENGTH = 64  # VARCHAR primary key for point_uuid (UUID5 = 36 chars)


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


def _esc_str(value: str) -> str:
    """Escape a string literal for a Milvus filter expression (double-quoted
    strings; backslash and the quote itself must be escaped)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class _FilterTranslator:
    """Platform filter DSL (app.schemas.vectors.validate_filter shape) ->
    a Milvus boolean expression string over the ``metadata`` JSON field.

    Keys access the JSON field literally (``metadata["key"]``) — the v1
    subset filters on top-level metadata keys (the same flat semantics as
    Chroma's where). String operands are double-quoted; numbers/bools bare.
    $and/$or/$not compose with parentheses; $contains maps to
    ``json_contains`` (array membership / string substring on the stored
    value).
    """

    _OPS = {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$contains", "$not_contains"}

    @classmethod
    def translate(cls, filters: dict[str, Any] | None) -> str:
        if not filters:
            return ""
        return cls._node(filters)

    @classmethod
    def _node(cls, node: dict[str, Any]) -> str:
        parts: list[str] = []
        for key, value in node.items():
            if key == "$and":
                parts.append("(" + " and ".join(cls._node(sub) for sub in value) + ")")
            elif key == "$or":
                parts.append("(" + " or ".join(cls._node(sub) for sub in value) + ")")
            elif key == "$not":
                parts.append(f"not ({cls._node(value)})")
            else:
                parts.append(cls._field(key, value))
        return " and ".join(parts)

    @classmethod
    def _literal(cls, value: Any) -> str:
        if isinstance(value, str):
            return f'"{_esc_str(value)}"'
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    @classmethod
    def _list(cls, value: Any) -> str:
        return "[" + ", ".join(cls._literal(v) for v in value) + "]"

    @classmethod
    def _field(cls, key: str, value: Any) -> str:
        path = f'metadata["{_esc_str(key)}"]'
        if not isinstance(value, dict):
            return f"{path} == {cls._literal(value)}"
        parts: list[str] = []
        for op, operand in value.items():
            if op not in cls._OPS:  # schema validates ops; defensive here
                raise AppError(
                    ErrorCode.VALIDATION_INVALID_FILTER,
                    f"Unsupported filter operator '{op}' on field '{key}'",
                    details={"backend": "milvus", "field": key, "operator": op},
                    status_code=422,
                )
            if op == "$eq":
                parts.append(f"{path} == {cls._literal(operand)}")
            elif op == "$ne":
                parts.append(f"{path} != {cls._literal(operand)}")
            elif op == "$gt":
                parts.append(f"{path} > {cls._literal(operand)}")
            elif op == "$gte":
                parts.append(f"{path} >= {cls._literal(operand)}")
            elif op == "$lt":
                parts.append(f"{path} < {cls._literal(operand)}")
            elif op == "$lte":
                parts.append(f"{path} <= {cls._literal(operand)}")
            elif op == "$in":
                parts.append(f"{path} in {cls._list(operand)}")
            elif op == "$nin":
                parts.append(f"{path} not in {cls._list(operand)}")
            elif op == "$contains":
                parts.append(f"json_contains({path}, {cls._literal(operand)})")
            elif op == "$not_contains":
                parts.append(f"not json_contains({path}, {cls._literal(operand)})")
        if not parts:
            raise AppError(
                ErrorCode.VALIDATION_INVALID_FILTER,
                f"Empty filter on field '{key}'",
                details={"backend": "milvus", "field": key},
                status_code=422,
            )
        return " and ".join(parts)


class MilvusAdapter(VectorDBAdapter):
    backend_name: ClassVar[str] = "milvus"
    tenancy_model: ClassVar[str] = "partition-per-tenant"

    def __init__(self, url: str | None = None) -> None:
        self._url = url or get_settings().milvus_url
        self._client: AsyncMilvusClient | None = None
        # The dense metric each physical collection was created with (the
        # hybrid dense AnnSearchRequest must declare it explicitly; the plain
        # search path uses the collection's index metric implicitly).
        self._metric_by_collection: dict[str, str] = {}

    async def _get_client(self) -> AsyncMilvusClient:
        if self._client is None:
            self._client = AsyncMilvusClient(uri=self._url, timeout=30)
        return self._client

    # --- health ---

    async def health_check(self) -> None:
        await (await self._get_client()).list_collections()

    # --- collection lifecycle ---

    async def create_collection(
        self,
        *,
        name: str,
        dimension: int,
        distance_metric: str,
        extras: dict[str, Any] | None = None,
    ) -> None:
        metric = _METRIC_TO_MILVUS.get(distance_metric)
        if metric is None:
            raise AppError(
                ErrorCode.VALIDATION_UNSUPPORTED_OPERATION,
                f"Distance metric '{distance_metric}' is not supported on milvus",
                details={"backend": "milvus", "capability": f"distance_metric:{distance_metric}"},
                status_code=400,
            )
        extras = extras or {}
        client = await self._get_client()
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(
            field_name="id", datatype=DataType.VARCHAR, is_primary=True, max_length=_ID_MAX_LENGTH
        )
        schema.add_field(field_name="dense", datatype=DataType.FLOAT_VECTOR, dim=dimension)
        schema.add_field(field_name="sparse", datatype=DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field(field_name="metadata", datatype=DataType.JSON)

        index_params = client.prepare_index_params()
        hnsw = dict(extras.get("hnsw") or {})
        index_params.add_index(
            field_name="dense",
            index_type="HNSW",
            metric_type=metric,
            params={"M": hnsw.get("m", 16), "efConstruction": hnsw.get("ef_construction", 200)},
        )
        index_params.add_index(
            field_name="sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="IP"
        )
        self._metric_by_collection[name] = metric
        await client.create_collection(
            collection_name=name,
            schema=schema,
            index_params=index_params,
            consistency_level=extras.get("consistency_level", "Strong"),
        )
        # Searches require a loaded collection; loading an empty collection is
        # cheap and makes the read path work immediately after creation.
        await client.load_collection(name)

    async def delete_collection(self, *, name: str) -> None:
        client = await self._get_client()
        if not await client.has_collection(name):
            return  # tolerant: nothing to hard-delete
        await client.drop_collection(name)

    async def list_collections(self) -> list[str]:
        result = await (await self._get_client()).list_collections()
        return cast(list[str], result)

    async def get_collection_info(self, *, name: str) -> CollectionInfo | None:
        client = await self._get_client()
        if not await client.has_collection(name):
            return None
        desc = await client.describe_collection(name)
        dimension: int | None = None
        for field in desc.get("fields", []):
            if field.get("name") == "dense":
                dimension = int((field.get("params") or {}).get("dim", 0)) or None
        return CollectionInfo(name=name, dimension=dimension, distance_metric=None)

    async def ensure_tenant(self, *, collection: str, tenant_id: str) -> None:
        """Idempotent create_partition: create if absent, no-op if present
        (the lazy, idempotent provisioning contract). The partition is the
        tenant boundary — every insert routes by partition_name and every read
        prunes to partition_names=[tenant_id]."""
        client = await self._get_client()
        if not await client.has_partition(collection, tenant_id):
            await client.create_partition(collection, tenant_id)

    # --- vectors ---

    @staticmethod
    def _metadata(record: VectorRecord, tenant_id: str, created_at: datetime) -> dict[str, Any]:
        metadata = {k: v for k, v in record.metadata.items()}
        metadata[_ID_FIELD] = record.id
        metadata[_TENANT_FIELD] = tenant_id
        metadata[_CREATED_FIELD] = _iso(created_at)
        metadata[_UPDATED_FIELD] = _iso(record.updated_at)
        return metadata

    @staticmethod
    def _row(record: VectorRecord, tenant_id: str, created_at: datetime) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": point_uuid(tenant_id, record.id),
            "dense": [float(x) for x in record.vector],
            "metadata": MilvusAdapter._metadata(record, tenant_id, created_at),
        }
        # The sparse field must always be present in the row: Milvus rejects a
        # missing field (and nullable sparse didn't survive the schema
        # round-trip on 3.0). An empty sparse vector has no terms and never
        # matches a sparse ANN, so records without a sparse side are simply
        # absent from hybrid's sparse leg.
        if record.sparse_vector is not None:
            row["sparse"] = {
                idx: val
                for idx, val in zip(
                    record.sparse_vector.indices, record.sparse_vector.values, strict=True
                )
            }
        else:
            row["sparse"] = {}
        return row

    def _to_record(self, row: dict[str, Any]) -> VectorRecord | None:
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            return None
        platform_id = metadata.get(_ID_FIELD)
        if not isinstance(platform_id, str):
            return None  # a row not written by the platform — not ours to return
        dense = row.get("dense")
        if not isinstance(dense, list):
            return None
        created = _parse_ts(metadata.get(_CREATED_FIELD)) or _utcnow()
        sparse = row.get("sparse")
        sparse_vector: SparseVector | None = None
        if isinstance(sparse, dict) and sparse:
            # Milvus returns sparse vectors as {index: value} with int keys.
            indices = sorted(int(k) for k in sparse)
            sparse_vector = SparseVector(
                indices=indices, values=[float(sparse[i]) for i in indices]
            )
        return VectorRecord(
            id=platform_id,
            vector=[float(x) for x in dense],
            metadata={k: v for k, v in metadata.items() if not k.startswith(RESERVED_PREFIX)},
            sparse_vector=sparse_vector,
            tenant_id=str(metadata.get(_TENANT_FIELD) or ""),
            created_at=created,
            updated_at=_parse_ts(metadata.get(_UPDATED_FIELD)) or created,
        )

    async def upsert_vectors(
        self,
        *,
        collection: str,
        tenant_id: str,
        records: list[VectorRecord],
        extras: dict[str, Any] | None = None,
    ) -> None:
        client = await self._get_client()
        # Preserve each existing record's created_at across idempotent upserts
        # (only updated_at refreshes): fetch the existing rows by the
        # deterministic point ids within the tenant partition, then merge.
        ids = [point_uuid(tenant_id, r.id) for r in records]
        existing = await client.get(
            collection, ids=ids, output_fields=["metadata"], partition_names=[tenant_id]
        )
        created_by_platform_id = {
            (row.get("metadata") or {}).get(_ID_FIELD): (row.get("metadata") or {}).get(
                _CREATED_FIELD
            )
            for row in existing
            if (row.get("metadata") or {}).get(_ID_FIELD)
        }
        rows = [
            self._row(
                r,
                tenant_id,
                _parse_ts(created_by_platform_id.get(r.id)) or r.created_at,
            )
            for r in records
        ]
        await client.upsert(collection, data=rows, partition_name=tenant_id)

    async def delete_vectors(self, *, collection: str, tenant_id: str, ids: list[str]) -> None:
        client = await self._get_client()
        point_ids = [point_uuid(tenant_id, rid) for rid in ids]
        # Missing ids simply delete nothing (idempotent); the partition_name
        # scopes the delete to the tenant's rows by construction.
        try:
            await client.delete(collection, ids=point_ids, partition_name=tenant_id)
        except exceptions.MilvusException:
            raise
        except ValueError as exc:
            # pymilvus 3.0.1 bug: a failed mutation (e.g. partition not
            # found — the mis-scoped fail-closed case) crashes in the
            # response _pack with int('') on an empty report_value before the
            # MilvusException is constructed. Normalize to the typed error so
            # callers see a consistent MilvusException, never a raw ValueError.
            raise exceptions.MilvusException(
                code=1100, message=f"delete rejected by milvus: {exc}"
            ) from exc

    async def fetch_vectors(
        self, *, collection: str, tenant_id: str, ids: list[str]
    ) -> list[VectorRecord]:
        client = await self._get_client()
        point_ids = [point_uuid(tenant_id, rid) for rid in ids]
        fetched = await client.get(
            collection,
            ids=point_ids,
            output_fields=["dense", "sparse", "metadata"],
            partition_names=[tenant_id],
        )
        by_id = {row.get("id"): row for row in fetched}
        records: list[VectorRecord] = []
        for point_id in point_ids:  # requested order
            row = by_id.get(point_id)
            if row is None:
                continue
            record = self._to_record(row)
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
        client = await self._get_client()
        expr = _FilterTranslator.translate(filters)
        try:
            hits = await client.search(
                collection,
                data=[vector],
                anns_field="dense",
                limit=top_k,
                filter=expr or None,
                output_fields=["metadata"],
                partition_names=[tenant_id],
            )
        except exceptions.MilvusException as exc:
            self._raise_or_wrap(exc, filters)
        return [self._query_result(h) for h in hits[0]]

    @classmethod
    def _query_result(cls, hit: dict[str, Any]) -> QueryResult:
        metadata = hit.get("entity", {}).get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        created = _parse_ts(metadata.get(_CREATED_FIELD)) or _utcnow()
        return QueryResult(
            id=str(metadata.get(_ID_FIELD) or ""),
            score=float(hit.get("distance", 0.0)),
            metadata={k: v for k, v in metadata.items() if not k.startswith(RESERVED_PREFIX)},
            tenant_id=str(metadata.get(_TENANT_FIELD) or None),
            created_at=_parse_ts(metadata.get(_CREATED_FIELD)),
            updated_at=_parse_ts(metadata.get(_UPDATED_FIELD)) or created,
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
        if sparse_vector is None:  # the service enforces this; defensive here
            raise AppError(
                ErrorCode.VECTOR_SPARSE_REQUIRED,
                "Hybrid search on milvus requires a sparse_vector",
                details={"backend": "milvus", "capability": "sparse_vector"},
                status_code=422,
            )
        client = await self._get_client()
        expr = _FilterTranslator.translate(filters)
        sparse_data: dict[int, float] = {
            idx: val for idx, val in zip(sparse_vector.indices, sparse_vector.values, strict=True)
        }
        reqs = [
            AnnSearchRequest(
                data=[vector],
                anns_field="dense",
                param={
                    "metric_type": self._metric_by_collection.get(collection, "COSINE"),
                    "params": {},
                },
                limit=top_k,
                filter=expr or None,
            ),
            AnnSearchRequest(
                data=[sparse_data],
                anns_field="sparse",
                param={"metric_type": "IP", "params": {}},
                limit=top_k,
                filter=expr or None,
            ),
        ]
        try:
            hits = await client.hybrid_search(
                collection,
                reqs=reqs,
                ranker=WeightedRanker(alpha, 1.0 - alpha),
                limit=top_k,
                output_fields=["metadata"],
                partition_names=[tenant_id],
            )
        except exceptions.MilvusException as exc:
            self._raise_or_wrap(exc, filters)
        return [self._query_result(h) for h in hits[0]]

    @staticmethod
    def _raise_or_wrap(exc: exceptions.MilvusException, filters: dict[str, Any] | None) -> None:
        """Distinguish a mis-scoped tenant from a bad filter. A partition-name
        error (code 1100) is a fail-closed tenant condition — propagate it as
        the backend error (the service layer maps to the 5xx path; an unknown
        tenant is an internal routing/provisioning bug, never a client-supplied
        value). Everything else is a metadata-filter rejection -> 422
        VALIDATION_INVALID_FILTER."""
        if "partition name" in str(exc):
            raise exc
        raise AppError(
            ErrorCode.VALIDATION_INVALID_FILTER,
            f"Metadata filter rejected by milvus: {exc}",
            details={"backend": "milvus", "filters": filters},
            status_code=422,
        ) from exc

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
        # Per-backend sizing contract: Milvus 1–10k records per request. Each
        # chunk is one created_at-merge get + one upsert; a failed chunk
        # aborts — retries are safe because upserts are idempotent.
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
        # Milvus hot-alters HNSW params on a live collection (the server
        # rebuilds the index in the background); anything outside the mutable
        # subset 409s at PATCH /config before reaching here. Platform keys map
        # to Milvus's native param names.
        client = await self._get_client()
        params: dict[str, Any] = {}
        if "m" in index_config:
            params["M"] = index_config["m"]
        if "ef_construction" in index_config:
            params["efConstruction"] = index_config["ef_construction"]
        if "ef" in index_config:
            params["ef"] = index_config["ef"]
        if not params:
            return
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="dense", index_type="HNSW", metric_type="COSINE", params=params
        )
        await client.create_index(collection, index_params)

    # --- introspection ---

    def capability(self) -> CapabilityEntry:
        return CapabilityEntry(
            backend=self.backend_name,
            tenancy_model=self.tenancy_model,
            hybrid_mode="sparse+vector",
            sparse_required=True,
            filtering=True,
            batch_async=True,
            quantization=False,
            multi_vector=False,
            sparse_vectors=True,
            mutable_config=_MUTABLE_CONFIG,
            default_batch_chunk_size=5000,  # Milvus 1–10k per request
            notes=(
                "tenancy: partition-per-tenant — inserts route by partition_name, every read "
                "prunes to partition_names=[tenant_id] (no unscoped path by construction). "
                "Drift from the design doc, verified on server 3.0.0: an unscoped search spans "
                "all partitions (the _default partition is empty, proving inserts route, but "
                "unscoped search scans the named partitions too) — the always-applied "
                "partition_names scope is load-bearing, and a nonexistent partition name "
                "errors (fail-closed).",
                "point ids are deterministic UUID5(tenant:platform_id) VARCHAR primary keys; "
                "platform ids round-trip via _vhk_id",
                "hybrid: dense+sparse AnnSearchRequests fused with WeightedRanker(alpha, "
                "1-alpha) — the normalized platform alpha maps directly onto Milvus's weighted "
                "ranker (RRFRanker ignores alpha, so it is not used here)",
                "score = milvus cosine similarity (higher is more similar)",
                "no native per-record timestamps — created_at/updated_at fold into the "
                "metadata JSON under reserved _vhk_* keys (same exception as Chroma)",
                "metadata filtering via Milvus JSON exprs on top-level keys "
                '(metadata["key"] ...); json_contains for $contains',
                "collections default to Strong consistency (immediate delete visibility, per "
                "the hard-delete contract); overridable via extras at creation",
                "delete is immediate at the backend level",
            ),
        )
