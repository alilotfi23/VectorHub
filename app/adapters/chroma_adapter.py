"""Chroma adapter — per-tenant physical collections (CLAUDE.md Tenancy Matrix).

**Tenancy model (``collection-per-tenant``):** Chroma has no native
multi-tenancy, so each (tenant, platform collection) pair is its own physical
collection. The platform generates an opaque ``col_<uuid>`` physical name per
registry row — two tenants' ``products`` collections are distinct physical
objects by construction, so the isolation boundary is the physical collection
itself and ``ensure_tenant`` is the idempotent ``get_or_create`` of that
object (create-if-absent, no-op-if-present). The adapter never sees
client-facing names.

**Record-schema exceptions (documented per CLAUDE.md):** Chroma does not
natively track per-record ``created_at``/``updated_at`` (and has no per-record
tenant field), so this adapter folds them into the stored metadata under the
reserved ``_vhk_*`` keys (``_vhk_created_at``, ``_vhk_updated_at``,
``_vhk_tenant_id``, ISO-8601 UTC strings) and strips them again on read. The
platform's API schemas reject user metadata keys with the same reserved
prefix. Chroma metadata values must be JSON primitives (str/int/float/bool/
None or lists of those) — dict values are JSON-serialized on write
(document in CapabilityMatrix: filter primitive-valued fields only).

**Score semantics:** ``query`` returns Chroma's distance (``1 - cosine`` for
the cosine space) — **lower is more similar**. This is backend-native and
documented as such; a future phase may normalize across backends.

**Client lifecycle:** one ``AsyncHttpClient`` per adapter instance, built
lazily on first use (never at construction, so import-time registration is
safe even when Chroma isn't running) and held for the app's lifetime — the
singleton-per-backend contract from the registry docstring.

**SDK note:** chromadb 1.5.x ships a first-class async client
(``chromadb.AsyncHttpClient``); this adapter uses it natively rather than
wrapping the sync client in ``asyncio.to_thread``.
"""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime
from typing import Any, ClassVar, cast
from urllib.parse import urlparse

import numpy as np
from chromadb import AsyncHttpClient
from chromadb.api.async_api import AsyncClientAPI
from chromadb.api.collection_configuration import (
    CreateCollectionConfiguration,
    CreateHNSWConfiguration,
)
from chromadb.api.models.AsyncCollection import AsyncCollection
from chromadb.errors import InvalidArgumentError, NotFoundError

from app.adapters.base import (
    BatchResult,
    CapabilityEntry,
    CollectionInfo,
    QueryResult,
    SparseVector,
    VectorDBAdapter,
    VectorRecord,
)
from app.core.config import get_settings
from app.core.exceptions import AppError, ErrorCode

# Platform metric -> Chroma HNSW space.
_METRIC_TO_SPACE: dict[str, str] = {"cosine": "cosine", "euclidean": "l2", "dot": "ip"}
_SPACE_TO_METRIC: dict[str, str] = {v: k for k, v in _METRIC_TO_SPACE.items()}

RESERVED_PREFIX = "_vhk_"
_RESERVED_KEYS = (
    f"{RESERVED_PREFIX}tenant_id",
    f"{RESERVED_PREFIX}created_at",
    f"{RESERVED_PREFIX}updated_at",
)


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


def _as_floats(value: Any) -> list[float]:
    """Normalize chroma's embedding returns (lists or numpy arrays) to floats."""
    if isinstance(value, list):
        return [float(x) for x in value]
    if hasattr(value, "tolist"):  # numpy arrays come back from get(include=["embeddings"])
        return [float(x) for x in value.tolist()]
    raise TypeError(f"unexpected embedding type: {type(value).__name__}")


def _coerce_metadata_value(value: Any) -> Any:
    """Chroma metadata accepts primitives and lists of primitives; anything
    else (dicts, nested structures) is JSON-serialized so the platform's
    "arbitrary user payload" contract holds at the cost of filterability."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_coerce_metadata_value(v) for v in value]
    return _json.dumps(value, default=str, sort_keys=True)


class ChromaAdapter(VectorDBAdapter):
    """One SDK client per instance, lazy and process-lifetime."""

    backend_name: ClassVar[str] = "chroma"
    tenancy_model: ClassVar[str] = "collection-per-tenant"

    def __init__(self, url: str | None = None) -> None:
        """Build the adapter from a URL (default: settings ``chroma_url``).
        Never connects — the client is created on first use."""
        self._url = url or get_settings().chroma_url
        parsed = urlparse(self._url)
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or 8000
        self._ssl = parsed.scheme == "https"
        self._client: AsyncClientAPI | None = None

    async def _get_client(self) -> AsyncClientAPI:
        if self._client is None:
            self._client = await AsyncHttpClient(host=self._host, port=self._port, ssl=self._ssl)
        return self._client

    # --- health ---

    async def health_check(self) -> None:
        await (await self._get_client()).heartbeat()

    # --- collection lifecycle ---

    async def create_collection(
        self,
        *,
        name: str,
        dimension: int,
        distance_metric: str,
        extras: dict[str, Any] | None = None,
    ) -> None:
        space = _METRIC_TO_SPACE.get(distance_metric)
        if space is None:
            raise AppError(
                ErrorCode.VALIDATION_UNSUPPORTED_OPERATION,
                f"Distance metric '{distance_metric}' is not supported on chroma",
                details={"backend": "chroma", "capability": f"distance_metric:{distance_metric}"},
                status_code=400,
            )
        client = await self._get_client()
        await client.create_collection(
            name=name,
            metadata={"dimension": dimension, "platform_metric": distance_metric},
            # The 1.x canonical configuration key is `hnsw` (the TypedDict's
            # `hnsw_configuration` spelling silently no-ops and the collection
            # falls back to l2 — a real trap).
            configuration=CreateCollectionConfiguration(
                hnsw=CreateHNSWConfiguration(space=cast(Any, space))
            ),
        )

    async def delete_collection(self, *, name: str) -> None:
        client = await self._get_client()
        try:
            await client.delete_collection(name=name)
        except NotFoundError:
            pass  # tolerant: nothing to hard-delete

    async def list_collections(self) -> list[str]:
        client = await self._get_client()
        collections = await client.list_collections()
        return [c.name for c in collections]

    async def get_collection_info(self, *, name: str) -> CollectionInfo | None:
        client = await self._get_client()
        try:
            col = await client.get_collection(name=name)
        except NotFoundError:
            return None
        meta = col.metadata or {}
        dimension = meta.get("dimension")
        platform_metric = meta.get("platform_metric")
        metric = platform_metric if isinstance(platform_metric, str) else None
        if metric is None:
            raw_space = meta.get("hnsw:space")
            metric = _SPACE_TO_METRIC.get(raw_space) if isinstance(raw_space, str) else None
        return CollectionInfo(
            name=name,
            dimension=int(dimension) if isinstance(dimension, int) else None,
            distance_metric=metric,
        )

    async def ensure_tenant(self, *, collection: str, tenant_id: str) -> None:
        """Idempotent provisioning: for Chroma the physical collection IS the
        tenant boundary, so this is the collection's get_or_create — a no-op
        when it already exists, a create when the platform row drifted ahead
        of the backend (lazy provisioning per CLAUDE.md)."""
        client = await self._get_client()
        await client.get_or_create_collection(name=collection)

    # --- vectors ---

    async def _collection(self, name: str) -> AsyncCollection:
        """Resolve a physical collection, or fail loud on drift: a missing
        physical object behind an existing registry row is a backend/registry
        inconsistency, not a client mistake."""
        client = await self._get_client()
        try:
            return await client.get_collection(name=name)
        except NotFoundError as exc:
            raise AppError(
                ErrorCode.COLLECTION_BACKEND_UNAVAILABLE,
                f"Physical collection '{name}' does not exist on chroma (registry/backend drift?)",
                details={"backend": "chroma", "physical_name": name},
                status_code=503,
            ) from exc

    @staticmethod
    def _assert_no_reserved_keys(metadata: dict[str, Any]) -> None:
        for key in metadata:
            if key.startswith(RESERVED_PREFIX):
                raise AppError(
                    ErrorCode.VALIDATION_GENERIC,
                    f"Metadata key '{key}' uses the reserved '{RESERVED_PREFIX}' prefix",
                    status_code=422,
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
        for record in records:
            self._assert_no_reserved_keys(record.metadata)
        ids = [r.id for r in records]
        # Preserve each existing record's created_at across idempotent
        # upserts (only updated_at refreshes): one get for the batch, then
        # merge. New ids fall back to the service-stamped timestamp. (SDK
        # typing note: GetResult unions are cast to Any, as in fetch_vectors.)
        existing = cast(Any, await col.get(ids=ids, include=["metadatas"]))
        created_by_id = {
            i: (m or {}).get(f"{RESERVED_PREFIX}created_at")
            for i, m in zip(existing["ids"], existing["metadatas"], strict=True)
        }
        chroma_metadatas: list[dict[str, Any]] = []
        for record in records:
            meta = {k: _coerce_metadata_value(v) for k, v in record.metadata.items()}
            meta[f"{RESERVED_PREFIX}tenant_id"] = tenant_id
            meta[f"{RESERVED_PREFIX}created_at"] = created_by_id.get(record.id) or _iso(
                record.created_at
            )
            meta[f"{RESERVED_PREFIX}updated_at"] = _iso(record.updated_at)
            chroma_metadatas.append(meta)
        await col.upsert(
            ids=ids,
            # The SDK types embeddings as numpy/Sequence unions; pass numpy so
            # the dtype is explicit. Both payloads are cast because the SDK's
            # static unions (ndarray shape args, Mapping vs dict) are looser
            # than the actual contract.
            embeddings=cast(Any, [np.asarray(r.vector, dtype=np.float32) for r in records]),
            metadatas=cast(Any, chroma_metadatas),
        )

    async def delete_vectors(self, *, collection: str, tenant_id: str, ids: list[str]) -> None:
        col = await self._collection(collection)
        await col.delete(ids=ids)  # chroma tolerates missing ids

    async def fetch_vectors(
        self, *, collection: str, tenant_id: str, ids: list[str]
    ) -> list[VectorRecord]:
        col = await self._collection(collection)
        # The SDK's static types for GetResult are broken unions of Mapping /
        # ndarray / None; its runtime contract is stable (verified by smoke),
        # so the result is cast once with the shapes documented here.
        res = cast(Any, await col.get(ids=ids, include=["metadatas", "embeddings"]))
        metadatas: list[dict[str, Any]] = [m or {} for m in res["metadatas"] or []]
        # Embeddings come back as numpy arrays (whose truth value is
        # ambiguous) — never use `or []` on them.
        raw_embeddings = res["embeddings"]
        embeddings = raw_embeddings if raw_embeddings is not None else []
        records: list[VectorRecord] = []
        for rid, meta, emb in zip(res["ids"], metadatas, embeddings, strict=True):
            meta = meta or {}
            records.append(
                VectorRecord(
                    id=rid,
                    vector=_as_floats(emb),
                    metadata={k: v for k, v in meta.items() if not k.startswith(RESERVED_PREFIX)},
                    tenant_id=str(meta.get(f"{RESERVED_PREFIX}tenant_id") or ""),
                    created_at=_parse_ts(meta.get(f"{RESERVED_PREFIX}created_at")) or _utcnow(),
                    updated_at=_parse_ts(meta.get(f"{RESERVED_PREFIX}updated_at")) or _utcnow(),
                )
            )
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
        col = await self._collection(collection)
        where_document = (extras or {}).get("where_document")
        try:
            res = await col.query(
                query_embeddings=cast(Any, [np.asarray(vector, dtype=np.float32)]),
                n_results=top_k,
                where=filters,
                where_document=where_document,
                include=["metadatas", "distances"],
            )
        except InvalidArgumentError as exc:
            raise AppError(
                ErrorCode.VALIDATION_INVALID_FILTER,
                f"Metadata filter rejected by chroma: {exc}",
                details={"backend": "chroma", "filters": filters},
                status_code=422,
            ) from exc
        # Same SDK-typing note as fetch_vectors: QueryResult's unions are
        # broken statically; the runtime contract (one entry per query, with
        # None for absent metadata) is what this code relies on.
        res = cast(Any, res)
        raw_ids = res.get("ids") or [[]]
        raw_distances = res.get("distances") or [[]]
        raw_metadatas = res.get("metadatas") or [[]]
        ids: list[str] = raw_ids[0]
        distances: list[float] = raw_distances[0]
        metadatas: list[dict[str, Any]] = [m or {} for m in raw_metadatas[0]]
        results: list[QueryResult] = []
        for rid, dist, meta in zip(ids, distances, metadatas, strict=True):
            results.append(
                QueryResult(
                    id=rid,
                    score=float(dist),
                    metadata={k: v for k, v in meta.items() if not k.startswith(RESERVED_PREFIX)},
                    tenant_id=str(meta.get(f"{RESERVED_PREFIX}tenant_id") or None),
                    created_at=_parse_ts(meta.get(f"{RESERVED_PREFIX}created_at")),
                    updated_at=_parse_ts(meta.get(f"{RESERVED_PREFIX}updated_at")),
                )
            )
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
        # Per-backend sizing contract: Chroma 100–1k records per request. Each
        # chunk is one get (created_at merge) + one upsert; a failed chunk
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
        # Chroma builds its HNSW index automatically at insert with the
        # creation-time configuration; nothing is hot-mutable, so
        # mutable_config is empty and PATCH /config 409s before reaching
        # here. Resolving the collection keeps a drifted registry row loud.
        await self._collection(collection)

    # --- hybrid (unsupported) ---

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
        raise AppError(
            ErrorCode.VALIDATION_UNSUPPORTED_OPERATION,
            "Hybrid search is not supported on chroma",
            details={"backend": "chroma", "capability": "hybrid_search"},
            status_code=400,
        )

    # --- introspection ---

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
            mutable_config=frozenset(),  # index config is creation-time on Chroma
            notes=(
                "tenancy: per-tenant physical collections; the physical object IS the boundary",
                "created_at/updated_at/tenant_id folded into reserved _vhk_* metadata keys",
                "metadata must be JSON-primitive or lists; dicts are JSON-serialized on write — "
                "filter primitive-valued fields only",
                "metadata keys with the _vhk_ prefix are reserved",
                "score = chroma distance; lower is more similar (cosine: 1 - similarity)",
                "delete is immediate; chroma's own compaction may reclaim space asynchronously",
                "hybrid search unsupported (capability=hybrid_search, see error taxonomy)",
            ),
        )
