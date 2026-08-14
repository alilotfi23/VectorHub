# Platform monitoring (Phase 7 pull-forward)

Alert rules and a Grafana dashboard for the metrics exposed by
`GET /metrics`. Phase 8's deployment artifacts reference these paths.

## What's here

| Path | Purpose |
| --- | --- |
| `prometheus/alerts.yml` | Prometheus alert rules for the `vhk_*` + `http_*` metric families |
| `alertmanager/alertmanager.yml` | Alertmanager routing: `critical` → pager (PagerDuty), `warning`/default → Slack, env-driven credentials |
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
`warning` and anything unlabeled → Slack. Credentials are injected by env vars
(`SLACK_WEBHOOK_URL`, `PAGERDUTY_ROUTING_KEY`) — Alertmanager expands `${VAR}`
at startup, so the config carries no secrets. Mount the config and templates:

```yaml
# alertmanager.yml (container mount points)
volumes:
  - ./deploy/monitoring/alertmanager/alertmanager.yml:/etc/alertmanager/alertmanager.yml
  - ./deploy/monitoring/alertmanager/templates:/etc/alertmanager/templates
environment:
  SLACK_WEBHOOK_URL: "https://hooks.slack.com/services/..."
  PAGERDUTY_ROUTING_KEY: "..."
```

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
