from app.core.rbac import (
    ROLE_PERMISSIONS,
    Permission,
    effective_role,
    has_permission,
    resolve_permission,
)
from app.core.security import Principal


def test_roles_grant_expected_permissions() -> None:
    expected = {
        "viewer": {
            Permission.TENANT_READ,
            Permission.COLLECTION_READ,
            Permission.VECTOR_READ,
            Permission.JOB_READ,
        },
        "editor": {
            Permission.TENANT_READ,
            Permission.COLLECTION_READ,
            Permission.COLLECTION_WRITE,
            Permission.VECTOR_READ,
            Permission.VECTOR_WRITE,
            Permission.VECTOR_DELETE,
            Permission.JOB_READ,
        },
        "admin": {
            Permission.TENANT_READ,
            Permission.TENANT_MANAGE,
            Permission.COLLECTION_READ,
            Permission.COLLECTION_WRITE,
            Permission.COLLECTION_DELETE,
            Permission.VECTOR_READ,
            Permission.VECTOR_WRITE,
            Permission.VECTOR_DELETE,
            Permission.JOB_READ,
        },
        "owner": set(Permission),
    }
    for role, perms in expected.items():
        assert ROLE_PERMISSIONS[role] == frozenset(perms), role


def test_permissions_are_monotonic_by_role() -> None:
    for higher, lower in [("owner", "admin"), ("admin", "editor"), ("editor", "viewer")]:
        assert ROLE_PERMISSIONS[lower] <= ROLE_PERMISSIONS[higher]


def test_viewer_cannot_write() -> None:
    principal = Principal(user_id="u", tenant_id="t", role="viewer")
    assert has_permission(principal, Permission.VECTOR_WRITE) is False
    assert has_permission(principal, Permission.COLLECTION_WRITE) is False


def test_platform_admin_bypasses_all() -> None:
    principal = Principal(user_id="u", tenant_id="t", role="viewer", is_platform_admin=True)
    assert has_permission(principal, Permission.TENANT_MANAGE) is True
    assert has_permission(principal, Permission.COLLECTION_DELETE) is True


def test_unknown_role_denies() -> None:
    principal = Principal(user_id="u", tenant_id="t", role="superuser")
    assert has_permission(principal, Permission.VECTOR_READ) is False


def test_resource_grant_elevates_tenant_role() -> None:
    # viewer on the tenant, editor on one collection -> can write that collection.
    principal = Principal(user_id="u", tenant_id="t", role="viewer")
    assert resolve_permission(principal, Permission.COLLECTION_WRITE) is False
    assert (
        resolve_permission(principal, Permission.COLLECTION_WRITE, collection_grant="editor")
        is True
    )
    # ...but not beyond the grant.
    assert (
        resolve_permission(principal, Permission.COLLECTION_DELETE, collection_grant="editor")
        is False
    )


def test_resource_grant_never_downgrades_tenant_role() -> None:
    principal = Principal(user_id="u", tenant_id="t", role="admin")
    assert (
        resolve_permission(principal, Permission.COLLECTION_DELETE, collection_grant="viewer")
        is True
    )


def test_effective_role_takes_max_of_tenant_and_grant() -> None:
    assert effective_role("viewer", "editor") == "editor"
    assert effective_role("admin", "viewer") == "admin"
    assert effective_role("editor", None) == "editor"


def test_unknown_grant_role_ignored() -> None:
    principal = Principal(user_id="u", tenant_id="t", role="viewer")
    assert resolve_permission(principal, Permission.VECTOR_READ, collection_grant="bogus") is True
    assert resolve_permission(principal, Permission.VECTOR_WRITE, collection_grant="bogus") is False
