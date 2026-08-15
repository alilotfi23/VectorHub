# Contributing to VectorHub

Thanks for contributing! This guide covers how to work in this repo: setup,
the architecture rules every change must follow, the test/lint gates, and how
to commit. It assumes you've read `README.md` (architecture, tenancy matrix,
hybrid contract, batch data path, error taxonomy) and `CLAUDE.md` (the full
build spec, tenancy matrix, and progress log).

## Table of contents

- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Architecture rules (read before writing code)](#architecture-rules-read-before-writing-code)
- [Testing](#testing)
- [Lint & type gates](#lint--type-gates)
- [CI and how to reproduce it locally](#ci-and-how-to-reproduce-it-locally)
- [Deploy smoke](#deploy-smoke)
- [Commit conventions](#commit-conventions)
- [Docs](#docs)

## Prerequisites

- **Python 3.13** (the CI `PYTHON_VERSION`)
- **uv** — the only dependency manager; `uv.lock` is committed. Install per
  the [uv docs](https://docs.astral.sh/uv/); the bootstrap is `uv sync`.
- **Docker** — the integration/e2e suites boot real Postgres, Redis, MinIO,
  Chroma, Qdrant, and Weaviate via testcontainers (the Milvus trio runs in a
  separate CI job). A local full-stack run needs the compose stack.
- **`python3` on PATH** — used by `deploy/smoke.sh` (stdlib-only journey).

## Setup

```bash
git clone <repo> && cd VectorHub
uv sync                      # install deps (uv required; uv.lock committed)
uv run pre-commit install    # ruff check / ruff format / mypy on every commit
```

No `.env` is needed for the test suite (testcontainers manage their own
infra). If you're running the app locally against the compose stack, see
`README.md` → Quick start and `.env.example` for the config surface.
**Never commit `.env`, credentials, or API keys** — only `.env.example` with
placeholder values.

## Architecture rules (read before writing code)

These are enforced in review. The platform is a **unified REST API over four
vector backends** (Weaviate, Qdrant, Milvus, Chroma) with enterprise
auth/RBAC, multi-tenancy, audit logging, rate limiting, and async batch
ingestion. The abstraction stays honest via a live capability matrix
(`GET /api/v1/capabilities`).

1. **Adapter / strategy pattern, pluggable registry.** Every backend is a
   `VectorDBAdapter` implementation in `app/adapters/<name>_adapter.py`,
   registered in `app/adapters/__init__.py` via `AdapterRegistry.register()`
   — never a hardcoded if/elif in the routing layer. Adding a fifth backend
   (e.g. Pinecone) is "write an adapter + register it", nothing else.
   Backend-specific features go through the `extras: dict` passthrough, never
   by breaking the common interface. Per-backend differences must be visible
   in the `CapabilityMatrix` — don't hide them.

2. **Service layer is mandatory.** Routes (in `app/api/v1/`) do request
   validation (Pydantic) and response shaping only. All orchestration —
   adapter calls via the registry, Postgres, audit, quotas — lives in a
   service class in `app/services/`. Routes never call adapters directly.

3. **Tenant scoping is a security boundary.** `tenant_id` is always derived
   from the authenticated principal, never from the request body. Request
   schemas use the `StrictRequest` base (`extra="forbid"`) so forged
   `tenant_id`/`owner_id`/role-escalation fields are rejected at the schema
   (422). Isolation is enforced by the vector backend per the Tenancy Matrix
   (Weaviate native tenants, Qdrant `is_tenant` index, Milvus
   partition-per-tenant, Chroma per-tenant collections) — never by filter
   logic alone — and the service layer additionally asserts
   `collection.tenant_id == principal.tenant_id` before every adapter call.

4. **Consistent error taxonomy.** Every error maps to an `ErrorCode` enum in
   `app/core/exceptions.py`, namespaced (`AUTH_*`, `COLLECTION_*`,
   `VECTOR_*`, `JOB_*`, `RATE_LIMIT_*`, `VALIDATION_*`, …), and the response
   is the standard `{error_code, message, details}` shape. Never raise raw
   strings. If no existing code fits, add the new code to the taxonomy in
   **the same commit**.

5. **No silent no-ops on capability gaps.** If a backend can't do something
   (Weaviate metadata filtering, Chroma hybrid search, a
   `PATCH /config` param that needs a rebuild), return the honest error —
   `400 VALIDATION_UNSUPPORTED_OPERATION` with `details.capability`, or `409
   REQUIRES_REINDEX` with `details.next_step` — never an empty result or a
   generic failure.

6. **Vectors-in, vectors-out.** The platform never calls embedding
   providers. New work on the vector path assumes pre-computed floats from
   the caller. A text-in embedding layer is a future phase, not something to
   fold into a vector-route change.

7. **Delete is a hard delete.** Collection/vector deletes must remove data
   from the backend as part of the same operation — no soft-delete-only
   cleanup jobs.

8. **`backend` is immutable** for the life of a collection. No new endpoint
   or code path may change a collection's backend; cross-backend migration is
   out of scope for v1.

## Testing

Three layers, all under `tests/`:

- **`tests/unit/`** — no containers. Pure logic: schemas, pagination,
  capability-driven examples, RBAC resolution, etc.
- **`tests/integration/`** — testcontainers: real Postgres/Redis for
  services, and one suite per adapter (`tests/integration/adapters/`) against
  a real backend. The **cross-tenant isolation suite** (adapter mechanisms +
  service-layer routing + e2e) is the security-boundary acceptance gate —
  every adapter must pass it.
- **`tests/e2e/`** — full API journeys over the shared fixtures.

### Rules that matter

- **One world, one FixtureDef.** All shared infra fixtures (Postgres engine,
  session factory, middleware patch cycle, vector-DB registrations) live in
  the **top-level `tests/conftest.py` only**. Never re-export another layer's
  conftest fixtures (`from tests.integration.conftest import session_factory`
  is the anti-pattern): each re-export registers a *second* session-scoped
  instance — its own Postgres container and its own registry/middleware
  state — and randomized-order runs interleave the two worlds mid-session.
  This exact bug broke the suite once; don't reintroduce it.
- **The suite runs in randomized order in CI** (`--random-order
  --random-order-bucket=global`). Write order-independent tests: no reliance
  on another test's side effects, no registry re-registration in tests, no
  fixture state assumptions across tests. A test that restores a displaced
  adapter instance (e.g. the OpenAPI-examples tests) must restore it via
  `tests/support.py::registry_preserved`, not by re-registering from settings
  defaults.
- **Markers.** `@pytest.mark.soak` for the 100k-vector ingest soak (deselected
  from the fast gate). `@pytest.mark.milvus` for anything needing the Milvus
  trio (etcd + MinIO sidecars) — it runs in its own CI job so a Milvus
  startup timeout can't block the rest of the pipeline. The nightly job
  includes both.
- **Float32.** Backends store vectors as float32; assert round-trips with a
  tolerance, never bit-exact equality.

### Run them

```bash
uv run pytest -q                                              # full suite (all containers, incl. milvus + soak)
uv run pytest -q -m "not soak and not milvus"                 # fast gate (Docker needed)
uv run pytest -q --random-order --random-order-bucket=global -m "not soak and not milvus"   # the CI shape
uv run pytest -q tests/unit                                   # no Docker needed
uv run pytest -q -m "milvus"                                  # Milvus trio only
uv run pytest -q -m "soak"                                    # 100k ingest soak
```

A failing randomized-order run reproduces with the printed seed:
`uv run pytest -q --random-order-seed=<seed>`.

## Lint & type gates

All three run on **every commit** via pre-commit (and in CI):

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
```

Keep them green before pushing; `ruff format` + `ruff check --fix` are your
friends. `deploy/` is excluded from mypy (the smoke journey is stdlib-only
and not type-checkable in this project's config) — don't re-include it.

## CI and how to reproduce it locally

`.github/workflows/ci.yml` runs, on push/PR:

| Job | What | Reproduce locally |
|---|---|---|
| `lint-type` | ruff check/format, mypy | the three commands above |
| `tests-random-order` | full suite, randomized, `-m "not soak and not milvus"` | the fast-gate command above |
| `tests-milvus` | milvus-marked tests, randomized | `uv run pytest -q --random-order -m "milvus"` |
| `helm-validate` | `helm lint` + render in default/managed modes | `helm lint deploy/helm/vectorhub` + `helm template` variants |
| `deploy-smoke` | boot full compose, `/health` ok, public 404, **all-four-backends API journey** | `bash deploy/smoke.sh` |
| `release` | **Semantic Release** on main: Conventional Commits → SemVer stamp in `pyproject.toml` + `CHANGELOG.md` + `vX.Y.Z` tag + GitHub release (no-op on docs/chore-only pushes) | `uvx python-semantic-release@10.6.1 -v --noop version` (dry run) |
| `docker-image` | push `ghcr.io` image on main, gated on lint + smoke; also tagged `vX.Y.Z` when a release was made | n/a |
| `tests-nightly-seed` | full suite incl. milvus + soak, date-derived seed, cron-only | `uv run pytest -q --random-order --random-order-seed=$(date +%Y%m%d)` |

If CI fails in a job you can't run locally (e.g. the Milvus trio is heavy),
say so in the PR rather than guessing — and never bypass a gate to land code.

## Deploy smoke

`deploy/smoke.sh` is the deployment gate: it builds the image, boots the full
compose stack (all four backends + the Milvus trio + MinIO + Postgres + Redis
+ app + worker), waits for the internal admin `/health` to report `status:
ok`, asserts the public app 404s on `/health`, then runs the real-user API
journey (`deploy/smoke/journey.py`) across all four backends — including the
async batch path through the real arq worker and MinIO. Run it before
touching anything deploy-related:

```bash
bash deploy/smoke.sh   # full self-hosted stack (destroys the compose volumes when done)
```

If you change the journey, keep it **stdlib-only** (it runs with the runner
host's `python3`, no uv/deps) and re-run `bash deploy/smoke.sh` to verify.

## Commit conventions

- **Conventional Commits**: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`,
  `chore:`, `ci:`, with a scope where useful
  (e.g. `feat(adapters): implement QdrantAdapter.query with metadata
  filtering`). Commit types are **semantic-release inputs**, not just style:
  `feat:` bumps minor, `fix:`/`perf:` bump patch, and a `BREAKING CHANGE:`
  footer (or `!` in the subject) bumps major. A release (tag + GitHub
  release + image tag) fires only when a commit since the last tag warrants
  one — `docs:`/`chore:`/`ci:` pushes are release-free by design.
- **Commit after every meaningful change** — a working unit (an adapter
  method, a route, a passing test suite, a config file). Don't batch
  unrelated changes into one commit; don't leave a giant "wip" commit.
- **Work on `main` by default** with frequent small commits (this project is
  a solo/sequential build; feature branches only if asked for).
- **Verify before committing**: run the relevant tests and the pre-commit
  gates first; report results alongside the commit. Never commit code that
  leaves the build broken at a natural stopping point — say so instead.
- **No secrets, ever**: `.env`, credentials, API keys must not appear in the
  tree or in commits. Only `.env.example` with placeholders.

## Docs

- **`CLAUDE.md`** holds the build spec, the tenancy matrix, the error-code
  taxonomy, and the **Progress Log**. At the end of a phase (or a
  significant chunk of work), tick the phase checkbox and add a progress-log
  entry (newest on top) describing what landed, committed as `docs: update
  progress log`.
- **`README.md`** is the user-facing doc. Keep the tenancy matrix, hybrid
  contract, batch data path, error taxonomy, and deployment sections in sync
  when you change behavior — the capability matrix endpoint and the README
  must not drift.
- The OpenAPI examples are generated from the live capability matrix
  (`app/schemas/examples.py`) — don't hand-maintain example bodies in
  schemas; change the capability entry instead.
