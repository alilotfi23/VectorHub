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
