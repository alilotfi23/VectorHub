# Cross-Tenant Isolation Test Suite — Design

**Date:** 2026-08-14
**Status:** Approved (design session; implementation lands with the adapter phases)
**Base specs:** `CLAUDE.md` (Tenancy Matrix, Tenancy Matrix subsection) and `docs/superpowers/specs/2026-08-14-vectorhub-platform-architecture-design.md`

## Purpose

Tenant isolation is a **security boundary**, not a convenience feature: tenant A must not be able to read, modify, or delete tenant B's vectors or collections through the platform, on any backend, through any API path (sync vectors, async batch, hybrid, list, get, delete). This document designs the test suite that *proves* that guarantee — the acceptance gate every adapter phase must pass before it is considered complete.

The suite verifies the full isolation chain, end to end:

```
principal → tenant derivation → service-layer assertion → registry physical-name resolution
    → adapter tenant scoping → backend's native tenancy mechanism
```

## 1. Threat model & scope

**In scope — the platform's own failure modes:**
- Routing bugs: the service resolves a collection or physical target belonging to another tenant
- Adapter bugs: tenant scoping omitted, wrong tenant passed, or the backend call silently degrades to unscoped (e.g. the tenant param no-ops)
- Forgery: a client supplies `tenant_id` or another tenant's collection name in a request
- Existence leaks: list/get/error responses differing in a way that reveals another tenant's collections
- Batch/hybrid paths bypassing the scoping the sync path applies

**Out of scope (consistent with the audit-log threat model):** direct Postgres access, superuser/`postgres`, physical DB access, and attacks against the vector backend's own network/auth (the platform's API is the attack surface).

## 2. Design principles

1. **Indistinguishable test data (the linchpin).** Every isolation test seeds tenant A and tenant B with the **same vector IDs and identical vectors**, differing only in which tenant they were written as (payloads carry an `_tenant_probe` marker for visibility; count assertions cover the identical-payload case). Any unscoped read therefore returns *both* tenants' data — a leak cannot hide behind coincidentally-different data. The only way a test passes is if the backend (or the routing layer) genuinely separates the tenants.

2. **Fail-closed property.** An unscoped or mis-scoped operation must **raise or return empty** — never silently return another tenant's data. Asserted explicitly per backend. (Backends differ in whether an unscoped call errors or returns nothing; the assertion contract is "error or empty, never cross-tenant rows", which holds regardless.)

3. **Behavioral, not implementation.** Tests assert outcomes, not call shapes. An implementation may switch between mechanisms (e.g. the documented filter-based fallback mode) without the suite being rewritten — the guarantee must hold regardless of how scoping is implemented.

## 3. Layer 1 — Adapter isolation suite (real backends, testcontainers)

Location: `tests/integration/adapters/` — one suite per backend, sharing a **parametrized contract suite** plus per-backend **mechanism tests**.

### 3.1 Shared contract cases (identical on all four backends, via the adapter interface)

Fixture: creates the collection (per-backend tenancy enabled), provisions tenants A and B via `ensure_tenant`, seeds the data.

| Case | What it does | Asserts |
|------|--------------|---------|
| **C1 — Same-ID isolation** | Upsert `doc-1` under A and B (same vector, same metadata). Fetch as A; fetch as B. | A's fetch returns A's record (`_tenant_probe: "A"`); B's fetch returns B's record. Same ID must not collide. |
| **C2 — Query scoping, oversized top_k** | Seed 5 records per tenant; query with `top_k=10` as A, then as B. | Exactly 5 results per query, all with the caller's `_tenant_probe` marker. An unscoped query would return 10. |
| **C3 — Delete scoping** | Delete `doc-1` as B. | B's record gone; A's `doc-1` intact (verified by A's fetch). |
| **C4 — Fail-closed unscoped/mis-scoped** | Invoke query/fetch without tenant context where the interface allows it, or with an unknown/other tenant. | Raises or returns empty. Never returns cross-tenant rows. |
| **C5 — `ensure_tenant` idempotency** | Call `ensure_tenant` twice for the same tenant; call for an unprovisioned tenant. | Second call is a no-op (no error, no duplicate); unprovisioned tenant is created. |
| **C6 — Hybrid scoping** (Phase 4+, backends with sparse support) | Seed identical sparse+dense vectors under A and B; hybrid query as A. | Only A's results, per the hybrid contract. |

### 3.2 Per-backend mechanism tests (prove the native mechanism actually isolates)

These catch the failure class where a call *looks* correct but the backend never enforces it (e.g. the tenant parameter silently no-ops, or inserts don't route).

- **Qdrant:** collection created with `multi_tenancy_config`; tenants A/B created. Same-ID seed; `query_points` with `tenant=A` returns only A's points (storage-level enforcement, not a filter). Query **without** tenant on a multi-tenant collection → **error** (fail-closed). Upsert under a never-created tenant → error. Assert the physical collection name is the opaque `col_<uuid>` (never the platform name).
- **Weaviate:** tenant-enabled class (`multiTenancyConfig.enabled: true`). Same-ID seed under A and B with tenant scoping; query scoped by tenant → only that tenant's data; unscoped query on the tenant-enabled class → error or empty (fail-closed, asserted per the §2 contract). `tenants.create` idempotency.
- **Milvus:** partition-per-tenant. Inserts route by `partition_name`; search with `partition_names=[A]` → only A's. **Search without `partition_names` must return nothing** — this is the strongest proof: data went to named partitions (default partition is empty), so *both* insert and search are proven to route. Insert into an uncreated partition → error.
- **Chroma:** per-tenant physical collections. Assert the physical names for (A, `products`) and (B, `products`) are **distinct** `col_<uuid>` values; querying A's physical collection returns only A's data. There is no shared physical object, so the leak vector here is routing — the test proves distinct physical targets + correct resolution.

### 3.3 Suite shape

A shared pytest fixture/parametrize over backends for §3.1; separate test modules per backend for §3.2. Setup/teardown per backend creates and cleans the physical collection + tenants/partitions so suites are repeatable and never depend on prior state.

## 4. Layer 2 — Service-layer routing tests (recording stub adapter, no containers)

Location: `tests/integration/services/`. Runs in the fast CI job (no vector backend needed).

**The stub:** a `VectorDBAdapter` implementation that records every call into an in-memory log — `[(operation, physical_name, tenant_id, payload_snapshot)]` — and returns canned success. Real `CollectionService`/`VectorService`/`SearchService`/`JobService` instances run against it via `AdapterRegistry` (registering the stub under a test backend). This layer catches routing bugs in milliseconds, container-free, and pins the contract the real adapters must honor.

| Case | What it does | Asserts |
|------|--------------|---------|
| **R1 — Assertion fires first** | Vector/collection op on a collection whose Postgres `tenant_id` ≠ principal's tenant. | Typed exception (`COLLECTION_NOT_FOUND`/`AUTH_INSUFFICIENT_SCOPE`) raised **before any adapter call** — stub log is empty. |
| **R2 — Correct physical resolution** | Successful op via the stub. | Recorded `physical_name` equals the `col_<uuid>` resolved from the *principal's* registry row. |
| **R3 — Forged `tenant_id` rejected** | Request body supplies `tenant_id: <other>`. | Schema-level rejection: 422 via Pydantic `extra="forbid"` on the record envelope (the schema deliberately has no `tenant_id` field). The stub never records a forged value — the principal's tenant is what reaches the adapter. |
| **R4 — Batch scoping** | Enqueue a batch against another tenant's collection name; enqueue against own collection. | Foreign name → `COLLECTION_NOT_FOUND` at enqueue; own collection → staged object key is `{principal_tenant_id}/{job_id}.jsonl` (stub/JobService records the key). |

## 5. Layer 3 — API/e2e isolation suite (real platform, real backend)

Location: `tests/e2e/test_tenant_isolation.py` — full HTTP surface, parametrized per backend. Two real principals (register/login or API keys) drive every case.

| Case | What it does | Asserts |
|------|--------------|---------|
| **E1 — Collection-name collision** | A and B both create `products`. B runs GET/query/vector ops on `products`. | B sees only B's data; A's data invisible on every path. |
| **E2 — Cross-tenant collection ops** | B GET/DELETE/PATCH on A's *distinctly-named* collection. | `404 COLLECTION_NOT_FOUND`; DELETE must not touch A's backend data (verify A's data still queryable). |
| **E3 — Forged `tenant_id`** | Body carries `tenant_id: <other>` on create/upsert. | 422 (schema rejection); data lands under the principal's tenant. |
| **E4 — Vector-ID collision via API** | Both tenants upsert `doc-1`; B fetches and queries. | B gets B's payload; B's query returns only B's results. |
| **E5 — Batch path** | B's async batch uses the same IDs as A's seed. | B's collection updated; A's data untouched (verified via A's subsequent fetch). Job reports success counts. |
| **E6 — Hybrid path** (backends with sparse support) | B's hybrid query. | Only B's results (see hybrid contract). |
| **E7 — Tenant-scoped listing** | B lists collections. | Never contains A's collections — even with identical names, each tenant sees their own. |
| **E8 — Negative control / existence oracle** | With B having no collections at all, B probes: GET collection, query, fetch vector. | Same status + error code as a genuinely nonexistent resource (e.g. random UUID name). Responses must not differ in a way that reveals A's collections. (Response *shapes* only — timing side channels are out of scope, §8.) |

## 6. Assertion & fixture conventions

- **Marker field:** every seeded payload includes `_tenant_probe: "A"|"B"`; result assertions check markers (visibility) and exact counts (identical-data proof).
- **Data hygiene:** the identical IDs/vectors used for the count proofs are regenerated per test; no test depends on another's state.
- **Error assertions:** use the platform's `{error_code, message, details}` shape; cross-tenant attempts must map to existing taxonomy codes (`COLLECTION_NOT_FOUND`, `AUTH_INSUFFICIENT_SCOPE`, `VALIDATION_*`) — no new codes expected from this suite.
- **Two principals minimum** per e2e run; Phase 2 adds API-key principals to the same suite (per-tenant keys).

## 7. Phase gates & CI placement

A backend's adapter phase is **not complete until its backend passes Layers 1 and 3** (Layer 2 is backend-independent and lands with Phase 3):

- **Phase 3 (Chroma):** Layers 1–3 all exist and pass on Chroma, plus the **100k-vector batch soak** from the throughput analysis (design session, 2026-08-14; conclusions to be folded into the batch design) — validates the ~10–30 s budget and Chroma's degradation curve.
- **Phase 4 (Qdrant, Weaviate):** join all layers; C6/E6 (hybrid scoping) activate for sparse-capable backends.
- **Phase 5 (Milvus):** joins all layers — partition routing cases incl. the no-`partition_names` fail-closed proof.
- **CI:** adapter isolation suites run inside the existing per-backend testcontainers jobs; **Milvus in its separate job** (existing policy — etcd+MinIO sidecars); Layer 2 runs in the fast service-test job (no containers).

## 8. Deferred (noted, not built)

- **Concurrency cross-talk:** parallel upserts/queries from A and B racing on identical IDs (worth a stress test once Layers 1–3 pass on all backends).
- **Response-timing side channels:** E8 covers response shapes, not timing; timing-based existence oracles are out of scope.
- **Fuzz-style sequences:** randomized cross-tenant request sequences; revisit if the deterministic suite ever passes while a production incident says otherwise.

## 9. Relationship to the rest of the test plan

- Unit tests (per service) cover the tenant-assertion logic in isolation; Layer 2 exercises it through real services.
- The adapter suites here are *isolation*-focused; functional correctness of each adapter (create/query/hybrid/index behavior) lives in the same directories but is a separate concern.
- The audit-log tests (immutability guard trigger, role grants) are separate; this suite never asserts audit behavior.
