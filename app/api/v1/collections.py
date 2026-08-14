from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_current_principal,
    require_collection_permission,
    require_permission,
)
from app.core.exceptions import AppError, ErrorCode
from app.core.rbac import Permission
from app.core.security import Principal
from app.db.session import get_session
from app.schemas.collections import (
    CollectionConfigUpdateRequest,
    CollectionCreateRequest,
    CollectionListResponse,
    CollectionPermissionListResponse,
    CollectionPermissionResponse,
    CollectionPermissionUpdateRequest,
    CollectionResponse,
)
from app.services.collection_service import (
    CollectionAccess,
    CollectionService,
    CollectionWithStatus,
)

router = APIRouter(prefix="/collections", tags=["collections"])


# Collection lifecycle + resource-level RBAC surface.


def _collection_response(item: CollectionWithStatus) -> CollectionResponse:
    collection = item.collection
    return CollectionResponse(
        name=collection.name,
        backend=collection.backend,
        dimension=collection.dimension,
        distance_metric=collection.distance_metric,
        metadata=collection.metadata_ or {},
        status=collection.status,
        backend_status=item.backend_status,
        created_at=collection.created_at,
        updated_at=collection.updated_at,
    )


@router.patch("/{name}/permissions", response_model=CollectionPermissionResponse)
async def update_collection_permissions(
    body: CollectionPermissionUpdateRequest,
    access: CollectionAccess = Depends(require_collection_permission(Permission.TENANT_MANAGE)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> CollectionPermissionResponse:
    grant = await CollectionService(session).grant_permission(
        principal, access=access, user_id=body.user_id, role=body.role
    )
    return CollectionPermissionResponse(
        collection_id=grant.collection_id,
        collection_name=access.collection.name,
        user_id=grant.user_id,
        role=grant.permission,
        created_at=grant.created_at,
    )


@router.get("/{name}/permissions", response_model=CollectionPermissionListResponse)
async def list_collection_permissions(
    limit: int = Query(default=50, ge=1, le=200, description="Page size"),
    cursor: str | None = Query(
        default=None, description="Opaque keyset cursor from a previous page"
    ),
    access: CollectionAccess = Depends(require_collection_permission(Permission.TENANT_MANAGE)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> CollectionPermissionListResponse:
    page = await CollectionService(session).list_permissions(
        principal, access=access, limit=limit, cursor=cursor
    )
    return CollectionPermissionListResponse(
        items=[
            CollectionPermissionResponse(
                collection_id=grant.collection_id,
                collection_name=access.collection.name,
                user_id=grant.user_id,
                role=grant.permission,
                created_at=grant.created_at,
            )
            for grant in page.items
        ],
        next_cursor=page.next_cursor,
        total=page.total,
    )


@router.delete("/{name}/permissions/{user_id}", status_code=204)
async def revoke_collection_permission(
    user_id: str,
    access: CollectionAccess = Depends(require_collection_permission(Permission.TENANT_MANAGE)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await CollectionService(session).revoke_permission(principal, access=access, user_id=user_id)
    return Response(status_code=204)


@router.post("", status_code=201, response_model=CollectionResponse)
async def create_collection(
    body: CollectionCreateRequest,
    principal: Principal = Depends(require_permission(Permission.COLLECTION_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> CollectionResponse:
    """Create a collection on a backend. ``tenant_id`` is the principal's —
    the envelope deliberately has no tenant/owner field (forged ones 422)."""
    collection = await CollectionService(session).create_collection(
        principal,
        name=body.name,
        backend=body.backend,
        dimension=body.dimension,
        distance_metric=body.distance_metric,
        metadata=body.metadata,
    )
    # Just created: the physical object exists by construction.
    return _collection_response(
        CollectionWithStatus(collection=collection, backend_status="exists")
    )


@router.get("", response_model=CollectionListResponse)
async def list_collections(
    limit: int = Query(default=50, ge=1, le=200, description="Page size"),
    cursor: str | None = Query(
        default=None, description="Opaque keyset cursor from a previous page"
    ),
    principal: Principal = Depends(require_permission(Permission.COLLECTION_READ)),
    session: AsyncSession = Depends(get_session),
) -> CollectionListResponse:
    """Tenant-scoped listing (isolation-suite E7) with per-collection
    backend_status on the read path."""
    page = await CollectionService(session).list_collections(principal, limit=limit, cursor=cursor)
    return CollectionListResponse(
        items=[_collection_response(item) for item in page.items],
        next_cursor=page.next_cursor,
        total=page.total,
    )


@router.get("/{name}", response_model=CollectionResponse)
async def get_collection(
    access: CollectionAccess = Depends(require_collection_permission(Permission.COLLECTION_READ)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> CollectionResponse:
    """GET one collection: tenant-scoped resolution (missing OR foreign is the
    same 404 — no existence oracle) plus backend_status."""
    item = await CollectionService(session).get_collection_with_status(principal, access=access)
    return _collection_response(item)


@router.delete("/{name}", status_code=204)
async def delete_collection(
    access: CollectionAccess = Depends(require_collection_permission(Permission.COLLECTION_DELETE)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Hard delete: the backend object and the registry row in one operation
    (destructive and immediate per the data-retention contract)."""
    await CollectionService(session).delete_collection(principal, access=access)
    return Response(status_code=204)


@router.patch("/{name}/config", response_model=CollectionResponse)
async def update_collection_config(
    body: CollectionConfigUpdateRequest,
    access: CollectionAccess = Depends(require_collection_permission(Permission.COLLECTION_WRITE)),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> CollectionResponse:
    """Mutate index config post-creation: only the backend's hot-mutable
    subset (CapabilityMatrix) applies; anything else is 409 REQUIRES_REINDEX
    with a stated next_step. On Chroma the subset is empty, so any config
    change 409s here."""
    collection = await CollectionService(session).update_config(
        principal, access=access, index_config=body.index_config
    )
    return _collection_response(
        CollectionWithStatus(collection=collection, backend_status="exists")
    )


@router.post("/{name}/reindex", status_code=501)
async def reindex_collection(
    access: CollectionAccess = Depends(require_collection_permission(Permission.COLLECTION_WRITE)),
) -> None:
    """Honest v1 stub: REQUIRES_REINDEX responses point here, but full
    reindex-as-a-job is a later phase (501 REINDEX_NOT_IMPLEMENTED)."""
    raise AppError(
        ErrorCode.REINDEX_NOT_IMPLEMENTED,
        "Full reindex is not implemented in v1",
        details={
            "message": "Reindex-as-a-job lands in a later phase; index config is "
            "creation-time on the currently supported backends."
        },
        status_code=501,
    )
