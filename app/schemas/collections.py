from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.schemas.auth import MemberRole, StrictRequest

# Backend names are schema-valid even before their adapters register; creating
# on an unregistered backend returns 503 COLLECTION_BACKEND_UNAVAILABLE.
Backend = Literal["chroma", "qdrant", "weaviate", "milvus"]
DistanceMetric = Literal["cosine", "euclidean", "dot"]

BackendStatus = Literal["exists", "missing", "error"]


class CollectionCreateRequest(StrictRequest):
    """Strict envelope: no tenant_id/owner_id — the collection is created
    under the authenticated principal's tenant (isolation-suite R3/E3).
    ``backend`` is immutable for the life of the collection."""

    name: str = Field(
        min_length=1, max_length=255, description="Client-facing name, unique within the tenant"
    )
    backend: Backend = "chroma"
    dimension: int = Field(
        ge=1,
        le=get_settings().vector_max_dimension,
        description="Embedding dimension of the collection (immutable post-creation)",
    )
    distance_metric: DistanceMetric = "cosine"
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Collection-level metadata (arbitrary user payload; may contain PII — "
            "see README: delete is destructive and immediate at the backend level)"
        ),
    )


class CollectionResponse(BaseModel):
    """A collection on the read path. ``backend_status`` is the drift-visibility
    field: exists | missing | error (disjoint by construction)."""

    name: str
    backend: str
    dimension: int
    distance_metric: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: str
    backend_status: BackendStatus | None = None
    created_at: datetime
    updated_at: datetime


class CollectionListResponse(BaseModel):
    """Cursor-paginated collection list: `items` plus an opaque `next_cursor`
    (None on the last page) and the total count."""

    items: list[CollectionResponse]
    next_cursor: str | None
    total: int


class CollectionConfigUpdateRequest(StrictRequest):
    """Index-config mutation post-creation. Only the backend's CapabilityMatrix
    ``mutable_config`` subset applies without a rebuild; anything else is a
    409 REQUIRES_REINDEX with a stated next_step."""

    index_config: dict[str, Any] = Field(
        min_length=1,
        description=(
            "Index parameters to mutate (HNSW params, etc.). Per-backend support is "
            "declared in the CapabilityMatrix (GET /capabilities, Phase 6); "
            "unsupported keys return 409 REQUIRES_REINDEX."
        ),
    )


class CollectionPermissionUpdateRequest(StrictRequest):
    """Strict envelope: the grant body deliberately has no tenant_id field —
    a client-supplied tenant/owner id must be rejected, never silently
    dropped (isolation-suite R3/E3)."""

    user_id: str = Field(min_length=1, description="Tenant member to grant the role to")
    role: MemberRole


class CollectionPermissionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    collection_id: str
    collection_name: str
    user_id: str
    role: str
    created_at: datetime


class CollectionPermissionListResponse(BaseModel):
    """Cursor-paginated grant list: `items` plus an opaque `next_cursor`
    (None on the last page) and the total grant count."""

    items: list[CollectionPermissionResponse]
    next_cursor: str | None
    total: int
