# VectorHub Platform

A production-grade **unified REST API over four vector databases** — **Weaviate, Qdrant, Milvus, and Chroma** — with enterprise auth/RBAC, multi-tenancy, audit logging, rate limiting, async batch ingestion, and full observability. One consistent API surface; no lowest-common-denominator: backend-specific capability is preserved and introspectable via a live capability matrix.

- **OpenAPI/Swagger**: `http://localhost:8000/docs` (request-body examples are generated from the live capability matrix, so the docs always match the current backend feature set)
- **Internal admin app** (`/health`, `/metrics`): `127.0.0.1:9091` by default — never exposed publicly (see [Observability](#observability))
- **Interactive capability matrix**: `GET /api/v1/capabilities`

---

## Architecture

```
                         ┌─────────────────────────────────────────────────────┐
                         │            API process (python -m app.main)         │
                         │  one process, two ASGI apps — the Prometheus        │
                         │  registry is process-local, so /metrics must live   │
                         │  in the same process as the public app              │
                         │                                                     │
  clients ──── :8000 ───▶│  PUBLIC APP  (FastAPI, /api/v1/*)                   │
  REST / JWT / API keys  │   auth ─ tenants ─ api-keys ─ collections ─         │
                         │   vectors ─ jobs ─ capabilities ─ audit-logs        │
                         │        │                                            │
                         │        ▼  (routes validate + shape responses only)  │
                         │  SERVICE LAYER   Collection/Vector/Search/Tenant/   │
                         │   Auth/ApiKey/Job — permission checks, tenant       │
                         │   scoping (always from the principal, never the     │
                         │   body), quota enforcement, audit writes            │
                         │        │                                            │
                         │        ▼                                            │
                         │  ADAPTER REGISTRY ──► one SDK client per backend,   │
                         │   constructed once at startup (singleton)           │
                         └────────┬──────────┬──────────┬──────────┬───────────┘
                                  │          │          │          │
            probes/scrapers ──▶ :9091 (admin app: /health + /metrics, no auth,
                                  no middleware — never published/Ingressed)
                                  │
                     ┌────────────┘          │           │              └───────┐
                     ▼                       ▼           ▼                      ▼
                Weaviate                 Qdrant      Milvus                  Chroma
              (native tenant        (is_tenant   (partition-            (per-tenant
               shards)               index)      per-tenant)             physical
                                                                         collections)

  CONTROL PLANE   PostgreSQL  tenants · users · roles · API keys · collections
                                 registry · jobs · audit_log (append-only)
                  Redis        rate limiting · auth cache · arq job broker ·
                                 worker heartbeats
                  Object store MinIO (compose) / S3 (cloud) — async batch
                                 staging + per-vector results
                  arq worker    background job process (async batch ingest)
```

**Key invariants:**

- **Routes never call adapters directly.** Every route goes through a service class; adapters are thin, backend-specific translators behind the `VectorDBAdapter` interface.
- **`tenant_id` is always derived from the authenticated principal, never the request body** — all request schemas use `extra="forbid"`, so a forged `tenant_id`/`owner_id`/role field is rejected with `422` (proven by test).
- **The registry is pluggable.** A fifth backend = write an adapter + `register()` it; no core-app changes.
- **One SDK client per backend**, held for the app's lifetime. Isolation is *not* separate connections — it is backend-enforced per the [Tenancy Matrix](#tenancy-matrix).

---

## Quick start

Prerequisites: Docker (with compose v2). No local Python needed for the stack.

```bash
# Full self-hosted stack: app + worker + Postgres + Redis + MinIO + all four
# vector DBs (incl. the Milvus etcd+MinIO sidecar trio). Migrations run as a
# one-shot; the batch bucket is provisioned automatically.
docker compose -f deploy/docker-compose.yml up -d --build

# Verify the whole thing boots healthy (the deploy gate):
deploy/smoke.sh

# OpenAPI docs:
open http://localhost:8000/docs
```

The stack is healthy when `deploy/smoke.sh` passes — it asserts the internal
admin `/health` reports `status: ok` (Postgres + Redis + worker heartbeats +
every adapter), that the public app 404s on `/health` (the admin boundary),
**and then runs the real-user API journey** (`deploy/smoke/journey.py`) across
all four backends — register → collection → upsert → query → filter → hybrid
→ async batch (real arq worker + MinIO staging) → delete — so the gate proves
the stack doesn't just boot, it actually serves the full API surface.

**First user:** registration creates a tenant and its owner. Emails listed in
`BOOTSTRAP_PLATFORM_ADMIN_EMAILS` (set before the first register) become
platform admins, who can create tenants and read all audit logs.

```bash
curl -s -X POST localhost:8000/api/v1/auth/register \
  -H 'content-type: application/json' \
  -d '{"email":"owner@example.com","password":"hunter2hunter2"}'
```

Then create a collection on any backend and upsert vectors:

```bash
TOKEN="<access_token from register>"
curl -s -X POST localhost:8000/api/v1/collections \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"docs","backend":"qdrant","dimension":4,"distance":"cosine"}'

curl -s -X POST localhost:8000/api/v1/collections/docs/vectors \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"vectors":[{"id":"v1","vector":[0.1,0.2,0.3,0.4],"metadata":{"tag":"a"}}]}'

curl -s -X POST localhost:8000/api/v1/collections/docs/query \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"vector":[0.1,0.2,0.3,0.4],"top_k":5}'
```

### Config

All configuration is environment variables (see `.env.example`). `pydantic-settings`
loads `.env` if present; compose has sane dev defaults via `${VAR:-default}`.

| Group | Variables |
| --- | --- |
| Serving | `API_HOST`/`API_PORT`, `ADMIN_HOST`/`ADMIN_PORT` (default `127.0.0.1:9091`; containers set `0.0.0.0`) |
| Auth | `JWT_SECRET` (required non-default in staging/prod), `JWT_ACCESS_TTL_MINUTES`, `JWT_REFRESH_TTL_DAYS`, `BOOTSTRAP_PLATFORM_ADMIN_EMAILS` |
| Control plane | `DATABASE_URL` (app role), `MIGRATOR_DATABASE_URL` (DDL role), `REDIS_URL` |
| Vector backends | `QDRANT_URL`, `WEAVIATE_URL` (+`WEAVIATE_GRPC_PORT`), `MILVUS_URL`, `CHROMA_URL` |
| Batch staging | `BATCH_STORAGE_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET` (MinIO or any S3-compatible) |
| Limits | `VECTOR_MAX_DIMENSION` (4096), `SPARSE_MAX_CARDINALITY` (100000) |
| Observability | `LOG_FORMAT` (`console`\|`json`), `OTEL_EXPORTER_OTLP_ENDPOINT`, `SENTRY_DSN`, `CORS_ALLOWED_ORIGINS` |

---

## Deployment

Three ways to run, one image (built by CI and pushed to `ghcr.io` on main after the deploy smoke passes):

| Mode | How |
| --- | --- |
| **Local/dev** | `deploy/docker-compose.yml` — everything self-hosted, `mc` bucket one-shot, `migrate` one-shot before app/worker start |
| **Cloud-managed** | `deploy/docker-compose.cloud.yml` — control plane self-hosted; vector URLs are `${VAR:?}`-required, so `docker compose config` fails fast if a managed endpoint is missing. Same image. |
| **Kubernetes** | `deploy/k8s/` — kustomize base + dev/staging/prod overlays; app Deployment with migrate initContainer, probes on the internal admin port (9091 appears in **no** Service by construction), HPA, Ingress; self-hosted backend StatefulSets (dev/staging) or managed endpoints (prod). `kubectl apply -k deploy/k8s/overlays/<env>` — or the **Helm chart** `deploy/helm/vectorhub` wrapping the same resources with image tags, credentials, replica counts, and self-hosted/managed toggles as values (`helm install vectorhub deploy/helm/vectorhub --set image.tag=main`) |

**Observability stack** (`deploy/monitoring/`): Prometheus alert rules, Alertmanager routing (critical → pager, warning → Slack), Grafana dashboard, and a monitoring compose trio. See `deploy/monitoring/README.md`.

**Migrations** (`deploy/migrate.sh`): `alembic upgrade head` as the `migrator` role, then the app/migrator role-password bootstrap. Idempotent — safe as a one-shot, a k8s initContainer, or re-runs.

**Deletion is destructive and immediate.** Collection/vector deletes call the backend adapter as part of the same operation — hard deletes, not soft-deletes. Backend-specific space-reclamation caveats are noted in the capability matrix.

---

## API surface

All routes under `/api/v1`. Errors always use the `{error_code, message, details}` shape (see [Error taxonomy](#error-taxonomy)).

```
POST   /auth/register                     # creates the user's tenant + owner role
POST   /auth/login                        # access (15 min) + refresh (30 d, rotated, stored hashed)
POST   /auth/refresh
POST   /auth/logout                       # revokes access token jti + refresh token
GET    /auth/me

GET    /capabilities                      # live per-backend capability matrix

POST   /tenants                           # platform-admin only
GET    /tenants/{id}
GET    /tenants/{id}/members              # cursor-paginated directory (limit 1-200)
POST   /tenants/{id}/members              # provision a member account (admin/owner)
PATCH  /tenants/{id}/members/{user_id}    # change role; last owner cannot be demoted

POST   /collections                       # body: name, backend (immutable), dimension, distance
GET    /collections
GET    /collections/{name}
DELETE /collections/{name}                # hard delete at the backend, same operation
PATCH  /collections/{name}/config         # mutable index params; else 409 REQUIRES_REINDEX
GET    /collections/{name}/permissions    # resource-level grants (cursor-paginated)
PATCH  /collections/{name}/permissions    # grant/update a user's role on this collection
DELETE /collections/{name}/permissions/{user_id}
POST   /collections/{name}/reindex        # v1 stub: 501 REINDEX_NOT_IMPLEMENTED

POST   /collections/{name}/vectors        # upsert 1-100 vectors (pre-computed)
POST   /collections/{name}/vectors/batch  # async job: streamed application/x-ndjson → 202 job_id
GET    /collections/{name}/vectors/{id}
DELETE /collections/{name}/vectors/{id}
POST   /collections/{name}/query          # similarity search, metadata filters, top_k ≤ 1000
POST   /collections/{name}/hybrid-query   # where the backend supports it (see contract)

GET    /jobs/{job_id}                     # async batch job status/counts (tenant-scoped)
GET    /admin/audit-logs                  # admin/owner, keyset-paginated
```

**Authentication** — JWT access tokens (`OAuth2PasswordBearer`) for interactive clients, plus per-tenant **API keys** for service-to-service calls (hashed at rest, optional expiry, per-key rate limits). **RBAC**: roles `owner > admin > editor > viewer` at the tenant level, plus **resource-level grants** per collection that elevate or demote a user's effective role on that collection. Access tokens are revocable: logout deny-lists the `jti` (Postgres source of truth, Redis front cache with positive markers only).

**Pagination** — every list endpoint is cursor-paginated with an opaque cursor: `{items, total, next_cursor}`. `limit` 1–200, default 50.

---

## Tenancy matrix

Tenant isolation is a **security boundary — enforced by the vector backend itself**, never by filter logic alone. The service layer additionally asserts `collection.tenant_id == principal.tenant_id` before every adapter call (defense-in-depth, never the boundary). `GET /capabilities` exposes `tenancy_model` per backend.

| Backend | Isolation mechanism | `ensure_tenant` | Notes |
| --- | --- | --- | --- |
| **Weaviate** | **Native multi-tenancy** — tenant-enabled class (`multiTenancyConfig.enabled: true`); each tenant owns a dedicated shard; all requests scoped by tenant name | idempotent `tenants.create` before first use | Gate: Weaviate ≥ 1.21 |
| **Qdrant** | **Payload-partition + `is_tenant` index** — a `_vhk_tenant_id` keyword field indexed with `is_tenant=True` co-locates a tenant's points (a locality hint, not a shard boundary); the adapter **always** applies the tenant filter — there is no unscoped path by construction | no-op (index created with the collection) | **Drift:** Qdrant removed its native tenant API from client and server (verified against v1.19 — the server 404s the old endpoints); the `is_tenant` payload index is the canonical current mechanism. Gate: Qdrant ≥ 1.10 |
| **Milvus** | **Partition-per-tenant** — one partition per tenant; inserts route by partition, every search prunes to `partition_names=[tenant]` | idempotent `create_partition` | A raw unscoped search spans *all* partitions — the always-applied scope is load-bearing (proven by the isolation suite) |
| **Chroma** | **Per-tenant physical collection** — no native tenancy exists; each (tenant, platform collection) pair is its own physical `col_<uuid>` collection, created on demand | no-op | Physical names are opaque UUIDs; nothing client-facing leaks |
| **Fallback** (documented, small self-hosted deploys only) | Shared collection + `tenant_id` payload filter | — | **Not a security boundary** — acceptable only when tenants are trusted namespaces, never external customers |

**Physical naming:** the platform `name` is client-facing metadata in Postgres; the physical backend object (Weaviate class, Qdrant collection, Milvus collection, Chroma collection) is `col_<uuid>`, owned exclusively by the registry — collision-free by construction, no information leakage.

**Provisioning** is lazy and idempotent: backend tenants/partitions/physical collections are created on first collection creation. No eager backfill.

**The isolation suite** (the acceptance gate for every adapter) proves this behaviorally with indistinguishable data: same IDs + same vectors across tenants, cross-tenant access must *error or return empty* — never cross-tenant rows. Three layers: adapter-level (testcontainers, per-backend mechanism proofs), service-level (recording stubs, no containers), and API-level (two principals per backend, JWT + API-key).

---

## Hybrid search contract

The platform is **vectors-in, vectors-out** — it never calls embedding providers and never tokenizes text on the client's behalf. For hybrid, the client supplies whichever side the backend needs: the dense `vector`, and either `query_text` (Weaviate's BM25 side) or `sparse_vector` (Qdrant/Milvus). A single normalized `alpha` (0.0 = pure keyword, 1.0 = pure dense, default 0.75) is translated per backend.

| Backend | Mode (`capabilities.hybrid.mode`) | Sparse side | Fusion |
| --- | --- | --- | --- |
| Weaviate | `text+vector` | `query_text` → BM25 against the inverted index | native alpha |
| Qdrant | `sparse+vector` | `sparse_vector` required | prefetch weights + RRF |
| Milvus | `sparse+vector` | `sparse_vector` required | `WeightedRanker(alpha, 1-alpha)` (RRF ignores alpha, so it is not used — the contract is alpha-faithful) |
| Chroma | `false` | — | `400 VALIDATION_UNSUPPORTED_OPERATION` with `details.capability: hybrid_search` |

Errors are disjoint: Chroma → `400` naming the capability; Qdrant/Milvus with sparse input missing → `422 VECTOR_SPARSE_REQUIRED`. Check `GET /capabilities` before calling — it is the canonical introspection point.

---

## Batch data path

`POST /collections/{name}/vectors/batch` accepts **streamed `application/x-ndjson`** (one vector record per line — never buffered whole) and returns `202 {job_id}`. The payload **never transits Redis or arq**.

```
 client ──streams NDJSON──▶  POST /vectors/batch
        │                    tenant quota checked at enqueue (max 5 outstanding)
        ▼
  object storage   {tenant_id}/{job_id}.jsonl      MinIO in compose, S3 in cloud
        │  arq job receives ONLY {job_id, object_key}
        ▼
  arq worker  run_batch_ingest
        │  streams in 1 MiB chunks → parses line-by-line → validates per record
        │  → chunked batch_upsert (per-backend chunk sizes) with backpressure
        │  (v1 pipeline is serial: parse pauses during each chunked upsert)
        ▼
  {tenant_id}/{job_id}.results.jsonl   (per-vector outcomes)
        └─ GET /jobs/{job_id}  → status/counts (queued/running/succeeded/failed)

  Retry is safe: upserts are idempotent (client-supplied IDs).
  Whole-file validation failure → JOB_PAYLOAD_INVALID.
```

**Throughput model** (design session 2026-08-14, validated by the soak test): JSONL is ~10 B/float, so a 100k × 1536-dim job is ~1.6 GB — the largest term in the path. Raw JSONL meets the v1 budget as designed; enqueue is bandwidth-bound, ingest is chunked per backend:

| Backend | Default chunk size |
| --- | --- |
| Qdrant | 5–10k per request |
| Weaviate | ~1k with server-side batching |
| Milvus | 1–10k |
| Chroma | 100–1k (the throughput floor — ~10–30 s for 100k) |

The measured soak (`tests/integration/test_batch_soak.py`, 100k × 64-dim on Qdrant): **60.5 s** worker run (parse+validate ≈ 20 s + ingest ≈ 12 s, serialized), **~74 MB RSS delta** on a 60 MB payload — the pipeline never materializes the file (whole-buffering would need ~460 MB). The model's optimistic ~15–45 s assumes read-ahead overlap; **gzip payloads and read-ahead are later knobs**, not v1.

---

## Vector record schema

Every adapter accepts and returns this common shape (backend-native filtering applies against `metadata`):

```jsonc
{
  "id": "client-supplied string",          // idempotent upserts
  "vector": [0.1, 0.2, ...],               // ≤ VECTOR_MAX_DIMENSION (4096) floats
  "sparse_vector": {                       // optional; required for hybrid on Qdrant/Milvus
    "indices": [1, 4, 9],                  // ascending, ≤ SPARSE_MAX_CARDINALITY (100000)
    "values": [0.5, 0.2, 0.1]
  },
  "metadata": { "any": "user payload" }    // backend-native filtering applies here
  // tenant_id: server-derived, never client-supplied
  // created_at / updated_at: server-set UTC
}
```

**Platform-wide limits** (enforced by Pydantic at the schema layer, documented in OpenAPI): `VECTOR_DIMENSION_EXCEEDED` (> 4096 dims), `BATCH_SIZE_EXCEEDED` (> 100 sync vectors — the async path has no hard cap), `TOP_K_EXCEEDED` (> 1000), sparse cardinality cap. One exception documented in the capability matrix: Chroma doesn't track per-record timestamps, so `ChromaAdapter` folds `created_at`/`updated_at` into the stored metadata.

---

## Error taxonomy

All errors: `{error_code, message, details}`. Namespaced, defined as an enum, mapped from typed exceptions — never raw strings.

| Namespace | Examples |
| --- | --- |
| `AUTH_*` | `AUTH_INVALID_CREDENTIALS`, `AUTH_TOKEN_EXPIRED`, `AUTH_TOKEN_REVOKED`, `AUTH_INSUFFICIENT_SCOPE`, `AUTH_EMAIL_TAKEN` |
| `API_KEY_*` | `API_KEY_NOT_FOUND` |
| `TENANT_*` | `TENANT_NOT_FOUND`, `TENANT_ALREADY_EXISTS`, `TENANT_MEMBER_NOT_FOUND`, `TENANT_LAST_OWNER`, `TENANT_QUOTA_EXCEEDED` |
| `COLLECTION_*` | `COLLECTION_NOT_FOUND`, `COLLECTION_ALREADY_EXISTS`, `COLLECTION_BACKEND_UNAVAILABLE`, `REQUIRES_REINDEX`, `REINDEX_NOT_IMPLEMENTED` |
| `VECTOR_*` | `VECTOR_NOT_FOUND`, `VECTOR_DIMENSION_MISMATCH`, `VECTOR_DIMENSION_EXCEEDED`, `BATCH_SIZE_EXCEEDED`, `TOP_K_EXCEEDED`, `VECTOR_SPARSE_REQUIRED` |
| `JOB_*` | `JOB_NOT_FOUND`, `JOB_FAILED`, `JOB_PAYLOAD_INVALID` |
| `RATE_LIMIT_*` | `RATE_LIMIT_TENANT_QPS`, `RATE_LIMIT_API_KEY_QPS`, `RATE_LIMIT_ROUTE_QPS` (populate `details` on 429) |
| `VALIDATION_*` | `VALIDATION_UNSUPPORTED_OPERATION` (`details.capability` names it), `VALIDATION_INVALID_CURSOR`, `VALIDATION_GENERIC` |

No-oracle discipline: cross-tenant access to a collection/job/vector returns the same static `COLLECTION_NOT_FOUND`/`JOB_NOT_FOUND` message regardless of existence — responses cannot be used as an existence oracle.

---

## Drift & non-goals (deliberate v1 scope)

- **`backend` is immutable** for the life of a collection. There is no v1 path that changes a backend, and none will be added without a separate migration-job design. **Cross-backend vector migration is out of scope** for v1.
- **`POST /collections/{name}/reindex` is an honest 501 stub** (`REINDEX_NOT_IMPLEMENTED`). `PATCH /config` supports only the subset each backend can apply live; anything else returns `409 REQUIRES_REINDEX` with `details.next_step` pointing at the stub. Full reindex-as-a-job is a tracked follow-up.
- **Qdrant tenancy drift (documented, not hidden):** the design originally targeted Qdrant's native tenant API; it was removed from client *and* server (verified live against v1.19), so the adapter implements the canonical current mechanism — `_vhk_tenant_id` payload + `is_tenant` index — and the isolation suite proves it. See the [Tenancy Matrix](#tenancy-matrix).
- **Embeddings:** the platform is **vectors-in, vectors-out**. No embedding-provider calls, no tokenization. A text-in layer would be an optional `services/embeddings/` phase with a pluggable provider interface — not folded into v1.
- **GDPR-style right-to-erasure tooling** (cross-tenant PII search) is out of scope; "delete" is destructive and immediate at the backend level (see Deployment).
- **Batch pipeline:** read-ahead overlap and gzip payloads are later knobs the throughput model assumed — see the [Batch data path](#batch-data-path).

---

## Audit logging — threat model

Every failed mutating request (4xx/5xx with a decodable principal) writes an immutable audit record (actor, tenant, action, resource, timestamp, result) to `audit_log`, readable via `GET /api/v1/admin/audit-logs`. Immutability is enforced at the **database level**, not by convention:

1. **Two Postgres roles:** `app` gets `INSERT`/`SELECT` only on `audit_log` (no `UPDATE`/`DELETE` grants); migrations run as `migrator`, which holds DDL.
2. **A guard trigger** on `audit_log` raises on any `UPDATE`/`DELETE`, applied to **all roles including `migrator`** — dropping it is a visible DDL event, so the property is "impossible to defeat quietly", not "impossible to defeat".

**Threat model:** this defends against the app's own failure modes — bugs, misconfigured grants, compromised app credentials. It does **not** defend against superuser/`postgres` access or physical DB access.

---

## Observability

- **`GET /health`** (admin app, `ADMIN_HOST:ADMIN_PORT`): overall `ok`/`degraded`/`down` with per-dependency checks — Postgres (connection + query), Redis (PING), worker liveness (arq heartbeat freshness), and every registered adapter's `health_check()` (timeout-bounded). `200` only when the critical deps (Postgres, Redis) are up; a single backend outage → `200` `degraded` (no k8s restart loops); Postgres/Redis down → `503`.
- **`GET /metrics`** (admin app): Prometheus counters for health-check outcomes, request counts by status, rate-limit rejections, plus request-duration/size histograms from `prometheus-fastapi-instrumentator`. **Both endpoints are absent from the public app** (404) — the admin port is never published (compose) or Service/Ingressed (k8s). Scrapers/probes can't do auth, so the boundary is network exposure.
- **Structured logging** (`structlog`): console (dev) or JSON (staging/prod via `LOG_FORMAT=json`); every request logs method/path/status at INFO, slow requests (> `SLOW_REQUEST_THRESHOLD_MS`) at WARNING.
- **Request correlation is mandatory:** every request gets a `request_id` (echoed in `X-Request-ID`), bound into the log context for its whole lifecycle; the OpenTelemetry trace ID is **deterministically derived from the request ID** (for UUID4 IDs they're identical), so logs, traces, and Sentry events join by construction — no lookups. Export via `OTEL_EXPORTER_OTLP_ENDPOINT`; unset = correlated but dropped.
- **Sentry** (`SENTRY_DSN`): env-gated (unset = SDK never imported); captures unhandled 500s only, with the request/trace IDs attached.

---

## Development

```bash
uv sync                      # deps (uv required; uv.lock committed)
uv run pre-commit install    # ruff check / ruff format / mypy on staged files
uv run pytest -q             # full suite — real Postgres/Redis/vector DBs via testcontainers
uv run pytest -q -m "not soak and not milvus"          # fast gate (Docker needed)
uv run pytest -q --random-order --random-order-bucket=global -m "not soak and not milvus"
```

- **CI** (`.github/workflows/ci.yml`): lint + type gates; the suite in **global-bucket randomized order** (the order-dependency hardening gate — seed printed, failures reproduce with `--random-order-seed=<seed>`); Milvus in its own job; a nightly date-seeded full-suite run; the **deploy smoke** (full compose boot + `/health` ok + the all-four-backends API journey) gating the image push.
- **Isolation suite**: the security-boundary acceptance gate, three layers (see [Tenancy Matrix](#tenancy-matrix)).
- **Soak**: `tests/integration/test_batch_soak.py` (`-m soak`) validates the throughput model's bounded-memory and chunk-size predictions on real Qdrant + MinIO.
