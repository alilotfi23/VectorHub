"""Monitoring stack deployment checks (docker-compose trio).

The compose (prometheus + alertmanager + grafana) and the Prometheus/Grafana
wiring files are data, so these tests pin the pieces that make the stack
actually work: every service mounts the shipped configs (the same files
test_monitoring_config.py parses), Prometheus loads the alert rules and
forwards to Alertmanager, the datasource/dashboard provisioning points at the
compose services, and the alertmanager credentials are passed through from the
environment — its config refuses to boot with an empty api_url/routing_key.
"""

from pathlib import Path
from typing import Any

import yaml

from app.core.config import get_settings

ROOT = Path(__file__).resolve().parents[2]
MONITORING = ROOT / "deploy" / "monitoring"
COMPOSE = MONITORING / "docker-compose.yml"
PROMETHEUS_CONFIG = MONITORING / "prometheus" / "prometheus.yml"
K8S_PROMETHEUS = MONITORING / "prometheus" / "prometheus.k8s.yml"
DATASOURCES = MONITORING / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
DASHBOARD_PROVIDER = MONITORING / "grafana" / "provisioning" / "dashboards" / "vhk.yml"


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    assert isinstance(doc, dict), f"{path.name} must parse to a mapping"
    return doc


def test_compose_runs_the_monitoring_trio() -> None:
    """The compose defines exactly the three monitoring services, each with an
    image, and the data volumes the services write to."""
    compose = _load(COMPOSE)
    services = compose["services"]
    assert set(services) == {"prometheus", "alertmanager", "grafana"}
    for name in services:
        assert services[name]["image"], f"{name} needs an image"
    assert set(compose.get("volumes", {})) >= {"prometheus-data", "grafana-data"}


def test_compose_mounts_the_shipped_configs() -> None:
    """Every service mounts the config files this repo ships — the same files
    the other monitoring tests parse — read-only, at the container paths the
    images expect. Any source path must exist relative to the compose file, so
    a renamed/moved config fails here instead of silently at container start."""
    compose = _load(COMPOSE)
    services = compose["services"]

    prom_vols = services["prometheus"]["volumes"]
    for mount in (
        "./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro",
        "./prometheus/alerts.yml:/etc/prometheus/alerts.yml:ro",
    ):
        assert mount in prom_vols, f"prometheus missing mount {mount}"

    am_vols = services["alertmanager"]["volumes"]
    for mount in (
        "./alertmanager/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro",
        "./alertmanager/templates:/etc/alertmanager/templates:ro",
    ):
        assert mount in am_vols, f"alertmanager missing mount {mount}"

    grafana_vols = services["grafana"]["volumes"]
    for mount in (
        "./grafana/provisioning:/etc/grafana/provisioning:ro",
        "./grafana/dashboards:/var/lib/grafana/dashboards:ro",
    ):
        assert mount in grafana_vols, f"grafana missing mount {mount}"

    for service in services.values():
        for volume in service["volumes"]:
            source = volume.split(":", 1)[0]
            if source.startswith("./"):
                assert (MONITORING / source[2:]).exists(), f"mount source missing: {source}"


def test_alertmanager_credentials_via_environment_secrets() -> None:
    """Alertmanager reads credentials from secret files (its config has no
    env expansion), so the compose must write both vars to /run/secrets/...
    via environment secrets — the exact paths the config's *_file fields
    reference. Missing vars fail compose with a clear error, never a crash
    loop, and no literal secret may appear in the compose."""
    compose = _load(COMPOSE)
    secrets = compose["secrets"]
    assert secrets["slack_webhook_url"]["environment"] == "SLACK_WEBHOOK_URL"
    assert secrets["pagerduty_routing_key"]["environment"] == "PAGERDUTY_ROUTING_KEY"

    am_secrets = compose["services"]["alertmanager"]["secrets"]
    assert "slack_webhook_url" in am_secrets
    assert "pagerduty_routing_key" in am_secrets

    # the secret names must match the paths the alertmanager config reads
    text = (MONITORING / "alertmanager" / "alertmanager.yml").read_text(encoding="utf-8")
    for name in am_secrets:
        assert f"/run/secrets/{name}" in text, (
            f"secret {name} must be referenced by the alertmanager config"
        )
    for marker in ("xoxb-", "hooks.slack.com/services/"):
        assert marker not in COMPOSE.read_text(encoding="utf-8"), (
            f"literal secret marker in compose: {marker}"
        )


def test_prometheus_config_loads_rules_and_forwards_alerts() -> None:
    """Prometheus must load the shipped alert rules, forward to the compose
    alertmanager, and scrape the platform's admin /metrics endpoint."""
    pc = _load(PROMETHEUS_CONFIG)
    assert "/etc/prometheus/alerts.yml" in pc["rule_files"], (
        "rule_files must load the shipped alerts.yml"
    )
    targets = pc["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
    assert targets == ["alertmanager:9093"], "alerting must point at the compose alertmanager"

    jobs = {j["job_name"]: j for j in pc["scrape_configs"]}
    vhk = jobs["vhk"]
    assert vhk["metrics_path"] == "/metrics"
    assert vhk["static_configs"][0]["targets"] == [f"app:{get_settings().admin_port}"], (
        "vhk job must scrape the app's admin port"
    )
    assert "prometheus" in jobs, "self-scrape job must exist"


def test_grafana_provisioning_points_at_compose_services() -> None:
    """The datasource provisioning targets the compose prometheus, and the
    dashboard provider's path is exactly where the compose mounts the shipped
    dashboard directory — so the VectorHub dashboard appears without manual
    setup."""
    ds = _load(DATASOURCES)
    datasource = ds["datasources"][0]
    assert datasource["name"] == "Prometheus"
    assert datasource["type"] == "prometheus"
    assert datasource["url"] == "http://prometheus:9090"
    assert datasource["isDefault"] is True

    provider = _load(DASHBOARD_PROVIDER)
    prov = provider["providers"][0]
    assert prov["name"] == "vhk"
    assert prov["type"] == "file"
    assert prov["options"]["path"] == "/var/lib/grafana/dashboards"

    grafana_vols = _load(COMPOSE)["services"]["grafana"]["volumes"]
    assert any(v.endswith("/var/lib/grafana/dashboards:ro") for v in grafana_vols), (
        "dashboard provider path must match the compose mount"
    )


def test_k8s_prometheus_config_uses_annotation_contract() -> None:
    """The k8s scrape config discovers the app pod by annotation
    (prometheus.io/scrape=true, path, port) instead of a static target — the
    standard Prometheus-on-k8s recipe, with the pod's k8s labels kept for
    grouping."""
    pc = _load(K8S_PROMETHEUS)
    assert "/etc/prometheus/alerts.yml" in pc["rule_files"], (
        "k8s config must load the same shipped alerts.yml"
    )
    targets = pc["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
    assert targets == ["alertmanager:9093"]

    jobs = {j["job_name"]: j for j in pc["scrape_configs"]}
    vhk = jobs["vhk"]
    sd = vhk["kubernetes_sd_configs"][0]
    assert sd["role"] == "pod", "must discover pods"
    assert "static_configs" not in vhk, "k8s job must not use a static target"

    relabels = vhk["relabel_configs"]
    keeps = [r for r in relabels if r["action"] == "keep"]
    assert keeps and keeps[0]["regex"] == "true", "must keep only prometheus.io/scrape=true pods"
    actions = [r["action"] for r in relabels]
    assert "replace" in actions and "labelmap" in actions
    for r in relabels:
        if r["action"] == "replace" and r.get("target_label") == "__metrics_path__":
            assert "prometheus_io_path" in r["source_labels"][0], (
                "metrics path must come from the path annotation"
            )
        if r["action"] == "replace" and r.get("target_label") == "__address__":
            assert "prometheus_io_port" in r["source_labels"][1], (
                "address must be rewritten to the annotated port"
            )
    assert "prometheus" in jobs, "self-scrape job must exist"


def test_k8s_scrape_contract_matches_admin_port_setting() -> None:
    """One source of truth: the config flag that exposes /metrics on the
    internal admin port (Settings.admin_port) must be what the k8s annotation
    contract and the compose static target both reference. Changing the flag
    forces updating the contract — a scrape port that drifted from the app's
    actual listener would silently collect nothing."""
    admin_port = get_settings().admin_port

    k8s_text = K8S_PROMETHEUS.read_text(encoding="utf-8")
    assert f'prometheus.io/port:   "{admin_port}"' in k8s_text, (
        "k8s annotation contract must document the admin port"
    )
    assert 'prometheus.io/scrape: "true"' in k8s_text
    assert 'prometheus.io/path:   "/metrics"' in k8s_text

    compose_targets = _load(PROMETHEUS_CONFIG)["scrape_configs"][0]["static_configs"][0]["targets"]
    assert compose_targets == [f"app:{admin_port}"], (
        "compose static target must match the admin port setting"
    )
