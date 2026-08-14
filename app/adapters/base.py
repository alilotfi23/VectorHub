"""The vector-backend adapter contract (Phase 3: the ABC lands with Chroma).

Every backend is accessed exclusively through this interface — the service
layer never touches an SDK directly, and adapters never see client-facing
collection names (only the opaque ``col_<uuid>`` physical names from the
registry). Signatures are deliberately symmetric across backends: the
platform's unified surface is this ABC, and backend-specific behavior is
surfaced through ``extras`` passthroughs and the per-backend
``CapabilityEntry`` — never through lowest-common-denominator reduction.

Record contract (``VectorRecord``): ``id`` (client-supplied, string, for
idempotent upserts), ``vector`` (list[float]), ``sparse_vector`` (optional —
required for hybrid search on Qdrant/Milvus, Phase 4+), ``metadata``
(arbitrary user payload; backend-native filtering applies against this),
``tenant_id`` (derived server-side, never client-supplied — an assertion and
audit field, load-bearing for Milvus partition routing but never the
isolation mechanism; see CLAUDE.md's Tenancy Matrix), and server-set UTC
``created_at``/``updated_at``. Note: Chroma has no native per-record
timestamps, so ``ChromaAdapter`` folds them (and ``tenant_id``) into the
stored metadata under reserved ``_vhk_*`` keys rather than dropping them —
documented in its docstring and in the capability notes.

Score semantics are backend-native and documented per adapter
(``ChromaAdapter`` returns Chroma's distance — lower is more similar; the
Phase 4+ adapters document theirs). A future phase may normalize scores
once all four backends are in and the discrepancy is observable.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

# The platform's v1 distance metrics. Backend-specific spellings (Chroma's
# "l2"/"ip" spaces, etc.) are translated inside each adapter.
DistanceMetric = Literal["cosine", "euclidean", "dot"]

# Reserved payload/property keys every adapter stores alongside user metadata
# (same names across backends; the platform's API schemas reject user keys
# with this prefix). The tenant key doubles as the Qdrant is_tenant partition
# field.
RESERVED_PREFIX = "_vhk_"
RESERVED_KEYS = (
    f"{RESERVED_PREFIX}tenant_id",
    f"{RESERVED_PREFIX}created_at",
    f"{RESERVED_PREFIX}updated_at",
)


def point_uuid(tenant_id: str, platform_id: str) -> str:
    """Deterministic backend point/object id for a (tenant, platform id)
    pair. Qdrant requires uint/UUID point ids and Weaviate requires UUIDs,
    and both are global to the physical collection — so two tenants' same
    platform id (``doc-1`` under A and B) must map to distinct backend ids.
    UUID5 over ``{tenant_id}:{platform_id}`` is deterministic (idempotent
    upserts and deletes re-derive the same id) and collision-free by
    construction. The platform id itself is stored in the payload/properties
    under ``_vhk_id`` and recovered on read.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{tenant_id}:{platform_id}"))


class SparseVector(BaseModel):
    """Client-supplied sparse vector (indices ascending), Phase 4+."""

    indices: list[int]
    values: list[float]


class VectorRecord(BaseModel):
    """The standard vector record every adapter accepts and returns."""

    id: str
    vector: list[float] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    sparse_vector: SparseVector | None = None
    tenant_id: str  # server-derived; see module docstring
    created_at: datetime
    updated_at: datetime


class QueryResult(BaseModel):
    """One row of a similarity-search result. ``score`` is backend-native
    (see module docstring); ``tenant_id``/timestamps are server-side fields
    recovered where the backend stores them."""

    id: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)
    tenant_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class CollectionInfo:
    """What the backend knows about a physical collection. ``None`` for the
    optional fields means the backend doesn't store that fact (e.g. a
    collection created outside the platform); the platform's registry row is
    authoritative. ``get_collection_info`` returning ``None`` (the object)
    means the physical collection does not exist — the ``missing`` arm of the
    ``backend_status`` read-path contract."""

    name: str
    dimension: int | None = None
    distance_metric: str | None = None


@dataclass(frozen=True)
class BatchResult:
    """Outcome of a chunked batch operation. Errors abort the batch and
    surface as exceptions; ``failed`` is reserved for per-record outcomes in
    the Phase 6 worker."""

    ok: int
    failed: int = 0


@dataclass(frozen=True)
class CapabilityEntry:
    """One backend's row in the CapabilityMatrix (exposed at GET /capabilities
    in Phase 6; consumed now by PATCH /config and the isolation suites).

    ``tenancy_model`` values: ``"collection-per-tenant"`` (Chroma — the
    physical collection *is* the tenant boundary), ``"native-tenant"``
    (Qdrant/Weaviate native multi-tenancy), ``"partition-per-tenant"``
    (Milvus). ``hybrid_mode``: ``"text+vector"`` (Weaviate), ``"sparse+vector"``
    (Qdrant/Milvus), or ``None`` (Chroma). ``mutable_config`` is the subset of
    index parameters this backend can change post-creation without a rebuild;
    anything else on PATCH /config is a 409 ``REQUIRES_REINDEX``.
    """

    backend: str
    tenancy_model: str
    hybrid_mode: Literal["text+vector", "sparse+vector"] | None
    sparse_required: bool
    filtering: bool
    batch_async: bool
    quantization: bool
    multi_vector: bool
    sparse_vectors: bool
    mutable_config: frozenset[str] = frozenset()
    notes: tuple[str, ...] = field(default_factory=tuple)


class VectorDBAdapter(ABC):
    """Abstract vector-DB adapter. One instance per backend name, constructed
    once at registration and held for the app's lifetime (never per-request,
    never per-tenant — see the registry docstring). All methods are async.

    Exception contract for implementers:

    - Raise ``AppError`` for client-error-class failures (unsupported
      operations, invalid filters, drifted/missing physical objects) with the
      taxonomy code that matches the HTTP surface.
    - Let transport/connectivity failures propagate as-is (the service layer
      maps them to ``COLLECTION_BACKEND_UNAVAILABLE``). Distinguishing the
      two keeps the runbook honest: "check the network/auth" vs "check the
      backend", per the ``backend_status`` drift design.
    - ``get_collection_info`` returns ``None`` (not an error) when the
      physical object deterministically does not exist.
    """

    backend_name: ClassVar[str]
    tenancy_model: ClassVar[str]

    @abstractmethod
    async def health_check(self) -> None:
        """Verify connectivity to the backend; raise on failure."""

    # --- collection lifecycle (physical names only) ---

    @abstractmethod
    async def create_collection(
        self,
        *,
        name: str,
        dimension: int,
        distance_metric: str,
        extras: dict[str, Any] | None = None,
    ) -> None: ...

    @abstractmethod
    async def delete_collection(self, *, name: str) -> None:
        """Hard-delete the physical collection. Tolerant of an already-missing
        object (idempotent retry); raises on genuine backend failure."""

    @abstractmethod
    async def list_collections(self) -> list[str]: ...

    @abstractmethod
    async def get_collection_info(self, *, name: str) -> CollectionInfo | None:
        """None = the physical collection does not exist (drift -> "missing"
        on the read path); raises on backend failure ("error")."""

    @abstractmethod
    async def ensure_tenant(self, *, collection: str, tenant_id: str) -> None:
        """Idempotently provision the tenant boundary inside the physical
        collection (native tenant / partition / per-tenant collection).
        Create-if-absent, no-op-if-present. See CLAUDE.md Tenancy Matrix."""

    # --- vectors ---

    @abstractmethod
    async def upsert_vectors(
        self,
        *,
        collection: str,
        tenant_id: str,
        records: list[VectorRecord],
        extras: dict[str, Any] | None = None,
    ) -> None:
        """Idempotent upsert by client-supplied id. Implementations should
        preserve each existing record's ``created_at`` (only ``updated_at``
        refreshes on overwrite)."""

    @abstractmethod
    async def delete_vectors(self, *, collection: str, tenant_id: str, ids: list[str]) -> None:
        """Hard-delete by id; missing ids are tolerated (idempotent)."""

    @abstractmethod
    async def fetch_vectors(
        self, *, collection: str, tenant_id: str, ids: list[str]
    ) -> list[VectorRecord]:
        """Return the records for the ids that exist (missing ids are simply
        absent from the result, in requested order)."""

    @abstractmethod
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
        """Top-k similarity search. ``filters`` is the platform's normalized
        metadata filter (chroma-shaped subset: equality shorthand, the
        $eq/$ne/$gt/$gte/$lt/$lte/$in/$nin/$contains/$not_contains operators,
        $and/$or/$not nesting — see app.schemas.vectors.validate_filter).
        Backends translate to their native filter form. ``extras`` passes
        backend-native query options through (e.g. Chroma ``where_document``).

        The tenant scope is enforced by the backend's native mechanism (the
        physical collection here); ``tenant_id`` is the service-level
        assertion the adapter carries into the call. Unscoped or mis-scoped
        behavior is fail-closed: raise or return empty, never cross-tenant
        rows (see the isolation design doc, §2)."""

    # --- batch (Phase 3: adapter-level; the async job path lands in Phase 6) ---

    @abstractmethod
    async def batch_upsert(
        self,
        *,
        collection: str,
        tenant_id: str,
        records: list[VectorRecord],
        chunk_size: int,
        extras: dict[str, Any] | None = None,
    ) -> BatchResult:
        """Chunked upsert with per-backend sizing (the chunking contract from
        the batch-throughput design: Chroma 100–1k per request, Qdrant 5–10k,
        Weaviate ~1k, Milvus 1–10k). The caller picks ``chunk_size``; the
        adapter applies backpressure and never assumes one size fits all.
        Upserts are idempotent, so a worker retry is safe."""

    @abstractmethod
    async def batch_delete(
        self,
        *,
        collection: str,
        tenant_id: str,
        ids: list[str],
        chunk_size: int,
    ) -> BatchResult: ...

    # --- index / config ---

    @abstractmethod
    async def create_index(
        self,
        *,
        collection: str,
        index_config: dict[str, Any],
        extras: dict[str, Any] | None = None,
    ) -> None:
        """Apply/validate index configuration. Only parameters in the
        backend's ``mutable_config`` subset reach this method (PATCH /config
        returns 409 REQUIRES_REINDEX otherwise); backends whose index is
        creation-time-only implement this as a validating no-op."""

    # --- hybrid (Phase 4+; backends without support raise the typed error) ---

    @abstractmethod
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
        """Hybrid dense+sparse/text search per CLAUDE.md's hybrid contract.
        Backends without hybrid support raise AppError
        VALIDATION_UNSUPPORTED_OPERATION with details.capability
        "hybrid_search"."""

    # --- introspection ---

    def capability(self) -> CapabilityEntry:
        """This backend's CapabilityMatrix row."""
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
            notes=(),
        )
