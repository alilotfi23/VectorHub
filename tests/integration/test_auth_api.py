import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.session import get_session
from app.main import app

API = "/api/v1"


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _unique_email(local: str = "user") -> str:
    # Suffix goes in the local part: EmailStr validates the domain strictly.
    return f"{local}-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _register(
    client: AsyncClient,
    tag: str = "user",
    *,
    email: str | None = None,
    tenant_name: str | None = None,
) -> dict[str, Any]:
    body = {
        "email": email or _unique_email(tag),
        "password": "password-123",
        "tenant_name": tenant_name or _unique(tag),
    }
    resp = await client.post(f"{API}/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    return cast(dict[str, Any], resp.json())


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_register_login_me_flow(client: AsyncClient) -> None:
    reg = await _register(client)
    assert reg["token_type"] == "bearer"
    assert reg["expires_in"] == 15 * 60
    assert reg["user"]["role"] == "owner"

    me = await client.get(f"{API}/auth/me", headers=_auth_headers(reg["access_token"]))
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == reg["user"]["email"]
    assert body["tenant_id"] == reg["user"]["tenant_id"]
    assert body["tenant_name"]  # tenant name is only exposed on /auth/me

    login = await client.post(
        f"{API}/auth/login",
        json={"email": reg["user"]["email"], "password": "password-123"},
    )
    assert login.status_code == 200
    assert login.json()["access_token"]


async def test_login_failures_are_indistinguishable(client: AsyncClient) -> None:
    reg = await _register(client)
    wrong = await client.post(
        f"{API}/auth/login",
        json={"email": reg["user"]["email"], "password": "wrong-password"},
    )
    unknown = await client.post(
        f"{API}/auth/login",
        json={"email": _unique_email("nobody"), "password": "password-123"},
    )
    for resp in (wrong, unknown):
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "AUTH_INVALID_CREDENTIALS"


async def test_refresh_rotation_and_replay(client: AsyncClient) -> None:
    reg = await _register(client)
    fresh = await client.post(f"{API}/auth/refresh", json={"refresh_token": reg["refresh_token"]})
    assert fresh.status_code == 200
    new_token = fresh.json()["refresh_token"]
    assert new_token != reg["refresh_token"]

    replay = await client.post(f"{API}/auth/refresh", json={"refresh_token": reg["refresh_token"]})
    assert replay.status_code == 401
    assert replay.json()["error_code"] == "AUTH_TOKEN_REVOKED"

    again = await client.post(f"{API}/auth/refresh", json={"refresh_token": new_token})
    assert again.status_code == 200


async def test_logout_revokes_both_tokens(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])

    # Logout requires the bearer access token and kills both credentials:
    # the refresh token in the body and the presented access token's jti.
    out = await client.post(
        f"{API}/auth/logout", json={"refresh_token": reg["refresh_token"]}, headers=headers
    )
    assert out.status_code == 204

    # The access token dies immediately at the auth boundary.
    me = await client.get(f"{API}/auth/me", headers=headers)
    assert me.status_code == 401
    assert me.json()["error_code"] == "AUTH_TOKEN_REVOKED"

    after = await client.post(f"{API}/auth/refresh", json={"refresh_token": reg["refresh_token"]})
    assert after.status_code == 401
    assert after.json()["error_code"] == "AUTH_TOKEN_REVOKED"


async def test_me_requires_valid_credential(client: AsyncClient) -> None:
    assert (await client.get(f"{API}/auth/me")).status_code == 401
    bad = await client.get(f"{API}/auth/me", headers=_auth_headers("garbage"))
    assert bad.status_code == 401
    assert bad.json()["error_code"] == "AUTH_INVALID_CREDENTIALS"


async def test_register_validation(client: AsyncClient) -> None:
    short = await client.post(
        f"{API}/auth/register",
        json={"email": _unique_email("a"), "password": "short", "tenant_name": "t"},
    )
    assert short.status_code == 422
    bad_email = await client.post(
        f"{API}/auth/register",
        json={"email": "not-an-email", "password": "password-123", "tenant_name": "t"},
    )
    assert bad_email.status_code == 422


async def test_duplicate_email_409(client: AsyncClient) -> None:
    reg = await _register(client)
    dup = await client.post(
        f"{API}/auth/register",
        json={
            "email": reg["user"]["email"],
            "password": "password-123",
            "tenant_name": _unique("other"),
        },
    )
    assert dup.status_code == 409
    assert dup.json()["error_code"] == "AUTH_EMAIL_TAKEN"


async def test_api_key_lifecycle_over_api(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])

    created = await client.post(f"{API}/api-keys", json={"name": "ci-robot"}, headers=headers)
    assert created.status_code == 201
    body = created.json()
    assert body["key"].startswith("vhk_")
    assert body["prefix"] == body["key"][:12]
    assert body["role"] == "editor"

    listed = await client.get(f"{API}/api-keys", headers=headers)
    assert listed.status_code == 200
    listed_body = listed.json()
    assert [k["name"] for k in listed_body["items"]] == ["ci-robot"]
    assert listed_body["total"] == 1
    assert listed_body["next_cursor"] is None
    assert "key" not in listed_body["items"][0]  # plaintext is never retrievable again

    # Key authenticates for tenant reads (editor -> tenant:read).
    tenant_id = reg["user"]["tenant_id"]
    key_read = await client.get(f"{API}/tenants/{tenant_id}", headers={"X-API-Key": body["key"]})
    assert key_read.status_code == 200

    revoked = await client.delete(f"{API}/api-keys/{body['id']}", headers=headers)
    assert revoked.status_code == 204
    after = await client.get(f"{API}/tenants/{tenant_id}", headers={"X-API-Key": body["key"]})
    assert after.status_code == 401
    assert after.json()["error_code"] == "AUTH_INVALID_CREDENTIALS"


async def test_api_key_management_requires_admin(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    reg = await _register(client)
    # Provision a viewer member directly in the DB and mint a viewer token;
    # using the member-management API would cover the same ground twice.
    async with session_factory() as session:
        from app.core.security import hash_password
        from app.db.models import User

        viewer = User(
            tenant_id=reg["user"]["tenant_id"],
            email=_unique("viewer@example.com"),
            password_hash=hash_password("password-123"),
            role="viewer",
        )
        session.add(viewer)
        await session.commit()
        viewer_id = viewer.id

    from app.core.security import create_access_token

    viewer_token = create_access_token(viewer_id, reg["user"]["tenant_id"], "viewer", False)
    resp = await client.post(
        f"{API}/api-keys", json={"name": "x"}, headers=_auth_headers(viewer_token)
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_INSUFFICIENT_SCOPE"

    # The owner (admin-equivalent) can manage.
    ok = await client.post(
        f"{API}/api-keys", json={"name": "y"}, headers=_auth_headers(reg["access_token"])
    )
    assert ok.status_code == 201


async def test_tenant_access_and_isolation(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    tenant_id = reg["user"]["tenant_id"]

    me = await client.get(f"{API}/auth/me", headers=headers)
    tenant_name = me.json()["tenant_name"]

    own = await client.get(f"{API}/tenants/{tenant_id}", headers=headers)
    assert own.status_code == 200
    assert own.json()["name"] == tenant_name

    foreign = await _register(client, "other")
    cross = await client.get(f"{API}/tenants/{foreign['user']['tenant_id']}", headers=headers)
    assert cross.status_code == 404
    assert cross.json()["error_code"] == "TENANT_NOT_FOUND"

    missing = await client.get(f"{API}/tenants/does-not-exist", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "TENANT_NOT_FOUND"


async def test_member_management_flow(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    tenant_id = reg["user"]["tenant_id"]

    created = await client.post(
        f"{API}/tenants/{tenant_id}/members",
        json={"email": _unique_email("dev"), "password": "password-123", "role": "editor"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    member = created.json()
    assert member["role"] == "editor"
    assert "password" not in member

    listed = await client.get(f"{API}/tenants/{tenant_id}/members", headers=headers)
    assert listed.status_code == 200
    body = listed.json()
    assert {m["role"] for m in body["items"]} == {"owner", "editor"}
    assert body["total"] == 2
    assert body["next_cursor"] is None
    # Rank-ordered: the owner lists before the provisioned editor.
    assert [m["role"] for m in body["items"]] == ["owner", "editor"]

    # The provisioned member can log in and read the tenant.
    member_login = await client.post(
        f"{API}/auth/login", json={"email": member["email"], "password": "password-123"}
    )
    assert member_login.status_code == 200
    member_headers = _auth_headers(member_login.json()["access_token"])
    read = await client.get(f"{API}/tenants/{tenant_id}", headers=member_headers)
    assert read.status_code == 200

    # An editor cannot manage members.
    deny = await client.post(
        f"{API}/tenants/{tenant_id}/members",
        json={"email": _unique_email("x"), "password": "password-123"},
        headers=member_headers,
    )
    assert deny.status_code == 403
    assert deny.json()["error_code"] == "AUTH_INSUFFICIENT_SCOPE"

    # The owner demotes the member.
    changed = await client.patch(
        f"{API}/tenants/{tenant_id}/members/{member['id']}",
        json={"role": "viewer"},
        headers=headers,
    )
    assert changed.status_code == 200
    assert changed.json()["role"] == "viewer"


async def test_member_ops_cross_tenant_404(client: AsyncClient) -> None:
    reg = await _register(client)
    other = await _register(client, "other")
    headers = _auth_headers(reg["access_token"])
    foreign_id = other["user"]["tenant_id"]

    for method, path, body in [
        ("GET", f"{API}/tenants/{foreign_id}/members", None),
        (
            "POST",
            f"{API}/tenants/{foreign_id}/members",
            {"email": _unique_email("x"), "password": "password-123"},
        ),
    ]:
        resp = await client.request(method, path, json=body, headers=headers)
        assert resp.status_code == 404, resp.text
        assert resp.json()["error_code"] == "TENANT_NOT_FOUND"


async def test_last_owner_demotion_409(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    resp = await client.patch(
        f"{API}/tenants/{reg['user']['tenant_id']}/members/{reg['user']['id']}",
        json={"role": "viewer"},
        headers=headers,
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "TENANT_LAST_OWNER"


async def test_create_tenant_requires_platform_admin(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    resp = await client.post(f"{API}/tenants", json={"name": _unique("nope")}, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "AUTH_INSUFFICIENT_SCOPE"


async def test_platform_admin_can_create_tenant(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_email = _unique_email("platform-admin")
    monkeypatch.setenv("BOOTSTRAP_PLATFORM_ADMIN_EMAILS", admin_email)
    get_settings.cache_clear()
    try:
        reg = await _register(client, email=admin_email)
    finally:
        get_settings.cache_clear()
    assert reg["user"]["email"] == admin_email
    assert reg["user"]["is_platform_admin"] is True

    resp = await client.post(
        f"{API}/tenants",
        json={"name": _unique("provisioned")},
        headers=_auth_headers(reg["access_token"]),
    )
    assert resp.status_code == 201
    assert resp.json()["name"].startswith("provisioned-")


async def test_forged_tenant_fields_rejected_everywhere(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Every request-body endpoint rejects a forged tenant_id at the schema
    (422) — the wire-level half of the isolation contract; the schema-level
    half is tests/unit/test_schemas.py."""
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    owner_id = reg["user"]["id"]
    tenant_id = reg["user"]["tenant_id"]
    forged = {"tenant_id": "other-tenant", "is_platform_admin": True}

    # Seed a collection so the permissions PATCH's dependency resolves and
    # the body validation (the point under test) is what fires.
    from app.db.models import Collection

    async with session_factory() as session:
        session.add(
            Collection(
                tenant_id=tenant_id,
                name="products",
                backend="chroma",
                dimension=8,
                distance_metric="cosine",
                physical_name=f"col_{uuid.uuid4().hex[:12]}",
            )
        )
        await session.commit()

    # role is legitimate on member/api-key/grant envelopes, so it is forged
    # only on the auth endpoints that must not accept it.
    escalated_role = {"role": "owner"}
    cases = [
        (
            "POST",
            f"{API}/auth/register",
            {
                "email": _unique_email("f"),
                "password": "password-123",
                "tenant_name": _unique("t"),
                **forged,
                **escalated_role,
            },
            None,
        ),
        (
            "POST",
            f"{API}/auth/login",
            {
                "email": reg["user"]["email"],
                "password": "password-123",
                **forged,
                **escalated_role,
            },
            None,
        ),
        ("POST", f"{API}/auth/refresh", {"refresh_token": "tok", **forged, **escalated_role}, None),
        (
            "POST",
            f"{API}/tenants/{tenant_id}/members",
            {"email": _unique_email("m"), "password": "password-123", **forged},
            headers,
        ),
        (
            "PATCH",
            f"{API}/tenants/{tenant_id}/members/{owner_id}",
            {"role": "viewer", **forged},
            headers,
        ),
        ("POST", f"{API}/api-keys", {"name": "x", **forged}, headers),
        (
            "PATCH",
            f"{API}/collections/products/permissions",
            {"user_id": owner_id, "role": "viewer", **forged},
            headers,
        ),
    ]
    for method, path, body, hdrs in cases:
        resp = await client.request(method, path, json=body, headers=hdrs)
        assert resp.status_code == 422, f"{method} {path}: {resp.status_code} {resp.text}"


async def test_tenant_create_rejects_forged_fields(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_email = _unique_email("platform-admin")
    monkeypatch.setenv("BOOTSTRAP_PLATFORM_ADMIN_EMAILS", admin_email)
    get_settings.cache_clear()
    try:
        reg = await _register(client, email=admin_email)
    finally:
        get_settings.cache_clear()
    assert reg["user"]["is_platform_admin"] is True

    resp = await client.post(
        f"{API}/tenants",
        json={
            "name": _unique("t"),
            "tenant_id": "other-tenant",
            "role": "owner",
            "is_platform_admin": True,
        },
        headers=_auth_headers(reg["access_token"]),
    )
    assert resp.status_code == 422, resp.text


async def test_member_list_pagination_over_api(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    tenant_id = reg["user"]["tenant_id"]

    member_ids: dict[str, str] = {}
    for tag in ("e1", "v1", "v2"):
        created = await client.post(
            f"{API}/tenants/{tenant_id}/members",
            json={"email": _unique_email(f"pg-{tag}"), "password": "password-123"},
            headers=headers,
        )
        assert created.status_code == 201
        member_ids[tag] = created.json()["id"]
    await client.patch(
        f"{API}/tenants/{tenant_id}/members/{member_ids['e1']}",
        json={"role": "editor"},
        headers=headers,
    )

    collected: list[tuple[str, str]] = []
    cursor: str | None = None
    pages = 0
    for _ in range(5):
        params: dict[str, str] = {"limit": "2"}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await client.get(
            f"{API}/tenants/{tenant_id}/members", params=params, headers=headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 4  # owner + 3 members
        collected.extend((m["role"], m["email"]) for m in body["items"])
        pages += 1
        if body["next_cursor"] is None:
            break
        cursor = body["next_cursor"]

    assert pages == 2  # 4 members, limit 2
    # Rank-ordered (owner then editor then viewers); the two viewers tie on
    # role, so email decides within the page — the no-overlap check proves
    # every member appears exactly once.
    assert [r for r, _ in collected] == ["owner", "editor", "viewer", "viewer"]
    assert len({email for _, email in collected}) == 4

    bad = await client.get(
        f"{API}/tenants/{tenant_id}/members", params={"cursor": "garbage"}, headers=headers
    )
    assert bad.status_code == 422
    assert bad.json()["error_code"] == "VALIDATION_INVALID_CURSOR"


async def test_api_key_list_pagination_over_api(client: AsyncClient) -> None:
    reg = await _register(client)
    headers = _auth_headers(reg["access_token"])
    for i in range(3):
        created = await client.post(f"{API}/api-keys", json={"name": f"k-{i}"}, headers=headers)
        assert created.status_code == 201

    collected: list[str] = []
    cursor: str | None = None
    pages = 0
    for _ in range(5):
        params: dict[str, str] = {"limit": "2"}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await client.get(f"{API}/api-keys", params=params, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 3
        collected.extend(k["name"] for k in body["items"])
        pages += 1
        if body["next_cursor"] is None:
            break
        cursor = body["next_cursor"]

    assert pages == 2  # 3 keys, limit 2 -> 2 + 1
    assert collected == ["k-2", "k-1", "k-0"]  # newest first

    bad = await client.get(f"{API}/api-keys", params={"cursor": "garbage"}, headers=headers)
    assert bad.status_code == 422
    assert bad.json()["error_code"] == "VALIDATION_INVALID_CURSOR"
