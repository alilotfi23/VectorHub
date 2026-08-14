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

ROOT = Path(__file__).resolve().parents[2]
MONITORING = ROOT / "deploy" / "monitoring"
COMPOSE = MONITORING / "docker-compose.yml"
PROMETHEUS_CONFIG = MONITORING / "prometheus" / "prometheus.yml"
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
    assert vhk["static_configs"][0]["targets"] == ["app:9091"], (
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
