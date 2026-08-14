"""Response schemas for infra probes (GET /health)."""

from typing import Literal

from pydantic import BaseModel

HealthStatus = Literal["ok", "degraded", "down"]


class HealthReport(BaseModel):
    """Per-dependency status plus the overall platform status.

    ``checks`` maps each dependency to ``"ok"`` or ``"down"``; ``adapters``
    nests one entry per backend registered in the AdapterRegistry (an empty
    map pre-Phase-3). Overall ``status`` is ``"ok"`` (200), ``"degraded"``
    (200 — a non-critical dependency, e.g. a vector backend or worker, is
    down while the control plane is fine), or ``"down"`` (503 — Postgres or
    Redis unreachable).
    """

    status: HealthStatus
    checks: dict[str, str | dict[str, str]]
