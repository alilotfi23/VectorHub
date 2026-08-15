# vectorhub Helm chart

Parameterized packaging of the kustomize manifests (`deploy/k8s`). Same
resources, same names — image tags, credentials, replica counts, resource
requests, and the self-hosted vs managed toggles become values.

## Install

```bash
helm dependency update   # none today; no-op
helm install vectorhub ./deploy/helm/vectorhub -n vectorhub --create-namespace \
  --set image.tag=main \
  --set jwt.secret="$(openssl rand -hex 32)"
```

Dev defaults self-host everything (Postgres, Redis, MinIO, Qdrant, Weaviate,
Chroma) with placeholder secrets — fine for a local cluster, exactly like the
kustomize dev overlay. For staging/prod, override credentials (or swap the
Secret for external-secrets with the same `vectorhub-secrets` name/keys) and
`environment: staging|prod` (the app refuses to boot with the dev JWT secret
there).

## Modes

| Mode | Values | What runs |
| --- | --- | --- |
| **dev / staging (self-hosted)** | defaults (`backends.enabled: true`, `postgres/redis/minio.enabled: true`) | everything in-cluster; the `migrate` initContainer runs Alembic on every pod start (idempotent) |
| **prod (managed vector DBs)** | `backends.enabled: false` + `backends.*Url` → cloud endpoints, `corsAllowedOrigins` → explicit allow-list | control plane self-hosted, vector DBs managed |
| **managed control plane** | `postgres.externalUrl` (+ `postgres.migratorUrl` to keep pod-run migrations), `redis.externalUrl`, `minio.endpoint` + key overrides → S3 | StatefulSets/Deployments skipped, URLs injected |

Milvus is never self-hosted by this chart (its standalone deployment needs the
etcd + MinIO sidecar trio in-cluster — out of v1 scope, same as the kustomize
overlays); point `backends.milvusUrl` at managed Milvus (e.g. Zilliz Cloud).

## Key values

| Value | Default | Meaning |
| --- | --- | --- |
| `image.repository` / `image.tag` | `ghcr.io/your-org/vectorhub-platform` / `latest` | the image CI pushes on main |
| `replicaCount.app` / `replicaCount.worker` | 1 / 1 | replicas (app also scales via HPA) |
| `hpa.minReplicas` / `maxReplicas` / `cpuUtilization` | 1 / 5 / 70 | CPU HPA on the app Deployment |
| `jwt.secret` | dev placeholder | **must** change for staging/prod |
| `postgres.*` | self-hosted | `externalUrl`/`migratorUrl` switch to managed |
| `redis.externalUrl` | "" | managed Redis URL |
| `minio.endpoint` | "" (in-cluster) | S3-compatible storage endpoint |
| `backends.enabled` | true | self-hosted Qdrant/Weaviate/Chroma StatefulSets |
| `backends.*Url` | in-cluster service URLs | managed endpoints when `backends.enabled=false` |
| `api.ingress.host` / `annotations` | `api.example.com` | ingress route (disabled with `api.ingress.enabled=false`) |
| `sentry.dsn` / `otelExporterEndpoint` | "" | observability sinks |

## Parity with kustomize

The chart and `deploy/k8s` deploy the same resources with the same names and
the same operational contract (migrate initContainer, admin-port probes, no
admin Service/Ingress). The kustomize overlays remain the no-Helm path; keep
behavioral changes (probes, securityContext, the admin boundary) in sync
across both — the CI helm validation catches template breakage, not
behavioral drift.
