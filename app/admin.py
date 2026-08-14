"""Internal admin ASGI app: /health and /metrics, and nothing else.

A separate app on its own port (``ADMIN_HOST:ADMIN_PORT``) so the public API
app never exposes the probe and scrape endpoints — public traffic physically
cannot reach them, which is the security boundary (both endpoints are
unauthenticated by design; scrapers/probes can't do auth). ``app/main.py``'s
``run()`` serves this app and the public app in one process.

**Same-process requirement:** the Prometheus registry is process-local — the
counters (``vhk_requests_total``, ``vhk_health_checks_total``, ...) and the
instrumentator's series are recorded by the *public* app's middleware and the
health service. This admin app renders that shared default registry, so it
must run in the same process as the public app. Running it standalone in a
separate container would scrape an empty registry — don't.

Deliberately absent: auth, rate limiting, CORS, the metrics/access-log
middleware, request-ID correlation, and the OpenAPI docs. The admin surface
is two GET routes; anything else is a 404. It is not part of the public
request path, so it shouldn't consume rate-limit budget or pollute the
request metrics with its own scrape traffic.
"""

from fastapi import Depends, FastAPI, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import metrics_response
from app.db.session import get_session
from app.schemas.health import HealthReport
from app.services.health_service import check_health

app = FastAPI(
    title="VectorHub admin (internal)",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health", response_model=HealthReport, tags=["infra"])
async def health(response: Response, session: AsyncSession = Depends(get_session)) -> HealthReport:
    """Per-dependency health probe (see the /health note in CLAUDE.md).

    Returns 200 while Postgres and Redis are up — ``"degraded"`` when a
    non-critical dependency (worker, vector backend) is down — and 503 with
    overall ``"down"`` when a critical dependency is unreachable.
    """
    report = await check_health(session)
    response.status_code = 503 if report.status == "down" else 200
    return report


@app.get("/metrics", tags=["infra"])
async def metrics() -> Response:
    """Prometheus text-format exposition of the platform's counters
    (request counts, health-check outcomes, rate-limit rejections) plus the
    instrumentator's duration/size series — the shared default registry.
    """
    return metrics_response()
