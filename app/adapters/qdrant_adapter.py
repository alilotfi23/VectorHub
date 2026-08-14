"""Qdrant adapter — payload-partition tenancy (CLAUDE.md Tenancy Matrix, 2026 update).

**Tenancy model (``payload-partition``):** Qdrant's native collection
multi-tenancy API (``multi_tenancy_config`` + ``create_tenant``, the
mechanism CLAUDE.md's original matrix specified at gate ≥1.10) was **removed
from the server and SDK** — qdrant-client 1.19.0 has no ``MultiTenancyConfig``
or tenant methods, and the v1.19.0 server returns 404 for
``/collections/{name}/tenants``. The current canonical mechanism (Qdrant's
own multitenancy docs) is **payload partitioning**: every point carries a
tenant field with a ``keyword`` index marked ``is_tenant=True``, which tells
Qdrant to co-locate each tenant's vectors in storage. This adapter therefore
isolates by **always** applying a ``_vhk_tenant_id == <tenant>`` filter to
every read, write, and delete — there is no unscoped code path through the
adapter. Honest caveat, documented in the capability notes and CLAUDE.md:
unlike Weaviate's per-tenant shards, a raw Qdrant query *without* the tenant
filter returns every tenant's points (verified empirically), so Qdrant
isolation rests on the platform always scoping, backed by Qdrant's
``is_tenant`` storage organization — it is not a separate-shard boundary.

**Point ids:** Qdrant point ids must be unsigned ints or UUIDs and are
global to the physical collection, so this adapter maps every (tenant,
platform id) pair to a deterministic ``point_uuid`` (UUID5) and stores the
platform id in the payload under ``_vhk_id`` (recovered on read). Two
tenants' ``doc-1`` are distinct backend points by construction.

**Hybrid (``sparse+vector``):** collections are created with named vectors
``dense`` + ``sparse`` so hybrid needs no migration. The dense/sparse
prefetch + RRF fusion honors ``alpha`` via per-prefetch weights
(dense = alpha, sparse = 1-alpha). The typed client 1.19.0 dropped the
``weight`` field from ``Prefetch`` (``extra_forbidden``) even though the
server supports it, so the hybrid call goes over one raw ``httpx`` REST
request — the only code path that bypasses the typed client, documented
here so it isn't "fixed" back into a silently weight-less call.

**Score semantics:** Qdrant returns cosine **similarity** (higher is more
similar) for the COSINE distance; scores are backend-native per the ABC
docstring.

**Client lifecycle:** one lazy ``AsyncQdrantClient`` + one lazy
``httpx.AsyncClient`` per adapter instance, process-lifetime
(singleton-per-backend contract).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar, cast
from urllib.parse import urlparse

import httpx
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

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

# Platform metric -> qdrant Distance.
_METRIC_TO_DISTANCE: dict[str, models.Distance] = {
    "cosine": models.Distance.COSINE,
    "euclidean": models.Distance.EUCLID,
    "dot": models.Distance.DOT,
}
_DISTANCE_TO_METRIC: dict[str, str] = {d.value: k for k, d in _METRIC_TO_DISTANCE.items()}

_TENANT_FIELD = f"{RESERVED_PREFIX}tenant_id"
_ID_FIELD = f"{RESERVED_PREFIX}id"
_CREATED_FIELD = f"{RESERVED_PREFIX}created_at"
_UPDATED_FIELD = f"{RESERVED_PREFIX}updated_at"
# Qdrant L2-normalizes stored vectors for COSINE distance (verified against
# v1.19: stored = input / ||input||), which would break the platform's
# vectors-in/vectors-out contract (GET returns what was upserted). The
# original dense vector is therefore round-tripped in the payload under this
# reserved key and returned on fetch; the storage vector is left to qdrant.
_VECTOR_FIELD = f"{RESERVED_PREFIX}vector"

# HNSW params Qdrant can hot-update on a live collection (no rebuild) — the
# PATCH /config mutable subset (CapabilityMatrix).
_MUTABLE_CONFIG = frozenset({"m", "ef_construct", "ef_search", "payload_m"})


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


def _tenant_condition(tenant_id: str) -> models.FieldCondition:
    """The always-on tenant scope — every read/write/delete composes this."""
    return models.FieldCondition(key=_TENANT_FIELD, match=models.MatchValue(value=tenant_id))


class _FilterTranslator:
    """Platform filter DSL (app.schemas.vectors.validate_filter shape) ->
    qdrant models.Filter. Field keys map to payload fields (dotted keys nest
    natively); $and/$or/$not flatten into qdrant's must/should/must_not
    (nested logic groups are flattened, matching the v1 subset's semantics).
    """

    @staticmethod
    def translate(filters: dict[str, Any] | None) -> models.Filter | None:
        if not filters:
            return None
        return _FilterTranslator._node(filters)

    @staticmethod
    def _conditions(value: Any) -> list[models.Condition]:
        """qdrant's Filter.must/should/must_not are typed as
        ``list[Condition] | Condition | None`` but are always a list or None
        at runtime — normalize for mypy."""
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    @staticmethod
    def _node(node: dict[str, Any]) -> models.Filter:
        must: list[models.Condition] = []
        should: list[models.Condition] = []
        must_not: list[models.Condition] = []
        for key, value in node.items():
            if key == "$and":
                for sub in value:
                    f = _FilterTranslator._node(sub)
                    must.extend(_FilterTranslator._conditions(f.must))
                    must.extend(_FilterTranslator._conditions(f.should))
                    must.extend(_FilterTranslator._conditions(f.must_not))
            elif key == "$or":
                for sub in value:
                    f = _FilterTranslator._node(sub)
                    should.extend(_FilterTranslator._conditions(f.must))
                    should.extend(_FilterTranslator._conditions(f.should))
                    must_not.extend(_FilterTranslator._conditions(f.must_not))
            elif key == "$not":
                f = _FilterTranslator._node(value)
                must_not.extend(_FilterTranslator._conditions(f.must))
                must_not.extend(_FilterTranslator._conditions(f.should))
                must_not.extend(_FilterTranslator._conditions(f.must_not))
            else:
                must.append(_FilterTranslator._field(key, value))
        return models.Filter(must=must or None, should=should or None, must_not=must_not or None)

    @staticmethod
    def _field(key: str, value: Any) -> models.Condition:
        if not isinstance(value, dict):
            return models.FieldCondition(key=key, match=models.MatchValue(value=value))
        must: list[models.FieldCondition] = []
        must_not: list[models.FieldCondition] = []
        for op, operand in value.items():
            if op == "$eq":
                must.append(models.FieldCondition(key=key, match=models.MatchValue(value=operand)))
            elif op == "$ne":
                must_not.append(
                    models.FieldCondition(key=key, match=models.MatchValue(value=operand))
                )
            elif op in ("$gt", "$gte", "$lt", "$lte"):
                must.append(
                    models.FieldCondition(key=key, range=models.Range(**{op.lstrip("$"): operand}))
                )
            elif op == "$in":
                must.append(
                    models.FieldCondition(key=key, match=models.MatchAny(any=list(operand)))
                )
            elif op == "$nin":
                must_not.append(
                    models.FieldCondition(key=key, match=models.MatchAny(any=list(operand)))
                )
            elif op == "$contains":
                must.append(models.FieldCondition(key=key, match=models.MatchText(text=operand)))
            elif op == "$not_contains":
                must_not.append(
                    models.FieldCondition(key=key, match=models.MatchText(text=operand))
                )
            else:  # schema validates ops; defensive for direct adapter callers
                raise AppError(
                    ErrorCode.VALIDATION_INVALID_FILTER,
                    f"Unsupported filter operator '{op}' on field '{key}'",
                    details={"backend": "qdrant", "field": key, "operator": op},
                    status_code=422,
                )
        if not must and not must_not:
            raise AppError(
                ErrorCode.VALIDATION_INVALID_FILTER,
                f"Empty filter on field '{key}'",
                details={"backend": "qdrant", "field": key},
                status_code=422,
            )
        if must_not:
            return models.Filter(must=must or None, must_not=must_not)
        return must[0] if len(must) == 1 else models.Filter(must=must)


class QdrantAdapter(VectorDBAdapter):
    backend_name: ClassVar[str] = "qdrant"
    tenancy_model: ClassVar[str] = "payload-partition"

    def __init__(self, url: str | None = None) -> None:
        self._url = url or get_settings().qdrant_url
        parsed = urlparse(self._url)
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or 6333
        self._client: AsyncQdrantClient | None = None
        self._http: httpx.AsyncClient | None = None

    async def _get_client(self) -> AsyncQdrantClient:
        if self._client is None:
            self._client = AsyncQdrantClient(url=self._url, timeout=30)
        return self._client

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self._url, timeout=30)
        return self._http

    # --- health ---

    async def health_check(self) -> None:
        await (await self._get_client()).get_collections()

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
                f"Distance metric '{distance_metric}' is not supported on qdrant",
                details={"backend": "qdrant", "capability": f"distance_metric:{distance_metric}"},
                status_code=400,
            )
        client = await self._get_client()
        await client.create_collection(
            collection_name=name,
            vectors_config={"dense": models.VectorParams(size=dimension, distance=distance)},
            sparse_vectors_config={"sparse": models.SparseVectorParams()},
        )
        # The tenant boundary: a keyword payload index on the tenant field with
        # is_tenant=true (Qdrant's storage-level co-location hint). Created
        # with the collection; ensure_tenant is a no-op (see its docstring).
        await client.create_payload_index(
            collection_name=name,
            field_name=_TENANT_FIELD,
            field_schema=models.KeywordIndexParams(
                type=models.KeywordIndexType.KEYWORD, is_tenant=True
            ),
        )

    async def delete_collection(self, *, name: str) -> None:
        client = await self._get_client()
        if not await client.collection_exists(collection_name=name):
            return  # tolerant: nothing to hard-delete
        await client.delete_collection(collection_name=name)

    async def list_collections(self) -> list[str]:
        client = await self._get_client()
        result = await client.get_collections()
        return [c.name for c in result.collections]

    async def get_collection_info(self, *, name: str) -> CollectionInfo | None:
        client = await self._get_client()
        if not await client.collection_exists(collection_name=name):
            return None
        info = await client.get_collection(collection_name=name)
        dense = info.config.params.vectors
        # Named vectors arrive as a dict keyed by name.
        vectors = dense if isinstance(dense, dict) else {"dense": dense}
        dense_params = vectors.get("dense")
        dimension = int(dense_params.size) if dense_params is not None else None
        metric = _DISTANCE_TO_METRIC.get(dense_params.distance.value) if dense_params else None
        return CollectionInfo(name=name, dimension=dimension, distance_metric=metric)

    async def ensure_tenant(self, *, collection: str, tenant_id: str) -> None:
        """No-op for Qdrant's payload-partition model: the tenant boundary is
        the collection-level ``is_tenant`` keyword index on ``_vhk_tenant_id``,
        created with the collection (lazy provisioning = nothing to provision
        per tenant; every tenant shares the indexed field). Idempotent by
        construction. The service's per-request tenant assertion plus the
        always-applied tenant filter are the isolation enforcement points."""

    # --- vectors ---

    def _payload(
        self, record: VectorRecord, tenant_id: str, created_at: datetime
    ) -> dict[str, Any]:
        payload = {k: v for k, v in record.metadata.items()}
        payload[_ID_FIELD] = record.id
        payload[_TENANT_FIELD] = tenant_id
        payload[_CREATED_FIELD] = _iso(created_at)
        payload[_UPDATED_FIELD] = _iso(record.updated_at)
        # The original dense vector (qdrant stores cosine vectors normalized).
        payload[_VECTOR_FIELD] = [float(x) for x in record.vector]
        return payload

    @staticmethod
    def _point_vector(record: VectorRecord) -> dict[str, Any]:
        vector: dict[str, Any] = {"dense": record.vector}
        if record.sparse_vector is not None:
            vector["sparse"] = models.SparseVector(
                indices=record.sparse_vector.indices, values=record.sparse_vector.values
            )
        return vector

    def _to_record(
        self, payload: dict[str, Any] | None, vector: list[float]
    ) -> VectorRecord | None:
        if not payload:
            return None
        platform_id = payload.get(_ID_FIELD)
        if not isinstance(platform_id, str):
            return None  # a point not written by the platform — not ours to return
        created = _parse_ts(payload.get(_CREATED_FIELD)) or _utcnow()
        return VectorRecord(
            id=platform_id,
            vector=vector,
            metadata={k: v for k, v in payload.items() if not k.startswith(RESERVED_PREFIX)},
            tenant_id=str(payload.get(_TENANT_FIELD) or ""),
            created_at=created,
            updated_at=_parse_ts(payload.get(_UPDATED_FIELD)) or created,
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
        # (only updated_at refreshes): bulk retrieve by the deterministic point
        # ids, then merge. Missing ids fall back to the service-stamped
        # timestamp.
        ids = [point_uuid(tenant_id, r.id) for r in records]
        existing = await client.retrieve(
            collection_name=collection, ids=ids, with_payload=True, with_vectors=False
        )
        created_by_platform_id = {
            (p.payload or {}).get(_ID_FIELD): (p.payload or {}).get(_CREATED_FIELD)
            for p in existing
            if (p.payload or {}).get(_ID_FIELD)
        }
        points = [
            models.PointStruct(
                id=point_uuid(tenant_id, r.id),
                vector=self._point_vector(r),
                payload=self._payload(
                    r,
                    tenant_id,
                    _parse_ts(created_by_platform_id.get(r.id)) or r.created_at,
                ),
            )
            for r in records
        ]
        await client.upsert(collection_name=collection, points=points, wait=True)

    async def delete_vectors(self, *, collection: str, tenant_id: str, ids: list[str]) -> None:
        client = await self._get_client()
        point_ids = [point_uuid(tenant_id, rid) for rid in ids]
        await client.delete(
            collection_name=collection, points_selector=models.PointIdsList(points=point_ids)
        )

    async def fetch_vectors(
        self, *, collection: str, tenant_id: str, ids: list[str]
    ) -> list[VectorRecord]:
        client = await self._get_client()
        point_ids = [point_uuid(tenant_id, rid) for rid in ids]
        fetched = await client.retrieve(
            collection_name=collection, ids=point_ids, with_payload=True, with_vectors=True
        )
        records: list[VectorRecord] = []
        for point in fetched:
            # Prefer the round-tripped original vector (qdrant stores cosine
            # vectors normalized); fall back to the storage vector for points
            # written outside the platform.
            payload = point.payload or {}
            original = payload.get(_VECTOR_FIELD)
            if isinstance(original, list):
                vector = [float(x) for x in original]
            else:
                # point.vector is typed as a broken union (list | dict | None);
                # the runtime contract for named-vector collections is a dict
                # keyed by vector name.
                vector = [float(x) for x in cast(Any, point.vector).get("dense", [])]
            record = self._to_record(payload, vector)
            if record is not None:
                records.append(record)
        return records

    def _scoped_filter(self, tenant_id: str, filters: dict[str, Any] | None) -> models.Filter:
        user = _FilterTranslator.translate(filters)
        return models.Filter(
            must=[_tenant_condition(tenant_id), *((user.must or []) if user else [])],
            should=(user.should or None) if user else None,
            must_not=(user.must_not or None) if user else None,
        )

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
        try:
            result = await client.query_points(
                collection_name=collection,
                query=vector,
                using="dense",
                query_filter=self._scoped_filter(tenant_id, filters),
                limit=top_k,
                with_payload=True,
                with_vectors=False,
            )
        except UnexpectedResponse as exc:
            raise AppError(
                ErrorCode.VALIDATION_INVALID_FILTER,
                f"Metadata filter rejected by qdrant: {exc}",
                details={"backend": "qdrant", "filters": filters},
                status_code=422,
            ) from exc
        return [self._query_result(p.payload, p.score) for p in result.points]

    @staticmethod
    def _query_result(payload: dict[str, Any] | None, score: float) -> QueryResult:
        if not payload:
            return QueryResult(id="", score=float(score))
        created = _parse_ts(payload.get(_CREATED_FIELD)) or _utcnow()
        return QueryResult(
            id=str(payload.get(_ID_FIELD) or ""),
            score=float(score),
            metadata={k: v for k, v in payload.items() if not k.startswith(RESERVED_PREFIX)},
            tenant_id=str(payload.get(_TENANT_FIELD) or None),
            created_at=_parse_ts(payload.get(_CREATED_FIELD)),
            updated_at=_parse_ts(payload.get(_UPDATED_FIELD)) or created,
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
        # The typed client 1.19 dropped Prefetch.weight (extra_forbidden)
        # though the server honors it — so the one hybrid call goes over raw
        # REST. Filters serialize from the same translator (model_dump).
        if sparse_vector is None:  # the service enforces this; defensive here
            raise AppError(
                ErrorCode.VECTOR_SPARSE_REQUIRED,
                "Hybrid search on qdrant requires a sparse_vector",
                details={"backend": "qdrant", "capability": "sparse_vector"},
                status_code=422,
            )
        scoped = self._scoped_filter(tenant_id, filters)
        body = {
            "prefetch": [
                {
                    "query": {
                        "indices": sparse_vector.indices,
                        "values": sparse_vector.values,
                    },
                    "using": "sparse",
                    "weight": round(1.0 - alpha, 6),
                },
                {"query": vector, "using": "dense", "weight": round(alpha, 6)},
            ],
            "query": {"fusion": "rrf"},
            "filter": scoped.model_dump(exclude_none=True),
            "limit": top_k,
            "with_payload": True,
        }
        http = await self._get_http()
        resp = await http.post(f"/collections/{collection}/points/query", json=body)
        if resp.status_code != 200:
            raise AppError(
                ErrorCode.VALIDATION_INVALID_FILTER,
                f"Hybrid query rejected by qdrant: {resp.text[:200]}",
                details={"backend": "qdrant", "filters": filters},
                status_code=422,
            )
        data = resp.json()
        results: list[QueryResult] = []
        for point in data.get("result", {}).get("points", []):
            results.append(self._query_result(point.get("payload"), point.get("score", 0.0)))
        return results

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
        # Per-backend sizing contract: Qdrant 5–10k records per request. Each
        # chunk is one retrieve (created_at merge) + one upsert; a failed chunk
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
        # Qdrant hot-updates HNSW params on a live collection (the mutable
        # subset below); anything else 409s at PATCH /config before reaching
        # here (REQUIRES_REINDEX, per the capability matrix).
        client = await self._get_client()
        allowed = {k: v for k, v in index_config.items() if k in _MUTABLE_CONFIG}
        await client.update_collection(
            collection_name=collection, hnsw_config=models.HnswConfigDiff(**allowed)
        )

    # --- introspection ---

    def capability(self) -> CapabilityEntry:
        return CapabilityEntry(
            backend=self.backend_name,
            tenancy_model=self.tenancy_model,
            hybrid_mode="sparse+vector",
            sparse_required=True,
            filtering=True,
            batch_async=True,
            quantization=True,
            multi_vector=False,
            sparse_vectors=True,
            mutable_config=_MUTABLE_CONFIG,
            notes=(
                "tenancy: payload-partition on the _vhk_tenant_id keyword index (is_tenant=true) — "
                "Qdrant's native tenant API was removed server-side; the adapter ALWAYS applies "
                "the tenant filter (no unscoped path), backed by Qdrant's is_tenant storage "
                "co-location. Not a separate-shard boundary: a raw unscoped query returns all "
                "tenants' points.",
                "point ids are deterministic UUID5(tenant:platform_id); platform ids round-trip "
                "via _vhk_id",
                "hybrid: dense+sparse prefetch fused with RRF; alpha = prefetch weights "
                "(dense=alpha, sparse=1-alpha) sent over raw REST (client 1.19 dropped the "
                "typed weight field)",
                "score = qdrant cosine similarity; higher is more similar",
                "qdrant stores COSINE vectors L2-normalized; the platform round-trips the "
                "original vector via a reserved payload field so GET returns what was upserted",
                "metadata payloads are arbitrary JSON (nested dicts filter via dotted keys)",
                "delete is immediate at the backend level",
            ),
        )
