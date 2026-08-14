"""100k-vector ingest soak — the Chroma throughput-floor validation.

From the batch-throughput design (2026-08-14): Chroma is the platform's
throughput floor (~10–30 s for 100k records via chunked ``batch_upsert``), and
the adapter's chunking contract (100–1k per request) must hold under load. The
soak runs through the real container-backed adapter with ``chunk_size=1000``
(100 requests), asserting exact count and a CI-safe wall-clock budget. The
same path is re-validated end-to-end when the Phase 6 async batch job lands.
"""

import time
import uuid
from datetime import UTC, datetime

import pytest

from app.adapters.base import VectorRecord
from app.adapters.chroma_adapter import ChromaAdapter
from app.adapters.registry import registry

SOAK_SIZE = 100_000
SOAK_DIM = 8
# The throughput analysis models ~10-30 s for 100k on Chroma. The CI-safe
# bound is generous enough to absorb slow CI runners but still catches a
# throughput collapse (e.g. an accidental per-record round trip).
SOAK_BUDGET_SECONDS = 180


@pytest.mark.soak
async def test_100k_ingest_soak(chroma_backend: None) -> None:
    adapter = registry.get("chroma")
    assert isinstance(adapter, ChromaAdapter)
    phys = f"col_soak_{uuid.uuid4().hex[:8]}"
    await adapter.create_collection(name=phys, dimension=SOAK_DIM, distance_metric="cosine")
    try:
        now = datetime.now(UTC)
        records = [
            VectorRecord(
                id=f"doc-{i}",
                vector=[float((i % 17) * 0.01) + 0.001] * SOAK_DIM,
                metadata={"_tenant_probe": "soak"},
                tenant_id="soak-tenant",
                created_at=now,
                updated_at=now,
            )
            for i in range(SOAK_SIZE)
        ]
        start = time.perf_counter()
        result = await adapter.batch_upsert(
            collection=phys, tenant_id="soak-tenant", records=records, chunk_size=1000
        )
        elapsed = time.perf_counter() - start
        assert result.ok == SOAK_SIZE and result.failed == 0
        count = await (await adapter._get_client()).get_collection(name=phys)
        assert await count.count() == SOAK_SIZE
        print(
            f"\n[soak] 100k x {SOAK_DIM}-dim ingest on chroma: "
            f"{elapsed:.1f}s ({SOAK_SIZE / elapsed:,.0f} rec/s) — "
            f"budget {SOAK_BUDGET_SECONDS}s"
        )
        assert elapsed < SOAK_BUDGET_SECONDS, f"soak exceeded budget: {elapsed:.1f}s"
    finally:
        await adapter.delete_collection(name=phys)
