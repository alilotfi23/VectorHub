"""Layer 3 of the cross-tenant isolation suite — collection permissions, e2e.

Two real principals (registered tenants) drive the full HTTP surface of the
resource-level grant API, each case exercised with BOTH principal types the
design doc requires — JWT (Bearer) and per-tenant API keys. Mirrors cases
E1/E2/E3/E8 of the isolation design doc, adapted to a control-plane surface:
indistinguishable data (identical collection names), fail-closed 404s,
schema-level rejection of forged `tenant_id`, and a byte-identical negative
control proving responses can't act as an existence oracle. Plus key-death
cases: a revoked or expired API key is rejected at authentication (401) on
every path — own tenant, foreign tenant, and nonexistent names — with
byte-identical bodies, so a dead key carries no tenant identity at all. Plus
access-token death: logout deny-lists the presented JWT's jti, so the token
dies immediately — byte-identical 401 AUTH_TOKEN_REVOKED on own, foreign,
and nonexistent targets — while a fresh login (new jti) works. See
docs/superpowers/specs/2026-08-14-tenant-isolation-tests-design.md.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import get_session
from app.main import app
from tests.e2e.helpers import (
    API,
    AUTH_STYLES,
    _assert_no_existence_oracle,
    _auth_headers,
    _no_oracle_404,
    _principal_headers,
    _provision_member,
    _register,
    _seed_collection,
    _unique_email,
)


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


@pytest.mark.parametrize("auth", AUTH_STYLES)
async def test_e1_same_name_collision_grants_invisible_across_tenants(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], auth: str
) -> None:
    """A and B both have `products`; grants made in A are invisible from B on
    every path, and same-name ops never touch the other tenant's rows."""
    reg_a = await _register(client, "orga")
    reg_b = await _register(client, "orgb")
    headers_a = await _principal_headers(client, reg_a, auth)
    headers_b = await _principal_headers(client, reg_b, auth)
    tenant_a = reg_a["user"]["tenant_id"]
    tenant_b = reg_b["user"]["tenant_id"]

    await _seed_collection(session_factory, tenant_a, "products")
    await _seed_collection(session_factory, tenant_b, "products")
    member_a = await _provision_member(client, headers_a, tenant_a)
    member_b = await _provision_member(client, headers_b, tenant_b)

    # A grants editor to its member on `products`.
    granted = await client.patch(
        f"{API}/collections/products/permissions",
        json={"user_id": member_a["id"], "role": "editor"},
        headers=headers_a,
    )
    assert granted.status_code == 200, granted.text

    # B, with the identical name, sees nothing.
    b_list = await client.get(f"{API}/collections/products/permissions", headers=headers_b)
    assert b_list.status_code == 200
    body = b_list.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["next_cursor"] is None

    # B tries to revoke A's grantee by user_id on the same name: resolves to
    # B's own collection row, no-op 204, A's grant untouched.
    b_revoke = await client.delete(
        f"{API}/collections/products/permissions/{member_a['id']}", headers=headers_b
    )
    assert b_revoke.status_code == 204
    a_list = await client.get(f"{API}/collections/products/permissions", headers=headers_a)
    assert a_list.status_code == 200
    assert [g["user_id"] for g in a_list.json()["items"]] == [member_a["id"]]

    # B grants its own member on `products`: lands under B only.
    b_grant = await client.patch(
        f"{API}/collections/products/permissions",
        json={"user_id": member_b["id"], "role": "viewer"},
        headers=headers_b,
    )
    assert b_grant.status_code == 200
    b_list = await client.get(f"{API}/collections/products/permissions", headers=headers_b)
    assert [g["user_id"] for g in b_list.json()["items"]] == [member_b["id"]]
    a_list = await client.get(f"{API}/collections/products/permissions", headers=headers_a)
    assert [g["user_id"] for g in a_list.json()["items"]] == [member_a["id"]]


@pytest.mark.parametrize("auth", AUTH_STYLES)
async def test_e2_cross_tenant_ops_not_found_and_do_not_touch_data(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], auth: str
) -> None:
    """B's GET/PATCH/DELETE on A's distinctly-named collection all 404, and
    A's grants survive."""
    reg_a = await _register(client, "orga")
    reg_b = await _register(client, "orgb")
    headers_a = await _principal_headers(client, reg_a, auth)
    headers_b = await _principal_headers(client, reg_b, auth)
    tenant_a = reg_a["user"]["tenant_id"]

    await _seed_collection(session_factory, tenant_a, "a-only")
    member_a = await _provision_member(client, headers_a, tenant_a)
    granted = await client.patch(
        f"{API}/collections/a-only/permissions",
        json={"user_id": member_a["id"], "role": "editor"},
        headers=headers_a,
    )
    assert granted.status_code == 200

    # B's GET, PATCH, and DELETE on A's collection: all COLLECTION_NOT_FOUND.
    for method, path, body in (
        ("GET", f"{API}/collections/a-only/permissions", None),
        (
            "PATCH",
            f"{API}/collections/a-only/permissions",
            {"user_id": member_a["id"], "role": "viewer"},
        ),
        ("DELETE", f"{API}/collections/a-only/permissions/{member_a['id']}", None),
    ):
        await _no_oracle_404(
            client, method, path, headers_b, expected="COLLECTION_NOT_FOUND", body=body
        )

    # A's grant survives every cross-tenant attempt.
    a_list = await client.get(f"{API}/collections/a-only/permissions", headers=headers_a)
    assert a_list.status_code == 200
    assert [g["user_id"] for g in a_list.json()["items"]] == [member_a["id"]]


@pytest.mark.parametrize("auth", AUTH_STYLES)
async def test_e3_forged_tenant_id_rejected_at_schema(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], auth: str
) -> None:
    """A PATCH body carrying `tenant_id` is rejected with 422 (the envelope
    forbids the field) — the forged value never reaches the service and no
    grant is created."""
    reg_a = await _register(client, "orga")
    reg_b = await _register(client, "orgb")
    headers_a = await _principal_headers(client, reg_a, auth)
    tenant_a = reg_a["user"]["tenant_id"]
    tenant_b = reg_b["user"]["tenant_id"]
    assert tenant_a != tenant_b

    await _seed_collection(session_factory, tenant_a, "products")
    member_a = await _provision_member(client, headers_a, tenant_a)

    forged = await client.patch(
        f"{API}/collections/products/permissions",
        json={"user_id": member_a["id"], "role": "editor", "tenant_id": tenant_b},
        headers=headers_a,
    )
    assert forged.status_code == 422, forged.text

    # Fail-closed: nothing was written.
    a_list = await client.get(f"{API}/collections/products/permissions", headers=headers_a)
    assert a_list.status_code == 200
    assert a_list.json()["items"] == []


@pytest.mark.parametrize("auth", AUTH_STYLES)
async def test_e8_negative_control_no_existence_oracle(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], auth: str
) -> None:
    """A tenant with no collections probes a name that exists in another
    tenant and a random name: byte-identical 404s."""
    reg_a = await _register(client, "orga")
    reg_c = await _register(client, "orgc")
    headers_a = await _principal_headers(client, reg_a, auth)
    headers_c = await _principal_headers(client, reg_c, auth)
    tenant_a = reg_a["user"]["tenant_id"]

    await _seed_collection(session_factory, tenant_a, "products")

    # A name that exists in another tenant and a name that exists nowhere:
    # byte-identical 404s by construction (see _assert_no_existence_oracle).
    await _assert_no_existence_oracle(
        client,
        method="GET",
        real_path=f"{API}/collections/products/permissions",
        missing_path=f"{API}/collections/{uuid.uuid4().hex}/permissions",
        headers=headers_c,
        expected="COLLECTION_NOT_FOUND",
    )

    # And A, for whom the collection genuinely exists, sees grants fine.
    a_list = await client.get(f"{API}/collections/products/permissions", headers=headers_a)
    assert a_list.status_code == 200


@pytest.mark.parametrize("auth", AUTH_STYLES)
async def test_forged_tenant_id_in_path_cannot_reach_foreign_members(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], auth: str
) -> None:
    """B — JWT or API key — putting A's tenant id in the path gets the
    no-oracle TENANT_NOT_FOUND on every member-surface op (GET/POST/PATCH
    on members, plus GET /tenants/{id}), byte-identical to a nonexistent
    id, and A's member roles (the tenant-level grants) survive untouched.
    The path-level twin of E3, which forges tenant_id in the body."""
    reg_a = await _register(client, "orga")
    reg_b = await _register(client, "orgb")
    headers_a = await _principal_headers(client, reg_a, auth)
    headers_b = await _principal_headers(client, reg_b, auth)
    tenant_a = reg_a["user"]["tenant_id"]
    tenant_b = reg_b["user"]["tenant_id"]
    assert tenant_a != tenant_b

    # A provisions a member and grants a role on the tenant (editor).
    member_a = await _provision_member(client, headers_a, tenant_a)
    promoted = await client.patch(
        f"{API}/tenants/{tenant_a}/members/{member_a['id']}",
        json={"role": "editor"},
        headers=headers_a,
    )
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "editor"

    # B's read of A's member directory (the grant state): 404, no oracle.
    await _no_oracle_404(
        client,
        "GET",
        f"{API}/tenants/{tenant_a}/members",
        headers_b,
        expected="TENANT_NOT_FOUND",
    )

    # B forges A's tenant id in the path: every write op 404s too.
    for method, path, body in (
        (
            "POST",
            f"{API}/tenants/{tenant_a}/members",
            {"email": _unique_email("intruder"), "password": "password-123"},
        ),
        (
            "PATCH",
            f"{API}/tenants/{tenant_a}/members/{member_a['id']}",
            {"role": "viewer"},
        ),
        ("GET", f"{API}/tenants/{tenant_a}", None),
    ):
        await _no_oracle_404(
            client, method, path, headers_b, expected="TENANT_NOT_FOUND", body=body
        )

    # Byte-identical to a nonexistent tenant id: no existence oracle (probed
    # fresh after the writes, so the failures are stable post-attempt).
    await _assert_no_existence_oracle(
        client,
        method="GET",
        real_path=f"{API}/tenants/{tenant_a}/members",
        missing_path=f"{API}/tenants/{uuid.uuid4().hex}/members",
        headers=headers_b,
        expected="TENANT_NOT_FOUND",
    )

    # A's roles survive untouched: still the owner and the editor member.
    a_list = await client.get(f"{API}/tenants/{tenant_a}/members", headers=headers_a)
    assert a_list.status_code == 200
    body = a_list.json()
    assert body["total"] == 2  # no intruder was added
    by_id = {m["id"]: m["role"] for m in body["items"]}
    assert by_id[member_a["id"]] == "editor"  # B's demotion never landed


KILL_METHODS = ("revoke", "expire")


@pytest.mark.parametrize("kill", KILL_METHODS)
async def test_killed_key_loses_all_access_immediately(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], kill: str
) -> None:
    """A revoked or expired API key is rejected at authentication (401) the
    moment it dies — on its own tenant's resources, on another tenant's
    resources, and on a name that exists nowhere, with byte-identical error
    bodies. A dead key carries no tenant identity at all, so it can neither
    reach foreign data nor act as an existence oracle."""
    reg_a = await _register(client, "orga")
    reg_b = await _register(client, "orgb")
    tenant_a = reg_a["user"]["tenant_id"]
    tenant_b = reg_b["user"]["tenant_id"]
    await _seed_collection(session_factory, tenant_a, "a-only")
    await _seed_collection(session_factory, tenant_b, "b-only")

    created = await client.post(
        f"{API}/api-keys",
        json={
            "name": f"iso-{uuid.uuid4().hex[:10]}",
            "role": "owner",
            **({"expires_at": "2020-01-01T00:00:00Z"} if kill == "expire" else {}),
        },
        headers=_auth_headers(reg_a["access_token"]),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    key_headers = {"X-API-Key": body["key"]}

    if kill == "revoke":
        # Prove the key was live a moment ago.
        before = await client.get(f"{API}/collections/a-only/permissions", headers=key_headers)
        assert before.status_code == 200
        revoked = await client.delete(
            f"{API}/api-keys/{body['id']}", headers=_auth_headers(reg_a["access_token"])
        )
        assert revoked.status_code == 204
    else:
        # An expiry in the past makes the key dead on arrival (the schema
        # deliberately doesn't forbid it — a TTL may elapse in transit).
        assert body["expires_at"] is not None

    # Dead on every path — own tenant, foreign tenant, nowhere — with
    # byte-identical 401s: the key carries no tenant identity anymore.
    own = await client.get(f"{API}/collections/a-only/permissions", headers=key_headers)
    other = await client.get(f"{API}/collections/b-only/permissions", headers=key_headers)
    nowhere = await client.get(
        f"{API}/collections/{uuid.uuid4().hex}/permissions", headers=key_headers
    )
    for resp in (own, other, nowhere):
        assert resp.status_code == 401, resp.text
        assert resp.json()["error_code"] == "AUTH_INVALID_CREDENTIALS"
    assert own.json() == other.json() == nowhere.json()

    # Repeated presentation stays dead — no revivification window.
    again = await client.get(f"{API}/collections/a-only/permissions", headers=key_headers)
    assert again.status_code == 401


async def test_revoked_key_loses_tenant_role_immediately(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Revocation kills the key's tenant role at the authentication gate,
    with no timing window: the request immediately after the revoke 204 —
    on a read surface that ANY tenant role, even viewer, would pass — is
    401. The role is gone entirely, not degraded, and never reaches the
    resolve-once gates again."""
    reg = await _register(client, "org")
    tenant_id = reg["user"]["tenant_id"]
    await _seed_collection(session_factory, tenant_id, "own")
    headers = _auth_headers(reg["access_token"])

    created = await client.post(
        f"{API}/api-keys",
        json={"name": f"iso-{uuid.uuid4().hex[:10]}", "role": "owner"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    key_headers = {"X-API-Key": body["key"]}

    # Live: the owner role passes the resolve-once gates on both surfaces —
    # the tenant read (any role passes) and the manage-gated grant list.
    read = await client.get(f"{API}/tenants/{tenant_id}", headers=key_headers)
    assert read.status_code == 200
    grants = await client.get(f"{API}/collections/own/permissions", headers=key_headers)
    assert grants.status_code == 200

    revoked = await client.delete(f"{API}/api-keys/{body['id']}", headers=headers)
    assert revoked.status_code == 204

    # The very next request is 401 even on the read surface a viewer would
    # pass — the role is gone entirely at authentication, before any
    # resolve-once gate can consult it.
    read_after = await client.get(f"{API}/tenants/{tenant_id}", headers=key_headers)
    assert read_after.status_code == 401
    assert read_after.json()["error_code"] == "AUTH_INVALID_CREDENTIALS"
    grants_after = await client.get(f"{API}/collections/own/permissions", headers=key_headers)
    assert grants_after.status_code == 401


async def test_logged_out_access_token_dies_immediately(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Logout deny-lists the presented access token's jti: the request
    immediately after the 204 — on any target, own tenant or foreign — is a
    401 AUTH_TOKEN_REVOKED, byte-identical everywhere (a dead credential
    can't act as an existence oracle). Revocation is per-jti, not per-user:
    a fresh login works, and other tenants' tokens are untouched."""
    reg_a = await _register(client, "orga")
    reg_b = await _register(client, "orgb")
    headers_a = _auth_headers(reg_a["access_token"])
    headers_b = _auth_headers(reg_b["access_token"])
    tenant_a = reg_a["user"]["tenant_id"]
    tenant_b = reg_b["user"]["tenant_id"]

    await _seed_collection(session_factory, tenant_a, "a-only")
    await _seed_collection(session_factory, tenant_b, "b-only")

    # Live right now: A's token passes on A's own resource.
    before = await client.get(f"{API}/collections/a-only/permissions", headers=headers_a)
    assert before.status_code == 200

    # Logout revokes both the refresh token and the presented access token.
    out = await client.post(
        f"{API}/auth/logout",
        json={"refresh_token": reg_a["refresh_token"]},
        headers=headers_a,
    )
    assert out.status_code == 204, out.text

    # Dead immediately on every path — own tenant, foreign tenant, nowhere —
    # with byte-identical 401 AUTH_TOKEN_REVOKED bodies: rejected at the auth
    # boundary before any resolve-once gate can consult it.
    own = await client.get(f"{API}/collections/a-only/permissions", headers=headers_a)
    other = await client.get(f"{API}/collections/b-only/permissions", headers=headers_a)
    nowhere = await client.get(
        f"{API}/collections/{uuid.uuid4().hex}/permissions", headers=headers_a
    )
    for resp in (own, other, nowhere):
        assert resp.status_code == 401, resp.text
        assert resp.json()["error_code"] == "AUTH_TOKEN_REVOKED"
    assert own.json() == other.json() == nowhere.json()

    # Revocation is per-jti: a fresh login issues a new jti that works...
    relogin = await client.post(
        f"{API}/auth/login",
        json={"email": reg_a["user"]["email"], "password": "password-123"},
    )
    assert relogin.status_code == 200
    fresh = await client.get(
        f"{API}/collections/a-only/permissions",
        headers=_auth_headers(relogin.json()["access_token"]),
    )
    assert fresh.status_code == 200

    # ...and B's session is untouched.
    b_ok = await client.get(f"{API}/collections/b-only/permissions", headers=headers_b)
    assert b_ok.status_code == 200
