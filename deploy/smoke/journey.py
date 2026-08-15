#!/usr/bin/env python3
"""Live-stack real-user API journey — the deploy gate's second half.

Runs the full vector-data journey against a *booted* compose stack (smoke.sh
starts it after the internal /health reports ok) across all four backends —
qdrant, weaviate, milvus, chroma — proving the unified API is
backend-agnostic and that the real production path works end to end: the
control plane (Postgres/Redis auth), the arq worker + MinIO staging for
async batch ingest, and every adapter behind one consistent surface.

Backend capability gates are asserted per the CapabilityMatrix (GET
/capabilities is the canonical introspection point clients should check):
  * weaviate hybrid = text+vector; metadata filtering unsupported -> 400
    VALIDATION_UNSUPPORTED_OPERATION (capability: metadata_filtering)
  * qdrant/milvus hybrid = sparse+vector
  * chroma hybrid unsupported -> 400 (capability: hybrid_search)

Fetched vectors are compared with float32 tolerance: the backends store
float32 natively, so 0.2 round-trips as 0.20000000298023224 — inherent
backend behavior, not a bug.

stdlib only — runs with `python3` on the runner host; no uv needed.

Usage:  python3 deploy/smoke/journey.py [BASE_URL]   (default http://127.0.0.1:8000)
Exits 1 on any failed step.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = (
    sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SMOKE_BASE", "http://127.0.0.1:8000")
).rstrip("/")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, ok, extra))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {extra}" if extra else ""))


def call(method, path, body=None, token=None, raw=None, ctype="application/json"):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("accept", "application/json")
    if ctype:
        req.add_header("content-type", ctype)
    if token:
        req.add_header("authorization", f"Bearer {token}")
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, data) as r:
            payload = r.read()
            return r.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except Exception:
            return e.code, payload


def vec(n: float) -> list[float]:
    return [round(float(n) * 0.1, 3), 0.2, 0.3, 0.4]


def close(a: list[float], b: list[float], tol: float = 1e-3) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b, strict=False))


SPARSE = {"indices": [1, 2], "values": [0.5, 0.5]}

suffix = uuid.uuid4().hex[:6]

print(f"=== journey against {BASE} ===")

print("=== 0. REGISTER Alice (owner of her own tenant) ===")
s, r = call(
    "POST",
    "/api/v1/auth/register",
    {
        "email": f"alice-{suffix}@smoke.dev",
        "password": "password-123",
        "tenant_name": f"Smoke {suffix}",
    },
)
check("register -> 201", s == 201, f"status={s}")
if s != 201:
    print(json.dumps(r)[:500])
    sys.exit(1)
alice = r["access_token"]

s, me = call("GET", "/api/v1/auth/me", token=alice)
check(
    "me -> owner of own tenant",
    s == 200 and me["role"] == "owner",
    f"status={s} role={me.get('role')}",
)


def journey(backend: str) -> None:
    coll = f"docs-{backend}"
    print(f"\n===== BACKEND: {backend} (collection '{coll}') =====")

    s, c = call(
        "POST",
        "/api/v1/collections",
        {"name": coll, "backend": backend, "dimension": 4, "distance_metric": "cosine"},
        token=alice,
    )
    check(
        f"create on {backend} -> 201, routed",
        s == 201 and c.get("backend") == backend and c.get("backend_status") == "exists",
        f"status={s} backend={c.get('backend')} status={c.get('backend_status')}",
    )

    records = [
        {
            "id": f"doc-{i}",
            "vector": vec(i),
            "metadata": {"tag": "b" if i % 3 == 0 else "a", "n": i},
        }
        for i in range(10)
    ]
    s, r = call("POST", f"/api/v1/collections/{coll}/vectors", {"vectors": records}, token=alice)
    check(
        f"upsert 10 on {backend} -> 200",
        s == 200 and r.get("upserted") == 10,
        f"status={s} upserted={r.get('upserted')}",
    )

    s, v = call("GET", f"/api/v1/collections/{coll}/vectors/doc-5", token=alice)
    check(
        f"fetch doc-5 on {backend} -> round-trip (float32 tolerance)",
        s == 200 and close(v.get("vector", []), vec(5)) and v["metadata"]["n"] == 5,
        f"status={s} vector={v.get('vector')}",
    )

    s, q = call(
        "POST", f"/api/v1/collections/{coll}/query", {"vector": vec(9), "top_k": 5}, token=alice
    )
    hits = q.get("results", [])
    check(
        f"query on {backend} -> 5 hits, best first",
        s == 200 and len(hits) == 5 and hits[0]["id"] == "doc-9",
        f"status={s} top1={hits[0]['id'] if hits else None}",
    )

    s, q = call(
        "POST",
        f"/api/v1/collections/{coll}/query",
        {"vector": vec(9), "top_k": 10, "filters": {"tag": "b"}},
        token=alice,
    )
    if backend == "weaviate":
        check(
            "filtered query on weaviate -> 400 (filtering unsupported per matrix)",
            s == 400
            and q.get("error_code") == "VALIDATION_UNSUPPORTED_OPERATION"
            and q.get("details", {}).get("capability") == "metadata_filtering",
            f"status={s} cap={q.get('details', {}).get('capability')}",
        )
    else:
        hits = q.get("results", [])
        check(
            f"filtered query on {backend} -> only tag=b",
            s == 200 and len(hits) == 4 and all(h["metadata"]["tag"] == "b" for h in hits),
            f"status={s} {len(hits)} hits",
        )

    if backend == "weaviate":
        s, q = call(
            "POST",
            f"/api/v1/collections/{coll}/hybrid-query",
            {"vector": vec(9), "query_text": "doc", "top_k": 5},
            token=alice,
        )
        check(
            "hybrid (text+vector) on weaviate -> 200 with hits",
            s == 200 and len(q.get("results", [])) > 0,
            f"status={s} {len(q.get('results', []))} hits",
        )
    elif backend == "chroma":
        s, q = call(
            "POST",
            f"/api/v1/collections/{coll}/hybrid-query",
            {"vector": vec(9), "sparse_vector": SPARSE, "top_k": 5},
            token=alice,
        )
        check(
            "hybrid on chroma -> 400 (unsupported per matrix)",
            s == 400
            and q.get("error_code") == "VALIDATION_UNSUPPORTED_OPERATION"
            and q.get("details", {}).get("capability") == "hybrid_search",
            f"status={s} cap={q.get('details', {}).get('capability')}",
        )
    else:  # qdrant / milvus
        s, q = call(
            "POST",
            f"/api/v1/collections/{coll}/hybrid-query",
            {"vector": vec(9), "sparse_vector": SPARSE, "top_k": 5},
            token=alice,
        )
        check(
            f"hybrid (sparse+vector) on {backend} -> 200 with hits",
            s == 200 and len(q.get("results", [])) > 0,
            f"status={s} {len(q.get('results', []))} hits",
        )

    # Async batch through the real production path: NDJSON enqueue -> MinIO
    # staging -> arq worker -> chunked adapter upsert -> results object.
    lines = "".join(
        json.dumps({"id": f"batch-{i}", "vector": vec(i), "metadata": {"tag": "batch"}}) + "\n"
        for i in range(3)
    )
    s, r = call(
        "POST",
        f"/api/v1/collections/{coll}/vectors/batch",
        raw=lines.encode(),
        ctype="application/x-ndjson",
        token=alice,
    )
    job_id = r.get("job_id")
    check(
        f"enqueue batch on {backend} -> 202 with job_id",
        s == 202 and job_id,
        f"status={s} job={job_id}",
    )
    final = None
    for _ in range(45):
        s, j = call("GET", f"/api/v1/jobs/{job_id}", token=alice)
        if j.get("status") in ("succeeded", "failed"):
            final = j
            break
        time.sleep(1)
    check(
        f"batch job on {backend} -> succeeded 3/3",
        final
        and final.get("status") == "succeeded"
        and final.get("ok") == 3
        and final.get("total") == 3,
        f"status={final.get('status') if final else 'timeout'}"
        f" ok={final.get('ok')}/{final.get('total')}",
    )

    s, _ = call("DELETE", f"/api/v1/collections/{coll}/vectors/batch-1", token=alice)
    s, r = call("GET", f"/api/v1/collections/{coll}/vectors/batch-1", token=alice)
    check(
        f"delete then fetch on {backend} -> VECTOR_NOT_FOUND",
        s == 404 and r.get("error_code") == "VECTOR_NOT_FOUND",
        f"status={s} {r.get('error_code')}",
    )


for backend in ("qdrant", "weaviate", "milvus", "chroma"):
    journey(backend)

print("\n=== Cross-backend: one token sees all four collections ===")
s, r = call("GET", "/api/v1/collections", token=alice)
names = {c["name"] for c in r.get("items", [])}
check(
    "list shows all 4 backend collections under one tenant",
    s == 200 and {"docs-qdrant", "docs-weaviate", "docs-milvus", "docs-chroma"}.issubset(names),
    f"status={s} {sorted(names)}",
)

s, r = call(
    "POST", "/api/v1/auth/login", {"email": f"alice-{suffix}@smoke.dev", "password": "password-123"}
)
check("login -> 200 with tokens", s == 200 and r.get("access_token"), f"status={s}")

failed = [n for n, ok, _ in results if not ok]
print(f"\n=== {len(results) - len(failed)}/{len(results)} steps passed ===")
sys.exit(1 if failed else 0)
