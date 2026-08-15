"""Locust load test — Qdrant adapter path through the public API.

Exercises the real user flow end-to-end: register (creates the tenant +
owner), create a Qdrant collection, then a mixed upsert/query loop. Each
Locust user gets its own tenant, collection, and vectors, so load scales
linearly without cross-user interference; the queries hit real vectors.

Run against a booted stack (deploy/smoke.sh proves it healthy first):

    # interactive (http://localhost:8089):
    uvx locust -f deploy/loadtest/locustfile.py -H http://localhost:8000
    # headless, 10 users ramping 2/s for 60s:
    uvx locust -f deploy/loadtest/locustfile.py -H http://localhost:8000 \
        --headless -u 10 -r 2 -t 60s
    # or via the official image:
    #   docker run --rm -p 8089:8089 -v $PWD/deploy/loadtest:/mnt/locust \
    #       locustio/locust -f /mnt/locust/locustfile.py -H host.docker.internal:8000

Notes:
- The sync upsert path caps at 100 vectors/request — BATCH_SIZE stays under.
- VECTOR_DIMENSION must match the collection's creation dimension; 64 is
  cheap and realistic (the batch soak uses the same shape).
- Registration rate-limits apply (default 100 qps/route) — scale users
  below that or tune RATE_LIMIT_ROUTE_QPS for the test.
"""

import random
import uuid

from locust import HttpUser, between, task

DIMENSION = 64
BATCH_SIZE = 10
TOP_K = 10


def _vector() -> list[float]:
    return [random.random() for _ in range(DIMENSION)]


class QdrantVectorUser(HttpUser):
    """One tenant, one Qdrant collection, upsert + query traffic."""

    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.email = f"load-{suffix}@example.com"
        self.collection = f"load-{suffix}"
        self.token = self._register()
        self._create_collection()

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}"}

    def _register(self) -> str:
        with self.client.post(
            "/api/v1/auth/register",
            json={
                "email": self.email,
                "password": "loadtest-password",
                "tenant_name": f"load-tenant-{uuid.uuid4().hex[:6]}",
            },
            name="auth/register",
            catch_response=True,
        ) as resp:
            if resp.status_code != 201:
                resp.failure(f"register: {resp.status_code} {resp.text[:200]}")
                raise RuntimeError("registration failed")
        return resp.json()["access_token"]

    def _create_collection(self) -> None:
        with self.client.post(
            "/api/v1/collections",
            json={
                "name": self.collection,
                "backend": "qdrant",
                "dimension": DIMENSION,
                "distance_metric": "cosine",
            },
            headers=self._headers(),
            name="collections/create",
            catch_response=True,
        ) as resp:
            if resp.status_code != 201:
                resp.failure(f"create collection: {resp.status_code} {resp.text[:200]}")
                raise RuntimeError("collection creation failed")

    @task(3)
    def query(self) -> None:
        self.client.post(
            f"/api/v1/collections/{self.collection}/query",
            json={"vector": _vector(), "top_k": TOP_K},
            headers=self._headers(),
            name="query",
        )

    @task(1)
    def upsert(self) -> None:
        vectors = [
            {
                "id": f"{self.collection}-{i}-{uuid.uuid4().hex[:6]}",
                "vector": _vector(),
                "metadata": {"loadtest": True},
            }
            for i in range(BATCH_SIZE)
        ]
        self.client.post(
            f"/api/v1/collections/{self.collection}/vectors",
            json={"vectors": vectors},
            headers=self._headers(),
            name="vectors/upsert",
        )
