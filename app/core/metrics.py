"""Prometheus metrics (Phase 7 pull-forward).

Two counter families, both cheap and process-local:

- ``vhk_requests_total`` — every HTTP request, labeled by method, route path
  (resolved post-routing by the MetricsMiddleware so path params collapse to
  the route template, e.g. ``/api/v1/tenants/{tenant_id}``), and status code.
- ``vhk_health_checks_total`` — GET /health probe outcomes per dependency
  check, labeled by check (``postgres``/``redis``/``workers``/``adapter:<name>``)
  and status (``ok``/``down``).

Counters are the foundation; request latency histograms land with Phase 7's
prometheus-fastapi-instrumentator pass. GET /metrics renders the default
registry in the Prometheus text format.
"""

from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

REQUESTS_TOTAL = Counter(
    "vhk_requests_total",
    "Total HTTP requests processed, labeled by method, route path, and status code.",
    ["method", "path", "status"],
)

HEALTH_CHECKS_TOTAL = Counter(
    "vhk_health_checks_total",
    "Health probe outcomes per dependency check (ok|down).",
    ["check", "status"],
)

RATE_LIMIT_REJECTIONS_TOTAL = Counter(
    "vhk_rate_limit_rejections_total",
    "Rate-limit rejections (429s), labeled by the limit that was hit.",
    ["limit"],
)


def health_check_outcome(check: str, status: str) -> None:
    """Record one probe result for a dependency check."""
    HEALTH_CHECKS_TOTAL.labels(check=check, status=status).inc()


def rate_limit_rejection(limit: str) -> None:
    """Record one 429, labeled by the limit that rejected the request."""
    RATE_LIMIT_REJECTIONS_TOTAL.labels(limit=limit).inc()


def metrics_response() -> Response:
    """Render the default registry in the Prometheus text exposition format."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
        headers={"Cache-Control": "no-store"},
    )
