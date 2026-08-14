# Project: Unified VectorHub Platform API

This file is read automatically by Claude Code at the start of every session in this repo. Don't ask me to re-paste context — read this file plus the current repo state, check the **Progress Log** at the bottom to see what's done, and continue from the next unchecked phase unless I tell you otherwise (e.g. "do Phase 3" or "fix X first").

## Session Start Checklist

Run through this at the start of every session, in order, before writing new code:

1. **Read the Progress Log** (bottom of this file) to see which phases are already checked off and what the last entry says.
2. **Check git state**: run `git log --oneline -10` and `git status` to confirm the working tree is clean and matches what the Progress Log claims. Flag any mismatch before proceeding.
3. **Run existing tests/lint** if the project has runnable code yet (`pytest`, `ruff check .`, `mypy .`). Confirm everything still passes before adding new code. If something is broken, fix or report it before starting new work — don't build on top of a broken base.
4. **Identify the next unchecked phase** in the Build Phases list and confirm with me in one line what you're about to do, unless I already told you explicitly which phase/task to work on.
5. **Work the phase**, committing incrementally per the Git Workflow rules below.
6. **At the end of the session**: confirm tests/lint pass, tick the phase checkbox, add a Progress Log entry, and commit that update to this file (`docs: update progress log for Phase N`). If the phase isn't fully done, log that clearly (e.g. "Phase 4 in progress — Qdrant adapter done, Weaviate adapter pending") rather than checking the box prematurely.

## Mission

Build a production-grade, enterprise-ready **FastAPI platform** that provides a **unified REST API** over four vector databases: **Weaviate, Qdrant, Milvus, and Chroma**. The platform must expose the full capability set of each database (not a lowest-common-denominator subset) while presenting one consistent, well-documented API surface to clients. It must support multi-tenancy, enterprise auth/RBAC, audit logging, and rate limiting, and be deployable via Docker Compose, Kubernetes, or against fully managed cloud DB endpoints.

## Git Workflow (mandatory)

- This repo is git-tracked from the start. If `.git` doesn't exist yet, run `git init` and create an initial commit before writing any code.
- **Commit after every meaningful change** — not one giant commit per phase. A meaningful change = a working unit: a new adapter method, a new route, a passing test suite, a config file, a migration. Don't batch unrelated changes into one commit.
- Use **Conventional Commits** style: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`, `ci:`. Example: `feat(adapters): implement QdrantAdapter.query with metadata filtering`.
- Keep commit messages specific to what changed and why, not generic ("update files", "wip" are not acceptable).
- Create a `.gitignore` immediately (Python, env files, `__pycache__`, `.venv`, IDE folders, secrets) before the first commit so nothing sensitive or generated gets tracked.
- Never commit `.env`, credentials, or API keys — only `.env.example` with placeholder values.
- At the end of each phase, make sure the working tree is clean (everything meaningful committed) and update the **Progress Log** section below in this same `CLAUDE.md` file with a checkbox tick and a one-line summary, committed as `docs: update progress log for Phase N`.
- Use feature branches only if I ask for them explicitly; by default work directly on `main` with frequent small commits, since this is a solo/sequential build.
- If a phase leaves the build in a broken/non-runnable state at a natural stopping point, say so clearly in your final message of that session rather than silently committing broken code as if it were done.
- Do not commit changes proactively before verification: run build/lint/tests first and confirm they pass, then commit. Report verification results and any uncovered risks alongside each commit summary.

## Architecture: Adapter / Strategy Pattern

- Define a single abstract interface, `VectorDBAdapter`, with the core operations every backend must support:
  - `create_collection`, `delete_collection`, `list_collections`, `get_collection_info`
  - `upsert_vectors`, `delete_vectors`, `fetch_vectors`
  - `query` (top-k similarity search with metadata filters)
  - `hybrid_search` (where supported — Weaviate, Qdrant, Milvus)
  - `batch_upsert` / `batch_delete` with async job tracking for large loads
  - `create_index` / index config (HNSW params, distance metric, quantization where applicable)
  - `health_check`
- Implement one concrete adapter per DB: `WeaviateAdapter`, `QdrantAdapter`, `MilvusAdapter`, `ChromaAdapter`, each in its own module under `app/adapters/`.
- Each adapter exposes an `extras: dict` passthrough so DB-specific features aren't lost — e.g. Weaviate `nearText`/generative search modules, Qdrant payload-based filtering and quantization config, Milvus partitions and consistency levels, Chroma's `where_document` full-text filters.
- **Standard vector record schema:** every adapter must accept and return vectors in a common shape regardless of backend: `id` (client-supplied, string, for idempotent upserts), `vector` (list[float]), `sparse_vector` (optional — `{indices: list[int], values: list[float]}`, indices ascending; required for hybrid search on Qdrant/Milvus), `metadata` (dict[str, Any], arbitrary user payload, backend-native filtering applies against this), `tenant_id` (derived server-side, never client-supplied — an assertion + audit field, load-bearing for Milvus partition routing, but **not** the isolation mechanism; see Tenancy Matrix below), `created_at`, `updated_at` (UTC timestamps, server-set). This is the contract `VectorService` and the API schemas are built around — adapters translate to/from it internally. Note: Chroma doesn't natively track `created_at`/`updated_at` per-record, so `ChromaAdapter` must fold them into the stored metadata payload rather than dropping them; document this exception in the adapter's docstring and in `CapabilityMatrix`.
- A **Collection** is the platform's core resource: it has a name, tenant owner, embedding dimension, distance metric, and a `backend` field declaring which adapter/DB instance it lives on (`weaviate|qdrant|milvus|chroma`). Backend is chosen at collection-creation time (per-collection, not global config), stored in Postgres, and resolved via an `AdapterRegistry` / factory at request time.
- **`AdapterRegistry` is pluggable, not a hardcoded switch statement.** Expose `register(backend_name, adapter_cls)`, `unregister(backend_name)`, `get(backend_name) -> VectorDBAdapter`, `list() -> list[str]`, and `capabilities(backend_name) -> CapabilityMatrix entry`. The four built-in adapters register themselves at startup via this same API (e.g. an explicit call in `app/adapters/__init__.py`), not a hardcoded if/elif in the routing layer — so a fifth backend later (e.g. Pinecone) is "write an adapter + register it," not "touch the core app."
- Add a `CapabilityMatrix` exposed at `GET /capabilities` so clients can introspect which features (hybrid search, filtering, batch async, quantization, multi-vector, sparse vectors) each backend currently supports — this keeps the abstraction honest instead of hiding backend differences.
- **Adapter client lifecycle:** each registered adapter instance owns exactly **one** underlying SDK client, constructed once at startup from that backend's connection config (env var/secret store) and held as a singleton for the app's lifetime — not created per-request and not created per-tenant. All tenants sharing a given backend instance (e.g. all tenants whose collections live on the platform's single self-hosted Qdrant) share that one client/connection pool — isolation is **not** achieved via separate physical connections, but it is also **not** filter-only: it is enforced by the vector backend itself per the **Tenancy Matrix** below. If "cloud-managed mode" later needs per-tenant vector DB endpoints (e.g. tenant A on their own Qdrant Cloud cluster, tenant B on another), that is a distinct future capability — a `backend_instance_id` resolved per-collection alongside `backend` — and is explicitly out of scope for v1, where each backend name maps to exactly one configured instance. Document this singleton-per-backend assumption in `registry.py`'s docstring so it isn't silently violated later.
- **Tenancy Matrix (tenant isolation is a security boundary — enforced by the backend, never by filter logic alone):**
  - **Weaviate** → native multi-tenancy: tenant-enabled class (`multiTenancyConfig.enabled: true` at creation); each tenant owns a dedicated shard; requests scoped by tenant name; `ensure_tenant` = idempotent `tenants.create` before first use; gate: Weaviate ≥ 1.21.
  - **Qdrant** → native collection multi-tenancy: collection created with `multi_tenancy_config`; `create_tenant` per tenant; queries pass `tenant` (dedicated shard per tenant); `ensure_tenant` = idempotent `create_tenant`; gate: Qdrant ≥ 1.10.
  - **Milvus** → partition-per-tenant: one partition per tenant; inserts route by partition, searches prune to `partition_names=[tenant]`; `ensure_tenant` = idempotent `create_partition`.
  - **Chroma** → per-tenant physical collection: no native tenancy exists; each (tenant, platform collection) pair is its own physical collection, created on demand.
  - **Fallback (documented, small self-hosted deploys only):** shared collection + `tenant_id` payload filter — **not** a security boundary; acceptable only when tenants are trusted namespaces, never external customers.
  - Provisioning is **lazy and idempotent**: backend tenants/partitions/physical collections are created on first collection creation via `ensure_tenant` (create if absent, no-op if present); no eager backfill. The service layer asserts `collection.tenant_id == principal.tenant_id` before every adapter call (defense-in-depth, never a substitute for backend enforcement). `CapabilityMatrix` exposes `tenancy_model` per backend.
- **Collection identity & physical naming:** the platform collection `name` is client-facing metadata stored in Postgres; the physical backend object (Weaviate class, Qdrant collection, Milvus collection, Chroma collection) is named with an opaque UUID — `col_<uuid>` — generated at creation and stored in the registry, which owns the mapping exclusively. Collision-free by construction (tenants can share names), no information leakage, and adapters never see client-facing names, routes never see physical names.
- **Service layer is mandatory between routes and adapters — routes never call adapters directly.** Every route handler goes through a dedicated service class in `app/services/`: `CollectionService`, `VectorService`, `SearchService`, `TenantService`, `AuthService`, `JobService`. A route's job is request validation (Pydantic) and response shaping only; a service's job is orchestrating the adapter (via `AdapterRegistry`) + Postgres + audit log + job queue, and enforcing cross-cutting rules (permission checks beyond the route dependency, tenant scoping, quota enforcement — e.g. "always derive `tenant_id` from the authenticated principal, never the request body"). This keeps adapters thin and backend-specific, and business logic centralized and testable in isolation from HTTP concerns.

- **Framework:** FastAPI (async), Pydantic v2 for schemas, `uvicorn`/`gunicorn` with `uvicorn.workers.UvicornWorker` for prod.
- **Dependency management:** `uv` (pyproject.toml + uv.lock). Use `uv sync` in Docker build stages, not raw pip.
- **Metadata/control-plane DB:** PostgreSQL (tenants, users, roles, API keys, collections registry, audit logs) via SQLAlchemy 2.0 async + Alembic migrations. Driver: `asyncpg`.
- **Cache / rate limiting / job queue broker:** Redis.
- **Background jobs:** `arq` (pinned — not Celery). Rationale: the stack is async-first end-to-end (FastAPI async, SQLAlchemy 2.0 async, `asyncpg`), and `arq` is async-native and Redis-backed like the rest of the caching/rate-limit layer, avoiding Celery's separate sync-worker model and broker/backend split. Only fall back to Celery if a concrete Phase 6 limitation is hit (e.g. scheduling/ecosystem tooling `arq` can't cover) — and if so, record the reason in the Progress Log before switching.
- **Vector DB clients:** official SDKs — `weaviate-client`, `qdrant-client`, `pymilvus`, `chromadb`.
- **Auth:** `pyjwt` for JWT, `passlib[bcrypt]` for password hashing, FastAPI's `OAuth2PasswordBearer` plus API-key auth for service-to-service calls.
- **Observability:** structured logging (`structlog`), Prometheus metrics via `prometheus-fastapi-instrumentator`, OpenTelemetry tracing, Sentry for error tracking. **Request/trace correlation is mandatory, not optional:** every inbound request gets a request ID (generate if not supplied via an `X-Request-ID` header, echo it back in the response header), bound into the `structlog` context for that request's entire lifecycle, and propagated into the OpenTelemetry trace ID so a single request's logs across FastAPI → service layer → adapter call → Postgres/Redis → `arq` job enqueue can all be correlated by one ID. This matters more than usual here because a single request can fan out across Postgres, Redis, `arq`, and one of four vector DB backends — without correlation, debugging a failure means grepping four log streams by timestamp. **Landed as a Phase 7 pull-forward:** `app/middleware/tracing.py` (public app only, outermost) extracts/generates the request ID, echoes it, binds `request_id` + 32-hex `trace_id` into the structlog contextvars, and starts a root span whose trace ID is **deterministically derived from the request ID** (`app/core/tracing.py::derive_trace_id`) — for a UUID4 request ID the trace ID *is* the request ID, so logs and traces correlate by construction, no lookup needed. Export is env-driven (`OTEL_EXPORTER_OTLP_ENDPOINT`; unset = spans correlated but dropped), the admin app deliberately stays middleware-free, and the shared post-routing route template lives in `app/middleware/routing.py`.
- **Testing:** `pytest`, `pytest-asyncio`, `httpx.AsyncClient`, `testcontainers` to spin up real Qdrant/Weaviate/Milvus/Chroma in CI. Note: Milvus standalone requires etcd + MinIO as sidecars, making its testcontainer suite notably heavier/slower than the others — run it as a separate CI job so a Milvus timeout doesn't block the rest of the pipeline, and consider gating it to run on PRs touching `adapters/milvus_adapter.py` or on a nightly schedule rather than every push.
- **Cross-tenant isolation suite (isolation is a security boundary — this is the acceptance gate):** every adapter phase is complete only when its backend passes **Layers 1 and 3** (Layer 2 is backend-independent and lands with Phase 3). Full design: `docs/superpowers/specs/2026-08-14-tenant-isolation-tests-design.md`. Three layers:
  - **Layer 1 — adapter isolation tests** (testcontainers, per backend): a shared contract suite (same-ID isolation; query scoping with oversized `top_k`; delete scoping; fail-closed unscoped/mis-scoped queries; `ensure_tenant` idempotency; hybrid scoping) plus per-backend mechanism tests proving the *native* tenancy isolates — e.g. Qdrant unscoped query on a multi-tenant collection errors; Milvus search without `partition_names` returns nothing (proving inserts *and* searches route); Chroma's per-tenant physical names are distinct. All tests use **indistinguishable data** (same IDs + same vectors across tenants, `_tenant_probe` payload markers) and the **fail-closed** contract (error or empty — never cross-tenant rows), asserted behaviorally, never by call shape.
  - **Layer 2 — service-layer routing tests** (recording stub adapter, no containers): assert the tenant assertion fires before any adapter call; physical names resolve only from the principal's registry row; forged `tenant_id` in a body is rejected at the schema (`422`, `extra="forbid"` — the envelope has no `tenant_id` field); batch objects stage under the principal's tenant key.
  - **Layer 3 — API/e2e tests** (two principals per backend): collection-name collision; cross-tenant GET/DELETE/PATCH → `COLLECTION_NOT_FOUND`; forged `tenant_id`; vector-ID collision; batch-path scoping; hybrid scoping; tenant-scoped listing; and a negative control proving responses can't act as an existence oracle.
- **pymilvus async note:** pymilvus's native async support has historically lagged `weaviate-client`/`qdrant-client`/`chromadb`. Before committing the Milvus adapter to a fully-`async def` interface in Phase 5, verify current SDK support; if it's still sync-only, wrap calls with `asyncio.to_thread` / a threadpool executor rather than blocking the event loop, and note this explicitly in the adapter's docstring.
- **Docs:** FastAPI's built-in OpenAPI/Swagger, customized with tags per resource group.

## Auth, RBAC, and Multi-Tenancy (Enterprise tier)

- **JWT auth:** access token (short-lived, ~15 min) + refresh token (longer-lived, rotated and stored **hashed in Postgres** with a `revoked` flag, so logout/compromise can actually invalidate it — don't rely on stateless-only JWTs for refresh tokens). Access tokens are **revocable too**: `logout` requires the bearer access token and deny-lists its `jti` in a Postgres-backed `revoked_tokens` table (checked at the auth boundary in `get_current_principal`, returning `401 AUTH_TOKEN_REVOKED`; stale rows purged opportunistically against the access TTL). This is the source of truth; Redis (app.core.cache) fronts the deny-list with **positive markers only** (written write-through at logout, read-through on a Postgres hit, TTL = access-token TTL — a miss is never cached as negative, so a revocation is never missed) and caches API-key principals (populated on authenticate, **invalidated on revoke**, TTL bounded by key expiry). The cache fails soft: on any Redis error the Postgres path runs — it's an optimization, never a gate. Use `pyjwt` for signing/verification and FastAPI's `OAuth2PasswordBearer` for the dependency wiring; hash passwords with `passlib[bcrypt]`.
- **API keys:** for server-to-server/programmatic access, scoped per tenant, hashed at rest, with optional expiry and per-key rate limits.
- **RBAC model:** roles (`owner`, `admin`, `editor`, `viewer`) at the tenant level, plus **resource-level permissions** on individual collections (e.g., a user can be `viewer` on the tenant but `editor` on one specific collection). Implement as a permission-checking dependency (`require_permission("collection:write")`) injected into routes.
- **Multi-tenancy:** every resource (collections, API keys, audit logs) is scoped by `tenant_id`, always derived from the authenticated principal — never from the request body. Vector-DB isolation is backend-enforced per the **Tenancy Matrix** (see Architecture); the service layer additionally asserts `collection.tenant_id == principal.tenant_id` before any adapter call (defense-in-depth, not the boundary). Support tenant-level resource quotas (max collections, max vectors, max QPS).
- **Audit logging:** every write operation (collection create/delete, vector upsert/delete, role changes, API key creation) writes an immutable audit record (actor, tenant, action, resource, timestamp, result) to a dedicated `audit_log` table; expose `GET /audit-logs` for admins with filtering. Enforce immutability at the DB level, not just app convention, with two layers (both in the Phase 1 migration): (1) **two Postgres roles** — `app` gets `INSERT`/`SELECT` only on `audit_log` (no `UPDATE`/`DELETE` grants), and Alembic migrations run as the `migrator` role, which holds DDL; (2) a **guard trigger** on `audit_log` that raises on any `UPDATE`/`DELETE`, applied to **all roles including `migrator`** — dropping it is a visible DDL event, so the property is "impossible to defeat quietly", not "impossible to defeat". **Threat model:** this defends against the app's own failure modes (bugs, misconfigured grants, compromised app credentials), **not** against superuser/`postgres` or physical DB access.
- **Rate limiting:** Redis-backed sliding-window or token-bucket limiter, configurable per tenant/per API key/per route. All applicable limits (tenant, API key, route) are checked on every request; **most restrictive wins** — the first limiter to reject the request returns `429` + `Retry-After`, and the response body's `details` field names which limit was hit (e.g. `"tenant_qps"` vs `"api_key_qps"`) so clients can distinguish.
- **CORS:** configurable via env (`CORS_ALLOWED_ORIGINS`, comma-separated). Default to permissive (`*`) in local/dev compose, and require an explicit allow-list in staging/prod config — document this in `.env.example` with both examples.

## API Surface (high level)

All routes below are prefixed with `/api/v1` (matches the `app/api/v1/` package layout) — e.g. `POST /api/v1/collections`. Exceptions: `/health` and `/metrics` are **not on the public app at all** — they live on a separate internal ASGI app (`app/admin.py`) served by the same process on `ADMIN_HOST:ADMIN_PORT` (default `127.0.0.1:9091`; see the /health note below). Public traffic physically cannot reach the probe/scrape endpoints: they 404 on the public app. `python -m app.main` runs both apps in one process (`run()` in `app/main.py`); the admin app must share the process with the public app because the Prometheus registry is process-local.

```
POST   /api/v1/auth/register
POST   /api/v1/auth/login
POST   /api/v1/auth/refresh
POST   /api/v1/auth/logout
GET    /api/v1/auth/me

GET    /api/v1/capabilities                     # backend feature matrix

POST   /api/v1/tenants                          # platform-admin only
GET    /api/v1/tenants/{id}
GET    /api/v1/tenants/{id}/members              # tenant directory, cursor-paginated (viewer+; `limit` 1-200, default 50, opaque `cursor`)
POST   /api/v1/tenants/{id}/members              # provision a member account (admin/owner; initial password set by inviter)
PATCH  /api/v1/tenants/{id}/members/{user_id}    # change a member's role (admin/owner; last owner cannot be demoted)

POST   /api/v1/collections                      # body includes `backend` (weaviate|qdrant|milvus|chroma)
GET    /api/v1/collections
GET    /api/v1/collections/{name}
DELETE /api/v1/collections/{name}
GET    /api/v1/collections/{name}/permissions   # list resource-level grants, cursor-paginated (admin/owner; `limit` 1-200, default 50, opaque `cursor`)
PATCH  /api/v1/collections/{name}/permissions   # RBAC grants on this collection
DELETE /api/v1/collections/{name}/permissions/{user_id}   # revoke a user's resource-level grant (admin/owner; idempotent)
PATCH  /api/v1/collections/{name}/config        # mutate index config post-creation (HNSW params, etc. — see note below)

POST   /api/v1/collections/{name}/vectors                 # upsert (single/batch), accepts pre-computed vectors
POST   /api/v1/collections/{name}/vectors/batch            # async job, returns job_id
GET    /api/v1/jobs/{job_id}
DELETE /api/v1/collections/{name}/vectors/{id}
GET    /api/v1/collections/{name}/vectors/{id}

POST   /api/v1/collections/{name}/query                    # similarity search, supports filters, top_k
POST   /api/v1/collections/{name}/hybrid-query              # where backend supports it

POST   /api/v1/collections/{name}/reindex                   # stub in v1 — see reindex note below

GET    /api/v1/admin/audit-logs
GET    /health                                  # admin app (ADMIN_HOST:ADMIN_PORT), not public — see note below
GET    /metrics                                 # admin app (ADMIN_HOST:ADMIN_PORT), not public
```

**On the admin app (`app/admin.py`, canonical):** `/health` and `/metrics` are served by a separate ASGI app on `ADMIN_HOST:ADMIN_PORT` (default `127.0.0.1:9091`) and are **absent from the public app** (404s there). Both endpoints are unauthenticated by design — scrapers/probes can't do auth — so the security boundary is network exposure: the admin port must never be published (compose) or exposed via Ingress/NodePort/LoadBalancer (k8s). `ADMIN_HOST` defaults to `127.0.0.1` (safe single-host); container orchestrators set `ADMIN_HOST=0.0.0.0` so pod/container-network probes reach it. The admin app deliberately carries no middleware (no rate limiting, no CORS, no metrics/access-log middleware, no docs, no auth) — its own traffic must not consume rate-limit budget or pollute the request counters it exposes. It must run in the same process as the public app (see `run()` in `app/main.py`, which owns the process's SIGINT/SIGTERM and stops both servers): the Prometheus registry is process-local, so a standalone admin container would scrape an empty registry.

**On `GET /health` (canonical — supersedes any other description of this endpoint):** must report more than "the API process is alive." Return an overall status plus a per-dependency breakdown: Postgres (connection + simple query), Redis (ping), background worker liveness (`arq` last-heartbeat check), and each adapter registered in `AdapterRegistry` (calling that adapter's `health_check()`). Shape:
```json
{"status": "ok"|"degraded"|"down", "checks": {"postgres": "ok", "redis": "ok", "workers": "ok", "adapters": {"qdrant": "ok", "weaviate": "down", ...}}}
```
Return `200` only if all *critical* dependencies (Postgres, Redis) are healthy. If one or more vector DB adapters are down but the platform itself is still serviceable, return `200` with overall `"status": "degraded"` and per-backend detail — don't fail k8s liveness/readiness probes and trigger a restart loop over a single backend outage (e.g. a transient Qdrant Cloud blip) when other backends and the control plane are fine. Overall `"status": "down"` (non-200) is reserved for Postgres or Redis being unreachable. The route is served by the admin app (see the admin-app note above) — k8s probes and Prometheus scrape the admin port, never the public one.

**On `PATCH /collections/{name}/config`:** not every param is mutable on every backend without a reindex (e.g. changing HNSW `m`/`ef_construction` or distance metric generally requires rebuilding the index; some backends won't allow it live at all). v1 scope: support the subset each adapter can apply without a full rebuild (documented per-adapter in `CapabilityMatrix`), and for anything else return a `409` with error_code `REQUIRES_REINDEX` (see Error Code Taxonomy below) rather than silently no-opping or failing generically. The `409` response's `details` field must include `"next_step": "POST /api/v1/collections/{name}/reindex"` so the client has a stated destination instead of a dead end.

**On `POST /collections/{name}/reindex` (v1 = stub, not a full implementation):** this route must exist in v1 so `REQUIRES_REINDEX` responses point somewhere real, but it returns `501 Not Implemented` with `error_code: "REINDEX_NOT_IMPLEMENTED"` and a `details.message` explaining that full reindex-as-a-job lands in a later phase. Do not silently 404 this route and do not implement the actual reindex job in v1 — just the honest stub. Track the real implementation as a follow-up in the README.

**On embeddings:** v1 is **vectors-in, vectors-out only** — the platform does not call out to an embedding provider (OpenAI, Cohere, etc.) on the client's behalf. `POST /vectors` and `/query` require pre-computed float arrays from the caller. This keeps the platform provider-agnostic and avoids owning embedding-model versioning/cost. If/when text-in embedding support is wanted, it should land as an optional `services/embeddings/` layer with a pluggable provider interface — treat it as a distinct future phase, not folded into Phase 3.

**On hybrid search (`POST /collections/{name}/hybrid-query`):** the vectors-in philosophy holds — the platform does not tokenize text or call embedding providers; the client who owns their tokenizer supplies the sparse side. Request shape: `vector` (required), `sparse_vector` (required on Qdrant/Milvus — see record schema), `query_text` (required on Weaviate, driving its BM25 side against the inverted index), and a single normalized `alpha` (0.0 = pure keyword, 1.0 = pure dense, default 0.75) translated per backend (Weaviate alpha; Qdrant prefetch weights + RRF fusion; Milvus `WeightedRanker`/`RRFRanker`). Chroma does not support hybrid. Errors are disjoint: Chroma → `400 VALIDATION_UNSUPPORTED_OPERATION` with `details.capability` naming `hybrid_search`; Qdrant/Milvus with sparse input missing → `422 VECTOR_SPARSE_REQUIRED`. `CapabilityMatrix` exposes `hybrid: {mode: text+vector | sparse+vector | false, sparse_required}` as the canonical introspection point clients check before calling.

**On async batch jobs (`POST /collections/{name}/vectors/batch`):** the payload never transits Redis/arq. The request is `application/x-ndjson` (one vector record per line, streamed — never buffered whole) and is staged to object storage — MinIO in compose / S3 in cloud mode via boto3 (`BATCH_STORAGE_*` config; MinIO already exists in the stack as the Milvus sidecar) — at `{tenant_id}/{job_id}.jsonl`; tenant quota is checked at **enqueue time**. The `arq` job receives only `{job_id, object_key}`, streams the file, validates per line, and upserts in chunks through the adapter's batch path; per-vector outcomes stream to `{tenant_id}/{job_id}.results.jsonl`, and `GET /jobs/{job_id}` reports status/counts. Retry is safe because upserts are idempotent. Whole-file validation failure → `JOB_PAYLOAD_INVALID`. Direct-to-storage pre-signed upload and parquet payloads are later-phase optimizations, not v1.

**Batch throughput model (design session 2026-08-14) — the wire format dominates:** JSON text is ~10 B/float, so a 100k × 1536-dim job is ~1.6 GB JSONL (the largest term in the whole path). Raw JSONL meets the v1 budget as designed: modeled job time is ~15–45 s across all four backends; enqueue is bandwidth-bound (~3–30 s on localhost/LAN, minutes on slow client links — the numbers that justify the later pre-signed direct-upload phase). Three structural consequences are **required, not optional**: (1) **chunked upsert is a first-class adapter contract** — `batch_upsert(chunk_size)` with per-backend sizing (Qdrant 5–10k/request, Weaviate ~1k with server-side batching, Milvus 1–10k, Chroma 100–1k) and backpressure; the worker never assumes one chunk size fits all; (2) **the worker runs a bounded read→parse→upsert pipeline** (staged read-ahead queues) so parse/validation (~7–15 s for 100k, single-threaded) overlaps with ingest — otherwise fast-ingest backends (Qdrant/Milvus) become parse-bound; (3) **Chroma is the throughput floor** (~10–30 s for 100k, and it degrades at scale) — see Phase 3 soak below. gzip payloads (stream-to-stream, memory-neutral, ~3–4× smaller) are a cheap later knob when storage/transfer cost matters.

**On `backend` immutability:** a collection's `backend` (weaviate|qdrant|milvus|chroma) is set once at creation time and is **immutable for the life of the collection**. There is no v1 endpoint or code path that changes a collection's backend after creation, and none should be added without an explicit, separate migration-job design (cross-backend vector migration is out of scope for v1 — moving data would require a full read-from-A/write-to-B job, not a metadata PATCH). If this is ever needed, it must be a new `POST /collections/{name}/migrate` capability designed and scoped on its own, not folded into `PATCH /config`.

## Vector & Batch Limits (Non-Functional)

To close DoS exposure on the vector write/query paths, the following limits are mandatory and must be enforced by Pydantic validation at the schema layer (not just documented) before Phase 1 route handlers ship:

- **Max vector dimension:** 4096 floats per vector (configurable via `VECTOR_MAX_DIMENSION` env var / `pydantic-settings`, default 4096). Requests exceeding this return `422` with error_code `VECTOR_DIMENSION_EXCEEDED`.
- **Max batch size, sync path (`POST /vectors`):** 100 vectors per request. Above this, the client must use the async path (`POST /vectors/batch`), which has no hard cap but is subject to tenant quotas. Sync requests exceeding 100 return `422` with error_code `BATCH_SIZE_EXCEEDED` and a `details.hint` pointing at the async endpoint.
- **Max `top_k` on `/query` and `/hybrid-query`:** 1000. Requests exceeding this return `422` with error_code `TOP_K_EXCEEDED`. This is a platform-wide ceiling regardless of what a given backend would technically allow.
- **Max sparse vector cardinality:** 100,000 non-zero entries per sparse vector (configurable via `SPARSE_MAX_CARDINALITY`, default 100000). Requests exceeding this return `422` (VALIDATION_*). Re-verify against backend caps during Phases 4–5 and lower the platform default if any backend caps lower.
- These four limits are part of the standard vector record/query contract and must be documented in the OpenAPI schema (Pydantic `Field` constraints + descriptions), not just enforced silently.

## Error Code Taxonomy

All error responses use the standard `{error_code, message, details}` shape (see Non-Functional Requirements). `error_code` values are namespaced by domain, `SCREAMING_SNAKE_CASE`, and defined as an enum in `app/core/exceptions.py` — routes and services raise typed exceptions that map to these, never raw strings. Namespaces (extend within a namespace as needed, but don't introduce new namespaces without updating this list):

- `AUTH_*` — e.g. `AUTH_INVALID_CREDENTIALS`, `AUTH_TOKEN_EXPIRED`, `AUTH_TOKEN_REVOKED`, `AUTH_INSUFFICIENT_SCOPE`, `AUTH_EMAIL_TAKEN`
- `API_KEY_*` — e.g. `API_KEY_NOT_FOUND`
- `TENANT_*` — e.g. `TENANT_NOT_FOUND`, `TENANT_ALREADY_EXISTS`, `TENANT_MEMBER_NOT_FOUND`, `TENANT_LAST_OWNER`, `TENANT_QUOTA_EXCEEDED`
- `COLLECTION_*` — e.g. `COLLECTION_NOT_FOUND`, `COLLECTION_ALREADY_EXISTS`, `COLLECTION_BACKEND_UNAVAILABLE`, `REQUIRES_REINDEX`, `REINDEX_NOT_IMPLEMENTED`
- `VECTOR_*` — e.g. `VECTOR_NOT_FOUND`, `VECTOR_DIMENSION_MISMATCH`, `VECTOR_DIMENSION_EXCEEDED`, `BATCH_SIZE_EXCEEDED`, `TOP_K_EXCEEDED`, `VECTOR_SPARSE_REQUIRED` (hybrid requested without sparse input/support on Qdrant/Milvus)
- `JOB_*` — e.g. `JOB_NOT_FOUND`, `JOB_FAILED`, `JOB_PAYLOAD_INVALID` (whole-file validation failure on a batch job)
- `RATE_LIMIT_*` — e.g. `RATE_LIMIT_TENANT_QPS`, `RATE_LIMIT_API_KEY_QPS`, `RATE_LIMIT_ROUTE_QPS` (these are the values that populate `details` on a `429`, per the Rate Limiting section above)
- `VALIDATION_*` — generic Pydantic validation failures not covered by a more specific code above; e.g. `VALIDATION_UNSUPPORTED_OPERATION` (operation unsupported by a backend — `details.capability` names it), `VALIDATION_INVALID_CURSOR` (malformed pagination cursor)

Every new route added in any phase must map its error conditions to one of these namespaces before merging; if none fits, add the new code to this list in the same commit (not as an afterthought).

## Project Structure

```
app/
  main.py
  core/            # config, security, rate_limit, logging, exceptions
  db/              # SQLAlchemy models, session, Alembic migrations
  schemas/         # Pydantic request/response models
  adapters/
    base.py        # VectorDBAdapter ABC
    weaviate_adapter.py
    qdrant_adapter.py
    milvus_adapter.py
    chroma_adapter.py
    registry.py     # AdapterRegistry/factory
  api/
    v1/
      auth.py
      tenants.py
      collections.py
      vectors.py
      jobs.py
      admin.py
  services/        # business logic layer between routes and adapters
  workers/         # arq tasks (background jobs — see pinned choice in Tech Stack)
  middleware/      # auth, rate limit, tenant context, audit logging
tests/
  unit/
  integration/     # testcontainers-based
    adapters/      # one suite per adapter (weaviate/qdrant/milvus/chroma), real backend via testcontainers
    services/      # service-layer integration tests against real Postgres (testcontainers), independent of any vector adapter — e.g. TenantService, AuthService, JobService correctness without needing a vector DB spun up
  e2e/
deploy/
  docker-compose.yml          # app + postgres + redis + qdrant + weaviate + milvus + chroma
  docker-compose.cloud.yml    # app + postgres + redis only, others point to managed endpoints
  k8s/
    base/
    overlays/{dev,staging,prod}
  helm/ (optional)
alembic/
.env.example
.gitignore
pyproject.toml
README.md
CLAUDE.md
```

## Deployment Requirements

- **Docker Compose (local/dev):** full stack including self-hosted Qdrant, Weaviate, Milvus (standalone, via its docker-compose with etcd+minio), and Chroma. Use multi-stage Dockerfile, non-root user, healthchecks. MinIO — already present as the Milvus sidecar — also hosts the async batch staging bucket (see API Surface notes).
- **Kubernetes (production):** namespaced manifests/Helm chart for the FastAPI app (Deployment + HPA + Service + Ingress), Postgres (or point to managed RDS/Cloud SQL), Redis (or managed), and StatefulSets for self-hosted vector DBs where applicable; use Secrets/ConfigMaps for credentials, readiness/liveness probes hitting the admin app's `/health` (`ADMIN_HOST=0.0.0.0`, port `ADMIN_PORT`, no public Service/Ingress for it — the admin port is internal by construction), and resource requests/limits tuned per service.
- **Cloud-managed mode:** config-driven — each adapter reads its connection info from env vars/secret store, so the same app image can run against Qdrant Cloud, Weaviate Cloud, Zilliz Cloud (managed Milvus), with self-hosted Chroma as the only one needing a server. Batch staging is S3-compatible (`BATCH_STORAGE_*` env), so the same image works against MinIO or AWS S3. Document all of this clearly in `.env.example`.
- Include CI (GitHub Actions) running lint (`ruff`), type-check (`mypy`), tests with testcontainers, and Docker image build.
- **Local pre-commit hooks:** set up `pre-commit` (config in `.pre-commit-config.yaml`) running `ruff check`, `ruff format`, and `mypy` on staged files, installed as part of Phase 1 scaffolding. This exists because of the "commit after every meaningful change" workflow below — with frequent small commits, lint/type failures need to surface before a commit lands, not several commits later in CI.

## Non-Functional Requirements

- All inputs validated via Pydantic; never trust client-supplied tenant/owner IDs.
- Idempotent upserts (support client-supplied vector IDs).
- Pagination on all list endpoints (cursor-based preferred for vector results).
- Consistent error schema (`{error_code, message, details}`) across all adapters — map each backend's native errors into this shape.
- Secrets never logged; structured logs scrub PII/API keys.
- Target: handle batch upserts of 100k+ vectors via background jobs without blocking request threads.
- **Data retention & deletion compliance:** collection/vector deletes must be genuinely hard deletes at the backend level, not soft-deletes-only in Postgres — when a client deletes a collection or vector, the corresponding adapter call to actually remove the data from the backend (Weaviate/Qdrant/Milvus/Chroma) is part of the same operation, not a deferred cleanup job. Since collection metadata (arbitrary `dict[str, Any]`) can contain customer PII, document in the README that "delete" is destructive and immediate at the vector-DB level across all four backends, and note any backend-specific caveats (e.g. a backend's own soft-delete/tombstone/compaction behavior before space is reclaimed) per adapter in `CapabilityMatrix`. Full GDPR-style "right to erasure" tooling (e.g. cross-tenant PII search) is out of scope for v1 — note it as a future phase in the README rather than silently ignoring it.

## Autonomy

You have full access to design and implement this system end-to-end without pausing for confirmation within a phase. Make reasonable, production-sound decisions on any ambiguous point (embedding dimension defaults, Milvus deployment mode, cloud provider specifics for K8s, etc.), document the decision and rationale briefly in the README or inline comments, and keep moving. Only stop if something is destructive/irreversible (e.g., dropping a production database, force-pushing over existing history) or genuinely blocks progress (missing credentials, conflicting requirements that can't be reconciled).

When a session starts, work on the next unchecked phase below unless told otherwise. When a phase is complete and committed, stop, summarize what was built, and tick the box in the Progress Log — don't auto-continue into the next phase in the same session unless I explicitly ask you to keep going.

## Build Phases

1. [x] **Scaffold** — project structure, `.gitignore`, git init, config management (pydantic-settings), `pre-commit` hooks (`ruff` + `mypy`), Postgres models (incl. opaque `physical_name` on the collections registry), Alembic (incl. two-role provisioning + `audit_log` guard trigger), initial commits.
2. [x] **Auth & RBAC** — register/login/JWT/refresh, roles, API keys, tenant model, permission-checking dependency.
3. [ ] **Adapter interface + Chroma adapter first** (simplest, easiest to test locally) — prove the abstraction end-to-end with collections + vectors + query routes; Chroma exercises the per-tenant-collection tenancy model, opaque physical naming, `backend_status` on the read path, and the **100k-vector ingest soak** (adapter-level `batch_upsert`) validating the throughput floor — re-validated end-to-end when the batch path lands in Phase 6; Chroma must also pass the cross-tenant isolation suite (Layers 1–3), the security-boundary acceptance gate.
4. [ ] **Qdrant and Weaviate adapters** — native multi-tenancy (Qdrant ≥ 1.10, Weaviate ≥ 1.21, `ensure_tenant`), hybrid search (client-supplied sparse + normalized `alpha`), filtering, expand integration tests via testcontainers; both backends must pass the isolation suite incl. hybrid-scoping cases.
5. [ ] **Milvus adapter** — partition-per-tenant routing, sparse support for hybrid, index types, consistency levels, expand integration tests via testcontainers; Milvus must pass the isolation suite incl. the no-`partition_names` fail-closed proof.
6. [ ] **Capability matrix endpoint (incl. `tenancy_model` + `hybrid` mode fields), async batch jobs** (`arq` + object-storage staging, JSONL, results objects), audit logging middleware, Redis rate limiting.
7. [ ] **Observability** — structured logs, Prometheus metrics, tracing, Sentry.
8. [ ] **Deployment artifacts** — Dockerfile, docker-compose (full + cloud variants, incl. MinIO batches bucket / `BATCH_STORAGE_*` S3 config), k8s manifests/Helm, CI pipeline.
9. [ ] **Docs & polish** — OpenAPI tags/examples, README (architecture diagram, setup instructions, tenancy matrix, hybrid contract, batch data path, drift non-goal, audit threat model), load test (k6/Locust) script for at least one adapter.

## Progress Log

_Update this section at the end of each phase. Newest entry on top._

- **2026-08-14** — Pull-forward: k8s-ready scraping contract — `prometheus/prometheus.k8s.yml` (a ConfigMap-ready variant of the compose config) discovers the app pod by the standard annotation recipe (`kubernetes_sd_configs` role pod, keep `prometheus.io/scrape=true`, path + port relabels, pod-label labelmap); the pod must carry `prometheus.io/scrape: "true"`, `prometheus.io/path: "/metrics"`, `prometheus.io/port: "<ADMIN_PORT>"`. README gains a Kubernetes section: the annotation contract table, `ADMIN_HOST=0.0.0.0` (pod-network probes) and never-expose-the-admin-port, and dashboard provisioning via ConfigMap. **One source of truth test:** the k8s annotation port, the compose static target (`app:<port>`), and `Settings.admin_port` (the flag that exposes /metrics only on the internal admin app) must all agree — changing the flag forces updating the contract. `test_monitoring_compose.py` +2 tests (7).

- **2026-08-14** — Pull-forward: monitoring trio compose + a real bug the smoke test caught — `deploy/monitoring/docker-compose.yml` (prometheus v3.13 LTS + alertmanager v0.33.1 + grafana v13.1.0) mounts the shipped configs read-only, plus `prometheus/prometheus.yml` (scrapes the admin app at `app:9091`, loads `alerts.yml`, forwards to `alertmanager:9093`) and Grafana provisioning (Prometheus datasource + auto-import of the `vhk-platform` dashboard). **Correction to the earlier routing/inhibition/fields entries:** Alertmanager does **not** expand `${VAR}` in its config — it parses the literal string, so the config as previously shipped (`api_url: ${SLACK_WEBHOOK_URL}`) crashed on boot with `unsupported scheme "" for URL`. Smoke-testing the compose caught it; the config now uses the Alertmanager-native `api_url_file`/`routing_key_file` fields reading `/run/secrets/...`, and the compose sources both vars as environment secrets (k8s mounts a Secret at the same paths — the k8s-ready pattern). Verified live end-to-end: all 7 rules loaded into Prometheus, alertmanager healthy with the file-based credentials, prometheus→alertmanager forwarding active, grafana datasource + dashboard provisioned. Note: this Windows box dynamically excludes host ports 9090/9093 (bind fails though `netsh` shows nothing) — a machine quirk, not a compose issue. `tests/unit/test_monitoring_compose.py` (5 tests) pins service/mount/secret wiring and the config↔secret path contract; the alertmanager config test now asserts `_file` fields and bans `${VAR}`.

- **2026-08-14** — Pull-forward: request duration in the access log + slow-request threshold — every `request_completed` line now carries `duration_ms` (wall time measured in `app/middleware/metrics.py`, includes rate limiting and inner middleware) and a `slow` flag; requests at/above `SLOW_REQUEST_THRESHOLD_MS` (default 1000) log the *same event* at WARNING with `slow=true` so latency outliers surface in the log stream without breaking access-log uniformity. Unit tests rewritten as two deterministic cases (INFO fast line with exact fields; WARNING slow line with `slow=true`).

- **2026-08-14** — Pull-forward: rich Slack alert fields — the Slack receiver in `deploy/monitoring/alertmanager/alertmanager.yml` posts structured attachment fields (Alert, Severity, Summary, Value, Check, Limit) beside the detail text instead of one text blob; field values are templated from the alert payload (`.CommonAnnotations.summary`, `range .Alerts` for value, `with/else` guards so absent `check`/`limit` labels render a placeholder rather than `<no value>`). `test_monitoring_config.py` +1 test (10) pins the required fields, their payload sources, and the missing-label guards.

- **2026-08-14** — Pull-forward: Alertmanager inhibition — `inhibit_rules` in `deploy/monitoring/alertmanager/alertmanager.yml` suppress the downstream symptom warnings (5xx spike, 429 flood, stale worker heartbeat, high p99) while `VhkCriticalDependencyDown` fires, so a control-plane outage pages once instead of six times. Vector-backend alerts (`VhkBackendDown`/`VhkBackendFlapping`) are deliberately **not** inhibited — backends are physically independent of the control plane, so a real backend outage still pages during a Postgres/Redis incident. No `equal` labels (single logical instance). `test_monitoring_config.py` +1 test (9): airtight coverage check — every emitted warning is either inhibited or named as an independent backend rule, so a new warning rule forces a routing decision; the extraction caught two parser-ordering bugs in the test itself before the contract test could pass.

- **2026-08-14** — Pull-forward: per-route latency isolation in the Grafana dashboard — `platform.json` gains a `route` template variable (multi-select + All, populated from the instrumentator's `handler` label via `label_values(http_request_duration_highr_seconds_bucket, handler)`) and a "Route latency percentiles (per handler)" panel plotting p50/p95/p99 `by (le, handler)` filtered with `handler=~"$route"` — hot vector-query routes (`.../collections/{name}/query`, `.../hybrid-query`) can be isolated from control-plane traffic, or All decomposes the global latency panel per route. `test_monitoring_config.py` +1 test (8) pins the variable contract and the panel wiring.

- **2026-08-14** — Pull-forward: Alertmanager routing for the monitoring stack — `deploy/monitoring/alertmanager/alertmanager.yml` maps the rules' `severity` labels to receivers: `critical` → PagerDuty (the only severity that drives /health to 503, so the only one that pages), `warning`/default → Slack, grouped by `alertname`/`check`/`limit` (30s group_wait, 4h repeat_interval). Credentials are env-only (`SLACK_WEBHOOK_URL`, `PAGERDUTY_ROUTING_KEY`, Alertmanager `${VAR}` expansion) — no secrets in the repo; notification title/text templates in `alertmanager/templates/vhk.tmpl` (referenced via a templates glob). `tests/unit/test_monitoring_config.py` +3 tests (8): routing covers every severity the rules emit (critical → pager, warning → default slack — a new severity can't silently fall through), env-only credential assertion (no literal webhook/token markers), and template reference/definition coupling. README documents the mount points + Prometheus `alerting:` forwarding.

- **2026-08-14** — Pull-forward: request/trace correlation middleware (`app/middleware/tracing.py`, public app only, outermost) — every request gets an `X-Request-ID` (client value echoed verbatim, else UUID4 hex), the request ID **is** the OTel trace ID (`derive_trace_id`: 32-hex IDs map onto themselves, short/hex left-padded, non-hex sha256-hashed, never 0), and both `request_id` + `trace_id` are bound into structlog contextvars for the request's lifetime (unbound precisely after, no leakage). Root span carries `http.request.method`/`url.path`/`http.route`/`http.response.status_code`/`http.request_id`, renamed to the templated route post-routing; exceptions recorded + re-raised. `opentelemetry-sdk` + OTLP-HTTP exporter added; export env-driven (`OTEL_EXPORTER_OTLP_ENDPOINT`, unset = correlated but not exported). Shared post-routing route template extracted to `app/middleware/routing.py` (metrics + tracing use the same reconstruction). Admin app stays middleware-free (asserted). 10 new tests (derivation determinism, header echo/generate, trace-ID inheritance via a NonRecordingSpan parent, templated span name, exception status, contextvars lifecycle, raw-path fallback) — the SDK 1.44 API caught: `set_tracer_provider` refuses to override once set, so the middleware takes an injectable `tracer` for tests; smoke-verified live (log shows `request_id=<uuid> trace_id=<same-uuid>` on generated IDs). **206 tests green**.

- **2026-08-14** — /health and /metrics moved off the public app onto a dedicated internal admin ASGI app (`app/admin.py`) served by the same process on `ADMIN_HOST:ADMIN_PORT` (default `127.0.0.1:9091`; `API_HOST`/`API_PORT` for the public app) — the public app now 404s on both (asserted in unit + integration tests). `python -m app.main` runs both apps via `run()`; the runner uses a `_SignalNeutralServer` subclass that disables uvicorn's per-server `capture_signals` (two concurrent `serve()` calls would fight over SIGINT/SIGTERM — the second install overrides the first, stopping only one server and hanging shutdown) and owns the process's signals itself, setting both servers' `should_exit`. Admin app carries no middleware/docs/auth by design; same-process requirement documented (registry is process-local — a standalone admin container would scrape an empty registry). 5 new tests (route-split + signal-neutrality unit tests, public-404 integration test) + smoke-verified both servers live (public: /health /metrics 404, /api/v1 serves; admin: real HealthReport + real counters incl. the public app's 404/401 series). **196 tests green**.

- **2026-08-14** — Pull-forward: platform monitoring artifacts — `deploy/monitoring/` with 7 Prometheus alert rules (`alerts.yml`: critical Postgres/Redis down; 5xx error-rate spike; 429 flood via `vhk_rate_limit_rejections_total`; stale worker heartbeat; per-adapter backend down; a counter-only flap detector proving two down episodes with a clean gap via offset windows; p99 latency) + a Grafana dashboard (`platform.json`, 7 panels over a datasource variable: QPS by status, error-rate stat, latency percentiles, per-dependency health failures, rejections by limit, 429 rate, avg response size). Validated by `tests/unit/test_monitoring_config.py` (4 tests) — no promtool here, so the test pins rule structure and cross-checks every PromQL expr against the real metric names from `app/core/metrics.py` + the instrumentator (this caught the Summaries' `_sum`/`_count` series and a missing `for` on the flap rule). PyYAML + types-PyYAML added as dev deps for the config tests. `deploy/monitoring/README.md` documents wiring + threshold rationale.

- **2026-08-14** — Pull-forward: env-driven log rendering (`LOG_FORMAT=console|json`, same processor chain, JSON for staging/prod pipelines) + a per-request access log — every HTTP request emits one `request_completed` INFO line (method, post-routing templated path, status) from the request-observation middleware. 3 new unit tests pin the JSON line shape and the access-log fields deterministically.

- **2026-08-14** — Pull-forward: request-duration histograms + request/response size metrics via `prometheus-fastapi-instrumentator` (v8, `Instrumentator(should_group_status_codes=True)` + `metrics.default()`), registered on the shared default registry so the existing `/metrics` route renders them alongside the `vhk_*` counters; handler label is templated (status grouped to 2xx/4xx), size metrics are Summaries. The previous entry's "latency histograms remain Phase 7" note is now obsolete.

- **2026-08-14** — Pull-forward: structured logging (structlog, stdlib-integrated, `app/core/logging.py`) + rate-limit observability — every 429 emits one `rate_limit_exceeded` log line (limit, retry_after, method, path, tenant/key ids — never secrets) and increments `vhk_rate_limit_rejections_total{limit=...}`, asserted via a deterministic unit test (stub logger) plus the /metrics counter series.

- **2026-08-14** — Pull-forward: `GET /metrics` (Prometheus text format, `prometheus-client`) with two counter families — `vhk_requests_total` (method/route-path/status, labels reconstructed post-routing so dynamic segments collapse to the template, including 4xx/5xx and rate-limit 429s) and `vhk_health_checks_total` (per-check ok/down outcomes recorded by the health service). Latency histograms + instrumentator remain Phase 7.

- **2026-08-14** — Pull-forward: Redis-backed token-bucket rate limiting as ASGI middleware — route (default QPS + per-route overrides), tenant (`tenants.rate_limit_qps`), and API-key (`api_keys.rate_limit_qps`, column already existed from Phase 2) limits, all checked per request with most-restrictive-wins 429 + `Retry-After` and `details.limit` naming the limit (`route_qps`/`api_key_qps`/`tenant_qps` → `RATE_LIMIT_*` codes). Atomic Lua bucket script; tenant/key rates resolved via Redis config-key read-through (bounded 2s, negative-cacheable); fails open when Redis/Postgres is down. Migration 0004 (tenant column); `TenantCreateRequest.rate_limit_qps` threaded through. 6 new tests (2 unit + 4 integration incl. refill and fail-open).

- **2026-08-14** — Pull-forward: `GET /health` now runs the full dependency probe — Postgres (SELECT 1), Redis (PING), worker liveness (arq heartbeat freshness under `vhk:worker:heartbeat:*`, TTL-driven), and per-adapter `health_check()` via a v0.5 `AdapterRegistry` scaffold (`app/adapters/`, class-based lifecycle lands with Phase 3) — with the ok/degraded/down + 200/503 contract from the spec; the workers check is honest pre-worker (no fresh heartbeat → degraded). 8 new tests (unit down-contract + integration ok/degraded/stale-heartbeat/adapter-status/postgres-outage/redis-outage).

- **2026-08-14** — Phase 2 complete. Auth & RBAC: bcrypt hashing behind a swap-friendly wrapper (passlib dropped — unmaintained + incompatible with bcrypt ≥ 4.1; deviation recorded), HS256 JWT access tokens, opaque refresh tokens stored hashed with atomic rotation (replay reads as revoked), register/login/refresh/logout/me, tenant model wiring (register provisions tenant + owner; platform-admin bootstrap via `BOOTSTRAP_PLATFORM_ADMIN_EMAILS`), tenant-scoped API keys (hashed at rest, plaintext shown once, per-key role, admin/owner manage), role→permission matrix + `require_permission`/`require_platform_admin` dependencies, `Principal` derived only from credentials, audit rows for tenant/API-key writes, 3 new taxonomy codes (`AUTH_EMAIL_TAKEN`, `TENANT_ALREADY_EXISTS`, `API_KEY_NOT_FOUND`). 73 tests green (unit + real-Postgres integration + ASGI API). Note: pytest-asyncio now uses a session-scoped event loop (pooled engine across tests); email validated with pydantic `EmailStr` (email-validator added once PyPI was reachable). 9 commits.

- **2026-08-14** — Phase 1 complete. Scaffolded app/ structure; uv/ruff/mypy/pytest/pre-commit toolchain (uv 0.12.4 installed); pydantic-settings config + .env.example; FastAPI skeleton with CORS and /health; complete error taxonomy enum; SQLAlchemy 2.0 async control-plane models (tenants, users, api_keys, refresh_tokens, collections with opaque physical_name, collection_permissions, audit_log, jobs); Alembic initial migration with two-role provisioning (app/migrator) + all-role audit_log guard trigger, verified against a real Postgres via testcontainers (15 tests, all green). 7 commits.

- **2026-08-14** — Pre-build design session (brainstorming) complete, no code written yet. Resolved five architecture decisions, now **folded into this file**: (1) tenant isolation = per-backend native tenancy matrix (Weaviate/Qdrant native tenants, Milvus partitions, Chroma per-tenant collections) — see **Tenancy Matrix**; (2) hybrid search = client-supplied sparse vectors + normalized `alpha` — see hybrid note in API Surface; (3) async batches staged via object storage (MinIO/S3, JSONL), never through arq/Redis — see batch note in API Surface; (4) control-plane drift = observable non-goal (`backend_status` field, disjoint exists/missing/error); (5) audit immutability = app/migrator Postgres roles + all-role guard trigger, stated threat model. The full rationale and knock-on analysis lives in `docs/superpowers/specs/2026-08-14-vectorhub-platform-architecture-design.md` — read it for depth before Phase 1. Follow-on designs from the same session, also folded into this file: the cross-tenant isolation suite (Testing bullet — Layers 1–3 and phase gates) and the batch throughput analysis (batch note — chunk sizing, read-ahead pipeline, Chroma soak), each with its own design doc under `docs/superpowers/specs/`.

<!-- Example entry once you start:
- **2026-07-01** — Phase 1 complete. Scaffolded app/ structure, set up Postgres models for tenant/user/collection, Alembic initial migration, pydantic-settings config. 6 commits.
-->
