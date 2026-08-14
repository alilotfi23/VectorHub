# Platform monitoring (Phase 7 pull-forward)

Alert rules and a Grafana dashboard for the metrics exposed by
`GET /metrics`. Phase 8's deployment artifacts reference these paths.

## What's here

| Path | Purpose |
| --- | --- |
| `prometheus/alerts.yml` | Prometheus alert rules for the `vhk_*` + `http_*` metric families |
| `alertmanager/alertmanager.yml` | Alertmanager routing: `critical` → pager (PagerDuty), `warning`/default → Slack, env-driven credentials; inhibition of downstream symptom warnings while the control plane is down |
| `alertmanager/templates/vhk.tmpl` | Notification title/text templates referenced by the Alertmanager config |
| `grafana/dashboards/platform.json` | Dashboard: request rate by status, error rate, latency percentiles, per-dependency health failures, rate-limit rejections, 429 rate, response size, per-route latency |

Validate with the bundled unit test (`tests/unit/test_monitoring_config.py`),
which parses both files and pins their structure — `promtool check rules` and
Grafana's import validation do the same in CI once Phase 8 ships the stack.

## Wiring

**Prometheus** — add the rules file to `rule_files` and (optionally) point
Alertmanager at the deployment:

```yaml
rule_files:
  - /etc/prometheus/alerts.yml   # copy of deploy/monitoring/prometheus/alerts.yml
```

Rules emit `severity` labels (`critical` | `warning`). A shipped Alertmanager
config (`alertmanager/alertmanager.yml`) routes them: `critical` → PagerDuty,
`warning` and anything unlabeled → Slack. The Slack receiver posts structured
attachment fields (alert, severity, summary, value, check, limit) alongside
the detail text, so the at-a-glance facts are scannable — optional labels
render a placeholder rather than `<no value>`. Credentials come from secret
**files** — the config uses `api_url_file` / `routing_key_file`, because
Alertmanager does **not** expand `${VAR}` in config (it would parse the
literal and crash). The compose sources both vars as environment secrets,
written to `/run/secrets/...` (k8s mounts a Secret at the same paths):

```yaml
secrets:
  slack_webhook_url:
    environment: SLACK_WEBHOOK_URL
  pagerduty_routing_key:
    environment: PAGERDUTY_ROUTING_KEY
```

Mount the config and templates:

```yaml
# alertmanager.yml (container mount points)
volumes:
  - ./deploy/monitoring/alertmanager/alertmanager.yml:/etc/alertmanager/alertmanager.yml
  - ./deploy/monitoring/alertmanager/templates:/etc/alertmanager/templates
```

The same config also **inhibits** the noisy symptom warnings while a
control-plane outage is firing: while `VhkCriticalDependencyDown` is active,
the downstream alerts it causes (5xx spike, 429 flood, stale worker heartbeat,
high latency) are suppressed so one incident pages once. Vector-backend alerts
(`VhkBackendDown`, `VhkBackendFlapping`) are deliberately **not** inhibited —
backends are physically independent of the control plane, so a real backend
outage still pages even during a Postgres/Redis incident. Inhibition lasts
only as long as the source fires; suppressed alerts re-notify after recovery.

Prometheus must be told to forward:

```yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets: ["alertmanager:9093"]
```

All rules reference label names that exist on the platform's metrics (checked
against `app/core/metrics.py` and `app/services/health_service.py`):

- `vhk_requests_total{method, path, status}` — exact status code in `status`
- `vhk_health_checks_total{check, status}` — `check` is `postgres` | `redis` |
  `workers` | `adapter:<name>`; increments once per `/health` probe
- `vhk_rate_limit_rejections_total{limit}` — `limit` is `route_qps` |
  `api_key_qps` | `tenant_qps`
- `http_request_duration_highr_seconds_bucket` — fine-grained histogram for
  percentile queries

**Grafana** — import the dashboard JSON, or provision it:

```yaml
# grafana/provisioning/dashboards/vhk.yml
apiVersion: 1
providers:
  - name: vhk
    folder: VectorHub
    type: file
    options:
      path: /var/lib/grafana/dashboards   # copy of platform.json
```

The dashboard takes a `datasource` variable (default: the first Prometheus
datasource) so the same JSON works across environments. A second variable,
`route`, lists every templated handler (from the latency histogram's `handler`
label) and is multi-select with an `All` option. It drives the **Route latency
percentiles** panel: pick the hot vector-query routes (e.g.
`/api/v1/collections/{name}/query`, `.../hybrid-query`) to isolate their
p50/p95/p99 from control-plane traffic (auth, tenants, collection CRUD), or
select `All` to decompose the global latency panel per route.

## Running the stack

```bash
cd deploy/monitoring
# export both vars first — alertmanager refuses to boot without credentials
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
export PAGERDUTY_ROUTING_KEY="..."
docker compose up -d --wait
#   Prometheus    http://localhost:9090
#   Alertmanager  http://localhost:9093
#   Grafana       http://localhost:3000   (admin / admin on first boot)
```

Prometheus scrapes the platform's admin app at `app:9091` — the service name
used by the main docker-compose (Phase 8). Running the trio standalone against
an API on the host? Point `prometheus/prometheus.yml` at
`host.docker.internal:9091` instead.

## Kubernetes scraping contract

In-cluster Prometheus discovers the VectorHub pod by **annotation** — the
standard Prometheus-on-k8s recipe. The pod must carry:

| Annotation | Value | Meaning |
| --- | --- | --- |
| `prometheus.io/scrape` | `"true"` | Opt the pod into scraping |
| `prometheus.io/path` | `"/metrics"` | The metrics endpoint (admin app) |
| `prometheus.io/port` | `"9091"` | **Must equal `ADMIN_PORT`** (`Settings.admin_port`) |

`prometheus/prometheus.k8s.yml` ships the matching scrape config
(`kubernetes_sd_configs`, `role: pod`, honoring those annotations via the
keep/path/port relabel recipes) — put it in a ConfigMap as the base of the
Prometheus `--config.file`. The contract is test-pinned: the annotation port
and the compose static target must both equal `Settings.admin_port`, so the
flag that exposes `/metrics` on the internal port is the single source of
truth for every scrape path.

Two non-negotiables for the app deployment:

- Set `ADMIN_HOST=0.0.0.0` — k8s probes/scrapers reach the pod by IP, not
  localhost, and the admin app binds `127.0.0.1` by default.
- Never expose the admin port via Service/Ingress/NodePort/LoadBalancer —
  `/metrics` and `/health` are unauthenticated by design; the security
  boundary is that the port is unreachable outside the pod network.

The Grafana dashboard (`platform.json`) is provisioned the same way in k8s as
in compose: a ConfigMap with the dashboard JSON + the provisioning files from
`grafana/provisioning/` (datasource → the in-cluster Prometheus service, e.g.
`http://prometheus.monitoring.svc:9090`), mounted at
`/etc/grafana/provisioning` and the dashboards path.

## Threshold rationale (adjust per SLA)

| Rule | Threshold | Why |
| --- | --- | --- |
| `VhkCriticalDependencyDown` | any Postgres/Redis probe failure, `for: 2m` | Only these drive `/health` to 503 and k8s restarts — page immediately |
| `VhkErrorRateSpike` | 5xx > 5% of requests, `for: 5m` | 5% for 5 minutes is a degraded API, not a blip |
| `VhkRateLimitFlood` | > 1 rejection/s, `for: 5m` | One 429/s sustained means a runaway client or misconfigured quota |
| `VhkWorkerHeartbeatStale` | workers check down, `for: 15m` | Workers are absent during deploys; 15m rides that out |
| `VhkBackendDown` | any adapter probe failure, `for: 5m` | Degrades but never fails the probe (no restart loop) |
| `VhkBackendFlapping` | two down episodes with a clean healthy gap in 30m | Crash/restart loop vs. a clean single outage — see the rule comment for the counter-only detection semantics |
| `VhkHighP99Latency` | p99 > 2s, `for: 10m` | Sustained p99 over 2s on a vector API is a backend or index problem |
| Inhibition | `VhkCriticalDependencyDown` suppresses error-spike / 429-flood / stale-worker / high-latency warnings | One incident, one page — the pager sees the root cause, not its symptoms; backend alerts stay live since backends are independent of the control plane |
