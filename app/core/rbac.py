"""Tenant-level RBAC.

Roles: owner > admin > editor > viewer. Platform admins bypass all checks.
Resource-level grants (collection_permissions) can only *elevate* a user's
effective role on that resource, never downgrade the tenant role — the
effective role is the max of (tenant role, resource grant) by rank. The
DB wiring for collection grants lands with the collection routes (Phase 3);
resolve_permission takes the grant as an argument so the matrix is testable
in isolation.
"""

from enum import StrEnum

from app.core.security import Principal

VALID_ROLES = ("owner", "admin", "editor", "viewer")

_ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2, "owner": 3}


class Permission(StrEnum):
    TENANT_READ = "tenant:read"
    TENANT_MANAGE = "tenant:manage"  # members, api keys
    COLLECTION_READ = "collection:read"
    COLLECTION_WRITE = "collection:write"
    COLLECTION_DELETE = "collection:delete"
    VECTOR_READ = "vector:read"
    VECTOR_WRITE = "vector:write"
    VECTOR_DELETE = "vector:delete"
    JOB_READ = "job:read"


_VIEWER = frozenset(
    {
        Permission.TENANT_READ,
        Permission.COLLECTION_READ,
        Permission.VECTOR_READ,
        Permission.JOB_READ,
    }
)
_EDITOR = _VIEWER | {
    Permission.COLLECTION_WRITE,
    Permission.VECTOR_WRITE,
    Permission.VECTOR_DELETE,
}
_ADMIN = _EDITOR | {Permission.TENANT_MANAGE, Permission.COLLECTION_DELETE}

ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "viewer": _VIEWER,
    "editor": _EDITOR,
    "admin": _ADMIN,
    "owner": frozenset(Permission),
}


def has_permission(principal: Principal, permission: Permission) -> bool:
    if principal.is_platform_admin:
        return True
    return permission in ROLE_PERMISSIONS.get(principal.role, frozenset())


def effective_role(tenant_role: str, collection_grant: str | None) -> str:
    """Max of tenant role and resource grant by rank; unknown roles rank 0."""
    if collection_grant in _ROLE_RANK and _ROLE_RANK[collection_grant] > _ROLE_RANK.get(
        tenant_role, -1
    ):
        return collection_grant
    return tenant_role if tenant_role in _ROLE_RANK else "viewer"


def resolve_permission(
    principal: Principal, permission: Permission, collection_grant: str | None = None
) -> bool:
    """Check a permission, honoring a resource-level grant if present.

    The grant can elevate but never downgrade (effective role = max by rank);
    platform admins always pass.
    """
    if principal.is_platform_admin:
        return True
    role = effective_role(principal.role, collection_grant)
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
