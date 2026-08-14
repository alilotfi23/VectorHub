# Unified VectorHub Platform — Architecture Design Decisions

**Date:** 2026-08-14
**Status:** Approved (design session complete; implementation not started). Updated 2026-08-14: all five decisions have been **folded into `CLAUDE.md`**, which is now the authoritative operational spec.

This document records the architecture decisions made during the pre-build design session for the Unified VectorHub Platform API. The decisions themselves now live inline in `CLAUDE.md` (Tenancy Matrix, hybrid contract, batch data path, drift policy, audit immutability); this document remains the **rationale and knock-on analysis** behind them — read it for depth when working a phase, especially Phases 3–6.

**Operating context (drives every decision below):** the platform is a real SaaS product — external customers as tenants, tenant isolation is a **security boundary**, and backends may be cloud-managed (Qdrant Cloud, Weaviate Cloud, Zilliz) or self-hosted. Per the base spec, each backend name maps to exactly one configured instance (one client per backend, held for the app lifetime); per-tenant physical backend instances (`backend_instance_id`) remain out of scope for v1.

---

## 1. Decisions register

| # | Tension in base spec | Decision | Sections |
|---|----------------------|----------|----------|
| D1 | How tenant isolation maps onto 4 backends with different native tenancy | **Native tenancy matrix**: use each backend's native tenancy mechanism where it is strong (Weaviate tenants, Qdrant tenants), partitions for Milvus, per-tenant collections for Chroma; filter-based isolation only as a documented fallback | §2–3 |
| D2 | Hybrid search contract is undefined for a vectors-in platform | **Client-supplied sparse vectors** (+ optional `query_text`); single normalized `alpha` knob; capability-matrix-driven errors | §5 |
| D3 | 100k+ vector batches through `arq` would transit Redis (hundreds of MB per job) | **Object-storage staging**: request streams to MinIO/S3 as JSONL, job carries only `{job_id, object_key}` | §6 |
| D4 | No reconciliation defined between Postgres registry and backend state | **Observable non-goal**: tolerant lifecycle ops + `backend_status` on the read path; reconciliation explicitly deferred | §7 |
| D5 | "INSERT/SELECT only" on `audit_log` lacks a concrete mechanism | **Two Postgres roles (app/migrator) + all-role guard trigger**; threat model stated | §8 |

---

## 2. Tenant isolation model (D1)

Tenant isolation is a security boundary: it must be **enforced by the vector backend**, not by our filter code. A filter bug must not be able to leak tenant A's data to tenant B. The platform therefore uses each backend's native tenancy mechanism where one exists, and physical separation where it doesn't:

| Backend | Tenancy model | Isolation mechanism | Provisioning | Version gate |
|---------|---------------|---------------------|--------------|--------------|
| **Weaviate** | Native multi-tenancy | Tenant-enabled class (`multiTenancyConfig.enabled: true` at class creation); each tenant owns a dedicated shard; requests scoped by tenant name | `ensure_tenant`: idempotent `tenants.create` before first use | Weaviate ≥ 1.21 (tenant status ops) |
| **Qdrant** | Payload-partition + `is_tenant` index | **Drift, recorded when implemented (Phase 4): Qdrant removed its native tenant API (`multi_tenancy_config`/`create_tenant`) from client and server — verified against v1.19, the server 404s the old endpoints.** Current mechanism: `_vhk_tenant_id` payload field with a `keyword` index marked `is_tenant=True`; adapter always applies the tenant filter (no unscoped path) — isolation rests on the always-applied filter plus Qdrant's `is_tenant` storage co-location | `ensure_tenant`: no-op (index created with the collection) | Qdrant ≥ 1.10 (`is_tenant` support) |
| **Milvus** | Partition-per-tenant | One partition per tenant; inserts route by partition, searches prune to `partition_names=[tenant]` | `ensure_partition`: idempotent `create_partition` | Milvus 2.x |
| **Chroma** | Per-tenant physical collection | Each (tenant, platform collection) pair is its own physical collection; no native tenancy exists | Physical collection created on demand | n/a |
| **Fallback** (documented, small self-hosted deploys) | Shared collection + `tenant_id` payload filter | Filter-based only — **not** a security boundary; documented as acceptable only when tenants are trusted namespaces, never external customers | n/a | n/a |

**Provisioning flow — lazy and idempotent.** A tenant is provisioned in Postgres at signup. Backend-native tenants (Weaviate/Qdrant), partitions (Milvus), and Chroma physical collections are created **lazily on first collection creation** for that tenant via an idempotent `ensure_tenant` adapter operation (create if absent, no-op if present). No eager backfill across collections; no background sync. The `CapabilityMatrix` exposes `tenancy_model` per backend so the guarantee is introspectable.

**Query/write path.** The service layer resolves the platform collection to its physical backend target *and* asserts that the collection's Postgres `tenant_id` matches the authenticated principal (cheap defense-in-depth — catches logic bugs, never a substitute for backend enforcement). The adapter then scopes by the backend's native mechanism. `tenant_id` is derived server-side only, per the base spec.

**Knock-ons:** collection lifecycle gains a tenancy step; tenant deletion (delete collections, then backend tenants/partitions) is out of the v1 route surface and noted as a future capability; `ensure_tenant` errors map to `COLLECTION_BACKEND_UNAVAILABLE` (taxonomy §10).

## 3. Collection identity & physical naming (D1 knock-on)

The platform collection `name` is **client-facing metadata stored in Postgres**. The physical backend object (Weaviate class, Qdrant collection, Milvus collection, Chroma collection) is named with an **opaque UUID** — `col_<uuid>` — generated at creation and stored in the registry.

- **Collision-free by construction:** tenant A and tenant B can both have a collection named `products`; their physical targets never share a name (on Weaviate/Qdrant the platform collection is one physical object with distinct tenants; on Milvus it is one collection with distinct partitions; on Chroma it is one physical collection *per tenant*, each `col_<uuid>`).
- **No information leakage:** the physical name reveals nothing about the tenant or the platform collection name (which may itself be sensitive).
- The registry owns the mapping exclusively; adapters never see client-facing names, routes never see physical names.

## 4. Vector record schema (D2 knock-on)

The standard record schema gains one optional field. Full shape:

```python
{
  "id": str,                    # client-supplied, idempotent upserts
  "vector": list[float],        # dense, pre-computed by client
  "sparse_vector": {            # OPTIONAL — required for hybrid on Qdrant/Milvus
      "indices": list[int],
      "values": list[float],
  },
  "metadata": dict[str, Any],   # arbitrary payload; backend-native filtering applies
  "tenant_id": str,             # derived server-side; assertion + audit field (see below)
  "created_at": datetime,       # UTC, server-set
  "updated_at": datetime,       # UTC, server-set
}
```

**Role of `tenant_id` on the record:** *not* the isolation mechanism on Weaviate/Qdrant (the backend enforces that via tenants). It is load-bearing on **Milvus** (partition routing), stored per Chroma collection for audit/assertion, and asserted against the principal at the service layer everywhere. Documented per-backend in the adapter docstrings.

**Chroma exception (unchanged from base spec):** Chroma has no per-record timestamps; `created_at`/`updated_at` are folded into the stored metadata payload, documented in the adapter docstring and `CapabilityMatrix`.

**Sparse vector representation:** `{indices: [...], values: [...]}` — matches Qdrant's `SparseVector` and Milvus sparse input shapes directly; ordered by index ascending. Dimension limits: dense capped at `VECTOR_MAX_DIMENSION` (default 4096); sparse capped at 100k non-zero entries per vector (new limit, same 422/`VALIDATION_*` path, added to the OpenAPI contract).

## 5. Hybrid search contract (D2)

Hybrid is three different mechanisms behind one normalized contract. The platform does **not** tokenize text and does **not** call embedding providers — consistent with the vectors-in philosophy; the client who owns their tokenizer/embedding stack supplies the sparse side.

**Request shape (`POST /collections/{name}/hybrid-query`):**

```python
{
  "vector": list[float],                  # required
  "sparse_vector": {indices, values},     # required on Qdrant/Milvus; ignored by Weaviate
  "query_text": str | None,               # required on Weaviate (drives BM25 side)
  "alpha": float,                         # 0.0 = pure keyword, 1.0 = pure dense; default 0.75
  "filter": ..., "top_k": int (≤ 1000),   # same as /query
}
```

**Per-backend mapping:**

| Backend | Mechanism | What the adapter needs |
|---------|-----------|------------------------|
| **Weaviate** | `hybrid(query=query_text, vector=vector, alpha=alpha)` against its inverted index + dense index | `query_text` (BM25 side builds automatically from text properties at write time — works with dense-only vectors) |
| **Qdrant** | Sparse+dense prefetch with RRF fusion | **Stored sparse vectors at upsert time** + `sparse_vector` on the query; `alpha` maps to prefetch weights (RRF default) |
| **Milvus** | Dense + sparse requests with RRF/weighted ranker | **Stored sparse vectors at upsert time** + `sparse_vector` on the query; `alpha` maps to `WeightedRanker` (RRF default) |
| **Chroma** | Unsupported | `hybrid_search: false` in `CapabilityMatrix`; route returns 400 `VALIDATION_*`/capability error |

`alpha` is the single client-facing weighting knob, normalized 0–1, translated per backend by the adapter (Weaviate alpha; Qdrant prefetch weights + RRF; Milvus `WeightedRanker`/`RRFRanker`). Exact SDK translation is adapter-phase work, not client contract.

**Errors (disjoint by construction, like `backend_status`):**
- Chroma (capability unsupported) → `400`, `VALIDATION_UNSUPPORTED_OPERATION`, `details.capability: "hybrid_search"`.
- Qdrant/Milvus with sparse input missing → `422`, `VECTOR_SPARSE_REQUIRED`.

`CapabilityMatrix` exposes `hybrid: {mode: "text+vector" | "sparse+vector" | false, sparse_required: bool}` — the canonical introspection point clients check before calling.

## 6. Async batch data path (D3)

**Problem:** `arq` serializes job args into Redis. A 100k-vector batch (100k × 1536 dims × 4 bytes ≈ 600MB) through Redis is not a scaling concern — it is a broken design at exactly the scale the endpoint exists for.

**Design:**

1. `POST /collections/{name}/vectors/batch` accepts `application/x-ndjson` (one vector record per line, streamed — never buffered whole in memory). Tenant quota (stored vectors + in-flight jobs) is checked at **enqueue time**; violation → `TENANT_QUOTA_EXCEEDED`.
2. The request body streams to an **object store** — S3-compatible via boto3 (`BATCH_STORAGE_*` config; MinIO in compose, S3 in cloud mode). Key: `{tenant_id}/{job_id}.jsonl`. **MinIO is already in the deployment as the Milvus sidecar** — the batch path reuses it, adding no new infrastructure category.
3. The `arq` job receives only `{job_id, object_key}` — small enough for Redis. No payload ever transits the queue.
4. The worker streams the file line-by-line, validates each record, and upserts in chunks through the adapter's batch path. Per-vector outcomes (`id → ok|error_reason`) stream to `{tenant_id}/{job_id}.results.jsonl`.
5. `GET /jobs/{job_id}` reports status (`queued|running|succeeded|failed`), counts (total/ok/failed), and a link to the results object.
6. **Retry is safe by design:** upserts are idempotent (client-supplied IDs); a retried job cannot duplicate.
7. Whole-file validation failures map to `JOB_PAYLOAD_INVALID`; per-line failures land in the results object and count toward `failed` (job still completes).

**Payload format:** JSONL for v1 (streamable, per-line validation). Parquet noted as a future format for very large loads — do not build it into v1.

**Later-phase optimization (noted, not built):** direct-to-object-storage upload via pre-signed URLs (client → S3/MinIO, never touching the API server). This is a real API-surface change (upload tokens, multi-part, retry semantics) and belongs in a later phase with its own design.

The sync `POST /vectors` path (≤ 100 vectors, `BATCH_SIZE_EXCEEDED` above) is unchanged: direct, no staging.

## 7. Control-plane drift policy (D4)

**Non-goal for v1:** building actual reconciliation (drift detection, conflict resolution policy, scheduler/queue) is machinery we'd be speculating about before we know how often drift actually occurs. Drift is made **observable**, not self-healing:

- **Tolerant lifecycle ops:** collection delete tolerates an already-missing backend collection; collection create is idempotent (backend exists → proceed, reconcile Postgres). These are hygiene regardless of drift.
- **`backend_status` on the read path:** `GET /collections/{name}` and `GET /collections` include a per-collection `backend_status` field from the adapter's `get_collection_info`. Values are **disjoint by construction**:
  - `exists` — the adapter confirmed the collection exists.
  - `missing` — the adapter *call succeeded* but the collection was not found. Runbook: backend state problem (accidental deletion, wrong instance); recover or re-create.
  - `error` — the adapter *call itself failed* (timeout, connection, auth). Runbook: network/credentials/backend health — a different runbook from `missing`, which is why the distinction is explicit.
- Reconciliation-as-a-job is documented in the README as a future phase, with drift frequency data from `backend_status` observations informing whether it's ever needed.

## 8. Audit-log immutability (D5)

Two layers, both in the Phase 1 migration:

1. **Two Postgres roles:** `app` (INSERT/SELECT only on `audit_log`; no UPDATE/DELETE grants) and `migrator` (DDL; Alembic runs as this role). This is good practice but fragile alone — one hurried `GRANT` and `app` quietly regains UPDATE.
2. **Guard trigger on `audit_log` (the actual enforcement):** a `BEFORE UPDATE OR DELETE` trigger that raises, applied to **all roles including `migrator`**. Dropping it is a visible DDL event, so the property is "impossible to defeat *quietly*" — not "impossible to defeat." Migrator only performs schema DDL; `ADD COLUMN` backfills don't touch existing rows, so no carve-out is needed. A deliberate historical rewrite is an explicit, visible operation (drop trigger → migrate → recreate trigger).

**Threat model (stated explicitly):** this defends against the app's own failure modes — bugs, misconfigured grants, compromised app credentials. It does **not** defend against superuser/`postgres` access or physical database access. "Immutable" in this spec means immutable-by-trigger against the application, not immutable against the database owner.

**Ops note:** Alembic migrations run under the `migrator` role, which the compose/k8s provisioning must reflect (two credentials, `MIGRATOR_*` env in deploy config only).

## 9. Phase-by-phase knock-ons

| Phase | Changes from the base plan |
|-------|----------------------------|
| **1 — Scaffold** | Two-role Postgres provisioning + audit guard trigger in the initial migration; `.env.example` gains `BATCH_STORAGE_*` (MinIO/S3) placeholders; registry model gains `physical_name`; git init + design doc committed (this session) |
| **2 — Auth & RBAC** | No change (tenancy provisioning hooks live in the tenant service: `ensure_tenant` is called from collection creation, not signup) |
| **3 — Chroma first** | Opaque `col_<uuid>` physical naming; per-tenant physical collections; `backend_status` on read path; `tenancy_model: "collection-per-tenant"` in matrix |
| **4 — Qdrant & Weaviate** | Qdrant: payload-partition + `is_tenant` index (native tenant API removed server-side — drift recorded); Weaviate: native multi-tenancy (≥ 1.20); `ensure_tenant` adapter op; tenant-scoped query/upsert; hybrid contract (§5) incl. sparse schema + `alpha` mapping |
| **5 — Milvus** | Partition-per-tenant, partition routing on insert/search; sparse support for hybrid; consistency levels |
| **6 — Jobs & matrix** | `arq` + object-storage staging (§6); results objects; quota at enqueue; `CapabilityMatrix` gains `tenancy_model`, `hybrid` mode, `sparse_required` |
| **7 — Observability** | No change (batch job span covers object storage read + adapter write) |
| **8 — Deployment** | MinIO in compose (already present for Milvus) gains the batches bucket; cloud compose/config gains S3 creds; Postgres provisioning creates `app`/`migrator` roles |
| **9 — Docs & polish** | README documents the tenancy matrix, hybrid contract, batch data path, drift non-goal, audit threat model |

## 10. Error taxonomy additions

Amending `CLAUDE.md`'s taxonomy (extending existing namespaces, as permitted):

- `VECTOR_SPARSE_REQUIRED` (VECTOR_*) — hybrid requested without sparse support/input on Qdrant/Milvus
- `JOB_PAYLOAD_INVALID` (JOB_*) — whole-file validation failure on a batch job
- `VALIDATION_UNSUPPORTED_OPERATION` (VALIDATION_*) — operation not supported by a backend (e.g. hybrid on Chroma), with `details.capability` naming the capability

No new namespaces introduced. All other error handling per the base spec.

## 11. Non-goals & future phases (explicit)

- Reconciliation engine / drift self-healing (deferred; §7)
- Direct-to-object-storage upload (pre-signed URLs) (deferred; §6)
- Parquet batch payloads (deferred; §6)
- Tenant deletion API (delete collections + backend tenants/partitions) (future capability)
- Per-tenant backend instances (`backend_instance_id`) (explicitly out of scope for v1, per base spec)
- Embeddings/text-in layer (future phase, per base spec)
- Cross-backend collection migration (`POST /collections/{name}/migrate`) (future capability, per base spec)

## 12. Open items for implementation

- Exact SDK call shapes for native tenancy (Weaviate `tenants.create`, Qdrant `create_tenant`, Milvus `create_partition`) verified against current SDK versions at each adapter phase, plus the pymilvus async check already required by the base spec.
- `alpha` → Qdrant/Milvus fusion mapping finalized against SDK semantics during Phase 4/5 (RRF by default).
- Sparse vector cap (100k non-zero entries) validated against backend limits during Phase 4/5; adjust if a backend caps lower.
