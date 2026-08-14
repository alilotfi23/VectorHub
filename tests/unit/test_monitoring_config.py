"""Deployment monitoring config sanity checks.

Prometheus rules and the Grafana dashboard are data files, so promtool and
Grafana's import validation normally catch mistakes — neither exists in this
repo's test env yet. These tests pin the essential structure:

- every rule carries alert name, expr, for, labels, and annotations;
- every PromQL expr references only metric names that actually exist
  (vhk_* families from app/core/metrics.py, http_* from the instrumentator);
- the dashboard parses, has the datasource variable, and every panel's
  targets reference known metrics too;
- Alertmanager routing covers every severity the rules emit (critical → pager,
  warning/default → slack) with credentials from env vars, never literals.
"""

import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "deploy" / "monitoring" / "prometheus" / "alerts.yml"
DASHBOARD = ROOT / "deploy" / "monitoring" / "grafana" / "dashboards" / "platform.json"
ALERTMANAGER = ROOT / "deploy" / "monitoring" / "alertmanager" / "alertmanager.yml"
TEMPLATES = ROOT / "deploy" / "monitoring" / "alertmanager" / "templates" / "vhk.tmpl"

# Metric families defined in app/core/metrics.py plus those registered by
# prometheus-fastapi-instrumentator (see app/main.py).
KNOWN_METRICS = {
    "vhk_requests_total",
    "vhk_health_checks_total",
    "vhk_rate_limit_rejections_total",
    "http_requests_total",
    "http_request_duration_seconds",
    "http_request_duration_seconds_bucket",
    "http_request_duration_seconds_count",
    "http_request_duration_seconds_sum",
    "http_request_duration_highr_seconds",
    "http_request_duration_highr_seconds_bucket",
    "http_request_duration_highr_seconds_count",
    "http_request_duration_highr_seconds_sum",
    "http_request_size_bytes",
    "http_request_size_bytes_sum",
    "http_request_size_bytes_count",
    "http_response_size_bytes",
    "http_response_size_bytes_sum",
    "http_response_size_bytes_count",
}

_METRIC_RE = re.compile(r"\b(vhk_[a-z0-9_]+|http_[a-z0-9_]+)\b")


def _all_metric_refs(expr: str) -> set[str]:
    return {m for m in _METRIC_RE.findall(expr)}


def _assert_known_metrics(expr: str) -> None:
    unknown = _all_metric_refs(expr) - KNOWN_METRICS
    assert not unknown, f"expr references unknown metric(s) {sorted(unknown)}: {expr}"


def _load_alerts() -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    with ALERTS.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    groups = doc["groups"]
    assert isinstance(groups, list) and groups, "alerts.yml must define at least one group"
    for group in groups:
        assert group["name"], "every group needs a name"
        assert isinstance(group["rules"], list) and group["rules"], (
            f"group {group['name']} has no rules"
        )
        rules.extend(group["rules"])
    return rules


def test_alert_rules_have_complete_structure() -> None:
    """Every rule pins name, expr, for, labels, annotations — the contract
    Alertmanager needs to route and render alerts."""
    for rule in _load_alerts():
        assert rule["alert"], "rule missing 'alert' name"
        assert rule["expr"], f"rule {rule['alert']} missing 'expr'"
        assert rule["for"], f"rule {rule['alert']} missing 'for'"
        labels = rule["labels"]
        assert labels["severity"] in {"critical", "warning"}, (
            f"rule {rule['alert']} has bad severity"
        )
        annotations = rule["annotations"]
        assert annotations["summary"] and annotations["description"], (
            f"rule {rule['alert']} needs summary+description"
        )
        _assert_known_metrics(rule["expr"])


def test_alert_rules_cover_the_agreed_behaviors() -> None:
    """The named examples from the request — error-rate spike, 429 flood,
    health-check flapping — plus the critical-dependency and latency gates."""
    names = {rule["alert"] for rule in _load_alerts()}
    expected = {
        "VhkCriticalDependencyDown",
        "VhkErrorRateSpike",
        "VhkRateLimitFlood",
        "VhkBackendFlapping",
        "VhkHighP99Latency",
    }
    assert expected <= names, f"missing rules: {sorted(expected - names)}"


def test_critical_dependency_rule_is_critical() -> None:
    """Only postgres/redis may be 'critical' — backends degrade, they don't
    page. This pins the /health contract in the alerting layer."""
    for rule in _load_alerts():
        if rule["labels"]["severity"] == "critical":
            assert "postgres|redis" in rule["expr"], (
                f"critical rule {rule['alert']} must be scoped to postgres|redis"
            )


def test_dashboard_parses_with_datasource_variable() -> None:
    """The dashboard JSON is valid, carries a Prometheus datasource variable,
    and every panel's queries reference known metrics."""
    with DASHBOARD.open(encoding="utf-8") as f:
        dash = json.load(f)

    assert dash["title"] == "VectorHub Platform"
    assert dash["uid"] == "vhk-platform"
    assert isinstance(dash["schemaVersion"], int)

    datasources = [v["name"] for v in dash["templating"]["list"]]
    assert "datasource" in datasources, "dashboard needs the datasource variable"

    panels = dash["panels"]
    assert panels, "dashboard has no panels"
    ids: list[int] = []
    for panel in panels:
        ids.append(panel["id"])
        assert panel["title"] and panel["type"], f"panel {panel['id']} missing title/type"
        assert isinstance(panel["gridPos"], dict), f"panel {panel['id']} missing gridPos"
        for target in panel["targets"]:
            assert target["refId"], f"panel {panel['id']} target missing refId"
            _assert_known_metrics(target["expr"])
    assert len(ids) == len(set(ids)), "panel ids must be unique"


def test_dashboard_has_per_route_latency_panel() -> None:
    """Hot vector-query routes must be isolatable from control-plane traffic:
    a 'route' variable (multi-select, includeAll, populated from the
    instrumentator's handler label) feeds a per-handler latency panel via
    handler=~"$route" — the isolation knob the global latency panel lacks."""
    with DASHBOARD.open(encoding="utf-8") as f:
        dash = json.load(f)

    variables = {v["name"]: v for v in dash["templating"]["list"]}
    route_var = variables.get("route")
    assert route_var is not None, "dashboard needs the 'route' variable"
    assert route_var["type"] == "query"
    assert route_var["multi"] is True, "route variable must allow multi-select"
    assert route_var["includeAll"] is True, "route variable must allow 'All'"
    assert route_var["allValue"] == ".*", "All must expand to a match-all regex"
    query = route_var["query"]
    if isinstance(query, dict):
        query = query["query"]
    assert query.startswith("label_values("), "route variable must be label_values-driven"
    assert "http_request_duration_highr_seconds_bucket" in query, (
        "route variable must list the latency histogram's handler label"
    )

    latency = [p for p in dash["panels"] if "Route latency" in p["title"]]
    assert len(latency) == 1, "exactly one per-route latency panel"
    panel = latency[0]
    assert panel["type"] == "timeseries"
    assert panel["gridPos"]["w"] == 12
    quantiles = set()
    for target in panel["targets"]:
        assert 'handler=~"$route"' in target["expr"], (
            f"target {target['refId']} must filter by $route"
        )
        assert "by (le, handler)" in target["expr"], (
            f"target {target['refId']} must break percentiles down per handler"
        )
        quantiles.add(target["legendFormat"])
        _assert_known_metrics(target["expr"])
    assert quantiles == {"{{handler}} p50", "{{handler}} p95", "{{handler}} p99"}, (
        f"panel must plot p50/p95/p99 per handler, got {quantiles}"
    )


def _load_alertmanager() -> dict[str, Any]:
    with ALERTMANAGER.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    assert isinstance(doc, dict), "alertmanager.yml must parse to a mapping"
    return doc


def test_alertmanager_routes_every_emitted_severity() -> None:
    """Cross-checks routing against the rules: every severity alerts.yml emits
    must land somewhere — critical routed explicitly to the pager, warning
    covered by the default Slack receiver. This is the coupling that prevents
    a new severity from silently falling through to no one."""
    am = _load_alertmanager()
    receivers = {r["name"] for r in am["receivers"]}
    assert {"slack", "pager"} <= receivers, "receivers must define slack and pager"

    route = am["route"]
    assert route["receiver"] == "slack", "default receiver must be slack (warnings)"
    assert route["group_by"], "route must group alerts"

    routed: dict[str, str] = {}
    for child in route.get("routes", []):
        for matcher in child["matchers"]:
            if matcher.startswith("severity="):
                routed[matcher.split("=", 1)[1].strip('"')] = child["receiver"]
    assert routed == {"critical": "pager"}, f"unexpected severity routing: {routed}"

    emitted = {rule["labels"]["severity"] for rule in _load_alerts()}
    assert emitted <= set(routed) | {"warning"}, (
        f"severity {sorted(emitted - set(routed) - {'warning'})} has no route"
    )


def test_alertmanager_credentials_are_env_only() -> None:
    """Receiver credentials come from ${VAR} expansion, never literals — no
    webhook URLs, tokens, or keys may be committed."""
    text = ALERTMANAGER.read_text(encoding="utf-8")
    assert "${SLACK_WEBHOOK_URL}" in text, "slack receiver must use the env var"
    assert "${PAGERDUTY_ROUTING_KEY}" in text, "pager receiver must use the env var"
    for secret_marker in ("xoxb-", "xoxp-", "hooks.slack.com/services/", "PDKK-"):
        assert secret_marker not in text, f"literal secret marker found: {secret_marker}"


def test_alertmanager_templates_referenced_and_defined() -> None:
    """The config points at a templates glob, and the shipped template file
    defines the two templates the receivers reference (vhk.title, vhk.text)."""
    am = _load_alertmanager()
    assert am.get("templates"), "alertmanager.yml must reference a templates path"
    assert any("*.tmpl" in p for p in am["templates"]), "templates glob must cover *.tmpl files"

    tmpl = TEMPLATES.read_text(encoding="utf-8")
    assert 'define "vhk.title"' in tmpl, "template must define vhk.title"
    assert 'define "vhk.text"' in tmpl, "template must define vhk.text"


def test_alertmanager_slack_posts_rich_fields() -> None:
    """The Slack receiver posts structured attachment fields (summary, value,
    check) — not just a text blob — so the at-a-glance facts are scannable.
    Field values must come from the alert payload and optional labels must
    render a placeholder instead of Go's <no value>."""
    am = _load_alertmanager()
    slack = next(r for r in am["receivers"] if r["name"] == "slack")
    sc = slack["slack_configs"][0]
    fields = sc["fields"]
    assert fields, "slack receiver must define fields"
    assert sc["title"], "slack receiver must keep a title"

    by_title = {f["title"]: f["value"] for f in fields}
    required = {"Summary", "Value", "Check"}
    assert required <= set(by_title), f"missing required fields: {sorted(required - set(by_title))}"

    # facts must be pulled from the alert payload, never hardcoded
    assert ".CommonAnnotations.summary" in by_title["Summary"]
    assert "range .Alerts" in by_title["Value"], "value must iterate the group's alerts"
    assert ".Value" in by_title["Value"]
    assert "CommonLabels.check" in by_title["Check"]
    # optional labels (check/limit) need a with/else so absent labels render
    # a placeholder, not <no value>
    assert "else" in by_title["Check"], "check field must guard missing labels"
    assert "with .CommonLabels.limit" in by_title["Limit"]
    # every field must be a template expression, not literal text
    for title, value in by_title.items():
        assert "{{" in value, f"field {title} value is not templated: {value!r}"


def test_alertmanager_inhibits_downstream_warnings_during_critical() -> None:
    """While VhkCriticalDependencyDown fires, the symptom warnings (error
    spikes, 429 floods, stale workers, latency) must be inhibited so one
    incident pages once. Vector-backend alerts are deliberately NOT inhibited
    — backends are physically independent of the control plane. The coverage
    check is airtight: every emitted warning is either inhibited or named as
    an independent backend rule, so a new warning rule forces a decision."""
    am = _load_alertmanager()
    inhibits = am.get("inhibit_rules")
    assert inhibits, "alertmanager.yml must define inhibit_rules"

    inhibited: set[str] = set()
    for rule in inhibits:
        src = rule["source_matchers"]
        assert 'alertname="VhkCriticalDependencyDown"' in src, (
            "inhibit source must be the critical dependency alert"
        )
        assert 'severity="critical"' in src, "inhibit source must be severity=critical"
        assert "equal" not in rule, "no equal labels: the platform is one instance"
        for matcher in rule["target_matchers"]:
            assert matcher.startswith('severity="warning"') or matcher.startswith("alertname"), (
                f"unexpected target matcher: {matcher}"
            )
            if matcher.startswith("alertname=~"):
                inner = re.findall(r"Vhk\(([^)]+)\)", matcher)
                assert inner, f"could not parse alertname regex: {matcher}"
                inhibited |= {f"Vhk{name}" for name in inner[0].split("|")}
            elif matcher.startswith("alertname="):
                inhibited.add(matcher.split("=", 1)[1].strip('"'))

    emitted_warnings = {
        rule["alert"] for rule in _load_alerts() if rule["labels"]["severity"] == "warning"
    }
    assert inhibited <= emitted_warnings, (
        f"inhibit targets unknown alerts: {sorted(inhibited - emitted_warnings)}"
    )
    symptom = {
        "VhkErrorRateSpike",
        "VhkRateLimitFlood",
        "VhkWorkerHeartbeatStale",
        "VhkHighP99Latency",
    }
    assert symptom <= inhibited, f"symptom rules must be inhibited: {sorted(symptom - inhibited)}"
    independent = {"VhkBackendDown", "VhkBackendFlapping"}
    assert independent.isdisjoint(inhibited), (
        f"backend rules must stay live, got inhibited: {sorted(independent & inhibited)}"
    )
    assert emitted_warnings == inhibited | independent, (
        f"every warning must be inhibited or independent; uncovered: "
        f"{sorted(emitted_warnings - inhibited - independent)}"
    )
