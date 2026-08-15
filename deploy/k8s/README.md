# Kubernetes deployment

Kustomize-based manifests for the platform. Apply an overlay with
`kubectl apply -k deploy/k8s/overlays/<env>`.

## Layout

```
deploy/k8s/
  base/        # control plane: app + worker + Postgres + Redis + MinIO
  backends/    # self-hosted vector DBs: Qdrant, Weaviate, Chroma StatefulSets
  overlays/
    dev/       # base + backends, 1 replica, dev secrets
    staging/   # base + backends, 2 replicas, JSON logs, real secrets required
    prod/      # base only — vector DBs are managed (cloud) endpoints
```

## How it works

- **One pod = both apps.** `app` runs `python -m app.main`, which serves the
  public API on 8000 *and* the internal admin app (/health, /metrics) on
  9091 in one process (the Prometheus registry is process-local).
- **The admin port is a network boundary, not an auth boundary.** The
  Service and Ingress expose only 8000. Readiness/liveness probes hit
  `/health` on the pod's 9091 directly — no NodePort, no LoadBalancer, no
  Ingress for it. `ADMIN_HOST=0.0.0.0` in the base ConfigMap so pod-network
  probes reach it.
- **Migrations run as an initContainer** on both `app` and `worker` pods
  (`deploy/migrate.sh`: `alembic upgrade head` as the migrator role, then
  the app/migrator role-password bootstrap). It is idempotent, so a fresh DB
  migrates on first rollout and later schema changes apply on the next pod
  (re)creation — no separate migration Job to sequence.
- **Batch jobs** are staged on the MinIO PVC (`vectorhub-batches`, created
  on demand by the enqueue path) and drained by the `worker` Deployment
  (`python -m app.workers`), whose liveness is the `vhk:worker:heartbeat`
  key observed by /health.
- **HPA** scales `app` on CPU (min 1 / max 5 in base; prod overlay: 3 / 10).
  Vector query/ingest is CPU-bound; the batch path runs on the worker, so
  API replicas can stay small.

## Secrets

The base Secret contains **dev placeholders**. Every overlay applies a
`secret-patch.yaml`; dev's values are fine for a local cluster, but
staging/prod MUST replace every `CHANGE_ME` — ideally by dropping the patch
and wiring `external-secrets`/`sealed-secrets`/Vault to a Secret with the
same `vectorhub-secrets` name. `JWT_SECRET` is a hard requirement: the app
refuses to boot with the dev default in `ENVIRONMENT=staging/prod`.

## Self-hosted vs managed

- **dev / staging**: self-hosted vector DBs via the `backends/` layer
  (Qdrant, Weaviate, Chroma StatefulSets with PVCs). Milvus is deliberately
  absent — its standalone deployment needs the etcd + MinIO sidecar trio
  in-cluster; v1 targets managed Milvus (e.g. Zilliz Cloud) via `MILVUS_URL`.
- **prod**: no backends layer. `configmap-patch.yaml` points `QDRANT_URL`,
  `WEAVIATE_URL`, `MILVUS_URL`, `CHROMA_URL` at managed endpoints and
  `CORS_ALLOWED_ORIGINS` at an explicit allow-list (never `*`).
- Postgres/Redis/MinIO can likewise be swapped for managed services: drop
  the resource from the overlay and patch the URL in the ConfigMap
  (`REDIS_URL`, or `DATABASE_URL` in the Deployment env — note the app role
  password is injected via `$(POSTGRES_APP_PASSWORD)` interpolation, so keep
  the `secretKeyRef` even when Postgres is managed).

## Helm chart

The same manifests are packaged as a parameterized Helm chart at
`deploy/helm/vectorhub` (image tags, credentials, replica counts, and the
self-hosted vs managed toggles as values):

```bash
helm install vectorhub ../helm/vectorhub -n vectorhub --create-namespace \
  --set image.tag=main --set jwt.secret="$(openssl rand -hex 32)"
```

Keep behavioral changes (probes, securityContext, the admin-port boundary)
in sync across both — the CI `helm-validate` job catches template breakage,
not behavioral drift. See the chart's README for the values reference.

## Verify

```bash
kubectl kustomize deploy/k8s/overlays/dev      # render without applying
kubectl apply -k deploy/k8s/overlays/dev
kubectl -n vectorhub get pods,svc,ingress
kubectl -n vectorhub port-forward svc/app 8000:8000   # then hit /api/v1/...
```

The image tag (`ghcr.io/your-org/vectorhub-platform:<env>`) is a placeholder
— the CI `docker-image` job pushes `ghcr.io/${{ github.repository }}` on
main; update the `images:` blocks to your registry.
