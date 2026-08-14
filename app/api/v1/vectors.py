"""Vector write/read/query routes (Phase 3). All paths live under the
collection resource (``/collections/{name}/vectors...``); the collection is
resolved once per request by ``require_collection_permission`` and handed
into the services (the resolve-once discipline). Hybrid search and the async
batch endpoint land in later phases.
"""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import SparseVector
from app.api.deps import get_current_principal, require_collection_permission
from app.core.rbac import Permission
from app.core.security import Principal
from app.db.session import get_session
from app.schemas.vectors import (
    HybridQueryRequest,
    QueryRequest,
    QueryResponse,
    QueryResultOut,
    UpsertResponse,
    VectorResponse,
    VectorUpsertRequest,
)
from app.services.collection_service import CollectionAccess
from app.services.search_service import SearchService
from app.services.vector_service import VectorService

router = APIRouter(prefix="/collections", tags=["vectors"])


@router.post("/{name}/vectors", response_model=UpsertResponse)
async def upsert_vectors(
    body: VectorUpsertRequest,
    access: CollectionAccess = Depends(require_collection_permission(Permission.VECTOR_WRITE)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> UpsertResponse:
    """Upsert 1–100 pre-computed vectors (idempotent on client-supplied ids).
    Larger loads must use the async batch endpoint (Phase 6). The envelope
    has no tenant_id — it is derived from the authenticated principal."""
    count = await VectorService(session).upsert(principal, access=access, records=body.vectors)
    return UpsertResponse(upserted=count)


@router.get("/{name}/vectors/{vector_id}", response_model=VectorResponse)
async def fetch_vector(
    vector_id: str,
    access: CollectionAccess = Depends(require_collection_permission(Permission.VECTOR_READ)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> VectorResponse:
    record = await VectorService(session).fetch(principal, access=access, vector_id=vector_id)
    return VectorResponse(
        id=record.id,
        vector=record.vector,
        metadata=record.metadata,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@router.delete("/{name}/vectors/{vector_id}", status_code=204)
async def delete_vector(
    vector_id: str,
    access: CollectionAccess = Depends(require_collection_permission(Permission.VECTOR_DELETE)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Hard delete (immediate at the backend level); idempotent — deleting an
    absent id is a no-op 204."""
    await VectorService(session).delete(principal, access=access, vector_id=vector_id)
    return Response(status_code=204)


@router.post("/{name}/query", response_model=QueryResponse)
async def query_vectors(
    body: QueryRequest,
    access: CollectionAccess = Depends(require_collection_permission(Permission.VECTOR_READ)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> QueryResponse:
    """Top-k similarity search with optional normalized metadata filters.
    ``score`` is backend-native (Chroma: distance, lower is more similar) —
    see the CapabilityMatrix notes."""
    results = await SearchService(session).query(
        principal,
        access=access,
        vector=body.vector,
        top_k=body.top_k,
        filters=body.filters,
    )
    return QueryResponse(
        results=[
            QueryResultOut(
                id=r.id,
                score=r.score,
                metadata=r.metadata,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in results
        ]
    )


@router.post("/{name}/hybrid-query", response_model=QueryResponse)
async def hybrid_query(
    body: HybridQueryRequest,
    access: CollectionAccess = Depends(require_collection_permission(Permission.VECTOR_READ)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> QueryResponse:
    """Hybrid dense+keyword search. Input contract per backend capability
    (see GET /capabilities): Qdrant requires ``sparse_vector``
    (422 VECTOR_SPARSE_REQUIRED), Weaviate requires ``query_text``, Chroma
    raises 400 VALIDATION_UNSUPPORTED_OPERATION. ``alpha`` fuses the two
    sides (0.0 = pure keyword, 1.0 = pure dense)."""
    results = await SearchService(session).hybrid(
        principal,
        access=access,
        vector=body.vector,
        sparse_vector=(
            SparseVector(indices=body.sparse_vector.indices, values=body.sparse_vector.values)
            if body.sparse_vector is not None
            else None
        ),
        query_text=body.query_text,
        alpha=body.alpha,
        top_k=body.top_k,
        filters=body.filters,
    )
    return QueryResponse(
        results=[
            QueryResultOut(
                id=r.id,
                score=r.score,
                metadata=r.metadata,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in results
        ]
    )
