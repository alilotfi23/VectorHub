"""Shared e2e harness for the Layer-3 cross-tenant isolation suite.

Generic setup helpers (register tenants, mint JWT/API-key principal
credentials, seed registry rows, provision members) plus the no-oracle
probe discipline every isolation case must follow. Phase 3's
vector-isolation e2e cases should import from here instead of redefining —
the no-existence-oracle contract is enforced by construction in
_assert_no_existence_oracle, so a new case can't forget half of it.

The helpers assume the ASGI app + session override are wired via the
`client` fixture in tests/e2e/conftest.py (migrated real Postgres).
"""

import uuid
from typing import Any, cast

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Collection

API = "/api/v1"
AUTH_STYLES = ("jwt", "api_key")


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _unique_email(local: str = "user") -> str:
    # Suffix goes in the local part: EmailStr validates the domain strictly.
    return f"{local}-{uuid.uuid4().hex[:10]}@example.com"


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(client: AsyncClient, tag: str = "user") -> dict[str, Any]:
    body = {
        "email": _unique_email(tag),
        "password": "password-123",
        "tenant_name": _unique(tag),
    }
    resp = await client.post(f"{API}/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    return cast(dict[str, Any], resp.json())


async def _principal_headers(client: AsyncClient, reg: dict[str, Any], auth: str) -> dict[str, str]:
    """Per-tenant principal credentials: a Bearer token (JWT) or a per-tenant
    API key minted at owner rank (the grants surface requires TENANT_MANAGE)
    — the design doc's two-principal-types requirement."""
    if auth == "jwt":
        return _auth_headers(reg["access_token"])
    created = await client.post(
        f"{API}/api-keys",
        json={"name": f"iso-{uuid.uuid4().hex[:10]}", "role": "owner"},
        headers=_auth_headers(reg["access_token"]),
    )
    assert created.status_code == 201, created.text
    return {"X-API-Key": created.json()["key"]}


async def _seed_collection(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: str, name: str
) -> None:
    # The create-collection route lands in Phase 3; seed the registry row
    # directly like the integration API tests do.
    async with session_factory() as session:
        collection = Collection(
            tenant_id=tenant_id,
            name=name,
            backend="chroma",
            dimension=8,
            distance_metric="cosine",
            physical_name=f"col_{uuid.uuid4().hex[:12]}",
        )
        session.add(collection)
        await session.commit()


async def _provision_member(
    client: AsyncClient, headers: dict[str, str], tenant_id: str
) -> dict[str, Any]:
    resp = await client.post(
        f"{API}/tenants/{tenant_id}/members",
        json={"email": _unique_email("member"), "password": "password-123"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return cast(dict[str, Any], resp.json())


async def _no_oracle_404(
    client: AsyncClient,
    method: str,
    path: str,
    headers: dict[str, str],
    *,
    expected: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fire one cross-tenant no-oracle probe: assert the fail-closed 404
    with the expected error_code and return the body so callers can compare
    probes byte-for-byte. Every isolation 404 assertion flows through here,
    so the suite's no-oracle discipline is enforced in one place."""
    resp = await client.request(method, path, json=body, headers=headers)
    assert resp.status_code == 404, f"{method} {path}: {resp.status_code} {resp.text}"
    assert resp.json()["error_code"] == expected
    return cast(dict[str, Any], resp.json())


async def _assert_no_existence_oracle(
    client: AsyncClient,
    *,
    method: str,
    real_path: str,
    missing_path: str,
    headers: dict[str, str],
    expected: str,
    body: dict[str, Any] | None = None,
) -> None:
    """Prove responses can't act as an existence oracle: a probe of a target
    that exists in another tenant and a probe of a target that exists nowhere
    must be byte-identical fail-closed 404s with the expected error_code.
    Byte-identity is enforced by construction — both probes flow through
    _no_oracle_404 and their bodies are compared here, so no caller can
    forget the comparison."""
    real = await _no_oracle_404(client, method, real_path, headers, expected=expected, body=body)
    missing = await _no_oracle_404(
        client, method, missing_path, headers, expected=expected, body=body
    )
    assert real == missing
