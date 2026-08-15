"""Phase 6 batch path + capabilities + audit-log read — integration tests.

The full data path against real components: NDJSON enqueued via the API is
staged on MinIO (BATCH_STORAGE_* env pointed at a MinIO testcontainer), the
worker task (driven in-process via its session/storage seams) streams it
back, validates per line, chunked-upserts through the real Chroma adapter,
and the jobs row + results object reflect the outcome. Also pins: content-type
enforcement, the enqueue-time quota, the whole-file-invalid failure, the
capabilities matrix shape, and the audit-log read showing middleware-recorded
failures.
"""

import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.job_service as job_service_module
import app.workers.batch as batch_worker
from app.core.config import get_settings
from app.db.session import get_session
from app.main import app
from app.services.batch_storage import BatchStorage


async def _noop_enqueue(job_id: str, payload_key: str) -> None:
    del job_id, payload_key


API = "/api/v1"
DIM = 8


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _unique_email(local: str = "user") -> str:
    return f"{local}-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
async def batch_storage(minio_url: str) -> AsyncGenerator[BatchStorage, None]:
    """Fresh bucket per test, exposed via BATCH_STORAGE_BUCKET so the route's
    JobService (env-default storage) and the worker seam (injected storage)
    share the same target — the exact keys the platform derives at runtime."""
    bucket = f"vhk-batch-{uuid.uuid4().hex[:8]}"
    os.environ["BATCH_STORAGE_BUCKET"] = bucket
    get_settings.cache_clear()
    try:
        yield BatchStorage(bucket=bucket)
    finally:
        os.environ.pop("BATCH_STORAGE_BUCKET", None)
        get_settings.cache_clear()


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    chroma_backend: None,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    # (The audit middleware's session factory is already pointed at the test
    # DB by the session_factory fixture — see conftest.)
    # No live arq worker in the integration layer: record/no-op the enqueue
    # (the real enqueue wiring is exercised in the e2e layer, which has Redis).
    monkeypatch.setattr(job_service_module, "default_enqueue", _noop_enqueue)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _register(client: AsyncClient, tag: str = "user") -> dict[str, Any]:
    resp = await client.post(
        f"{API}/auth/register",
        json={"email": _unique_email(tag), "password": "password-123", "tenant_name": _unique(tag)},
    )
    assert resp.status_code == 201, resp.text
    return cast(dict[str, Any], resp.json())


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _line(i: int, *, dim: int = DIM, tag: str = "batch") -> str:
    import json

    return json.dumps(
        {
            "id": f"doc-{i}",
            "vector": [float(((i + j) % 5) + 1) * 0.1 for j in range(dim)],
            "metadata": {"tag": tag, "seed": i},
        }
    )


async def _create_collection(client: AsyncClient, headers: dict[str, str]) -> str:
    name = _unique("batch")
    resp = await client.post(
        f"{API}/collections",
        json={"name": name, "backend": "chroma", "dimension": DIM, "distance_metric": "cosine"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return name


async def _enqueue(client: AsyncClient, headers: dict[str, str], name: str, body: str) -> Any:
    return await client.post(
        f"{API}/collections/{name}/vectors/batch",
        content=body,
        headers={**headers, "Content-Type": "application/x-ndjson"},
    )


async def test_batch_full_round_trip(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    batch_storage: BatchStorage,
) -> None:
    """Enqueue NDJSON -> worker ingests into the real backend -> GET /jobs
    reports succeeded with counts -> fetched vectors prove the ingest."""
    reg = await _register(client, "bat-full")
    headers = _auth_headers(reg["access_token"])
    name = await _create_collection(client, headers)

    payload = "".join(_line(i) + "\n" for i in range(5))
    resp = await _enqueue(client, headers, name, payload)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    # Drive the worker in-process (session + storage seams).
    batch_worker.set_session_factory(session_factory)
    batch_worker.set_storage(batch_storage)
    tenant_id = reg["user"]["tenant_id"]
    outcome = await batch_worker.run_batch_ingest({}, job_id, f"{tenant_id}/{job_id}.jsonl")
    assert outcome is not None and outcome["status"] == "succeeded"
    assert outcome["ok"] == 5 and outcome["failed"] == 0

    job = await client.get(f"{API}/jobs/{job_id}", headers=headers)
    assert job.status_code == 200, job.text
    body = job.json()
    assert body["status"] == "succeeded"
    assert body["ok"] == 5 and body["total"] == 5 and body["failed"] == 0

    fetched = await client.get(f"{API}/collections/{name}/vectors/doc-2", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["metadata"]["tag"] == "batch"

    # Results object exists on storage, per-vector ok rows.
    assert await batch_storage.head(f"{tenant_id}/{job_id}.results.jsonl")
    lines = [line async for line in batch_storage.iter_lines(f"{tenant_id}/{job_id}.results.jsonl")]
    assert len(lines) == 5


async def test_batch_whole_file_invalid(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    batch_storage: BatchStorage,
) -> None:
    """Zero valid lines -> JOB_PAYLOAD_INVALID (whole-file validation failure),
    distinct from per-vector outcomes."""
    reg = await _register(client, "bat-inv")
    headers = _auth_headers(reg["access_token"])
    name = await _create_collection(client, headers)

    payload = "\n".join(["not-json{", "", "still-not-json"]) + "\n"
    resp = await _enqueue(client, headers, name, payload)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    batch_worker.set_session_factory(session_factory)
    batch_worker.set_storage(batch_storage)
    tenant_id = reg["user"]["tenant_id"]
    outcome = await batch_worker.run_batch_ingest({}, job_id, f"{tenant_id}/{job_id}.jsonl")
    assert outcome is not None and outcome["status"] == "failed"
    assert "JOB_PAYLOAD_INVALID" in outcome["error"]

    job = await client.get(f"{API}/jobs/{job_id}", headers=headers)
    assert job.status_code == 200, job.text
    assert job.json()["status"] == "failed"
    assert "JOB_PAYLOAD_INVALID" in job.json()["error"]


async def test_batch_content_type_enforced(client: AsyncClient) -> None:
    reg = await _register(client, "bat-ct")
    headers = _auth_headers(reg["access_token"])
    name = await _create_collection(client, headers)
    resp = await client.post(
        f"{API}/collections/{name}/vectors/batch",
        content='{"id": "x"}\n',
        headers={**headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 415, resp.text


async def test_batch_quota_at_enqueue(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    batch_storage: BatchStorage,
) -> None:
    """max_concurrent_jobs_per_tenant is checked at enqueue time — the second
    outstanding job 429s TENANT_QUOTA_EXCEEDED."""
    monkeypatch.setattr(get_settings(), "max_concurrent_jobs_per_tenant", 1)
    reg = await _register(client, "bat-quota")
    headers = _auth_headers(reg["access_token"])
    name = await _create_collection(client, headers)

    first = await _enqueue(client, headers, name, _line(0) + "\n")
    assert first.status_code == 202, first.text
    second = await _enqueue(client, headers, name, _line(1) + "\n")
    assert second.status_code == 429, second.text
    assert second.json()["error_code"] == "TENANT_QUOTA_EXCEEDED"


async def test_capabilities_matrix(client: AsyncClient) -> None:
    """GET /capabilities exposes every registered backend with the canonical
    hybrid {mode, sparse_required} shape + tenancy_model + chunk sizing."""
    resp = await client.get(f"{API}/capabilities")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"chroma", "qdrant", "weaviate", "milvus"}
    assert body["chroma"]["tenancy_model"] == "collection-per-tenant"
    assert body["chroma"]["hybrid"] == {"mode": False, "sparse_required": False}
    assert body["qdrant"]["hybrid"] == {"mode": "sparse+vector", "sparse_required": True}
    assert body["weaviate"]["hybrid"] == {"mode": "text+vector", "sparse_required": False}
    assert body["milvus"]["hybrid"] == {"mode": "sparse+vector", "sparse_required": True}
    assert body["chroma"]["default_batch_chunk_size"] == 500
    assert body["milvus"]["default_batch_chunk_size"] == 5000
    assert body["qdrant"]["tenancy_model"] == "payload-partition"


async def test_audit_log_records_failed_write(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A failed mutating request with a decodable principal lands in
    audit_log, and GET /admin/audit-logs (admin/owner) reads it back."""
    reg = await _register(client, "bat-audit")
    headers = _auth_headers(reg["access_token"])
    name = await _create_collection(client, headers)

    # A failed write: vector dimension mismatch -> 422.
    bad = await client.post(
        f"{API}/collections/{name}/vectors",
        json={"vectors": [{"id": "x", "vector": [0.1, 0.2]}]},
        headers=headers,
    )
    assert bad.status_code == 422, bad.text

    logs = await client.get(f"{API}/admin/audit-logs", headers=headers)
    assert logs.status_code == 200, logs.text
    items = logs.json()["items"]
    assert any(
        item["result"] == "failure"
        and item["details"].get("error_code") == "VECTOR_DIMENSION_MISMATCH"
        for item in items
    )

    # Cross-tenant read: another principal's tenant-scoped view never shows
    # the first tenant's failure row (its own tenant.created rows are all it
    # sees — no cross-tenant leakage).
    other = await _register(client, "bat-audit2")
    other_headers = _auth_headers(other["access_token"])
    logs2 = await client.get(f"{API}/admin/audit-logs", headers=other_headers)
    assert logs2.status_code == 200, logs2.text
    assert not any(
        item["details"].get("error_code") == "VECTOR_DIMENSION_MISMATCH"
        for item in logs2.json()["items"]
    )
