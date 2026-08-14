from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import api_keys, auth, collections, tenants
from app.core.cache import close_redis
from app.core.config import get_settings
from app.core.exceptions import AppError, error_response_handler
from app.core.metrics import metrics_response
from app.db.session import get_session
from app.middleware.metrics import MetricsMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.schemas.health import HealthReport
from app.services.health_service import check_health

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # The Redis client is built lazily on first use (see get_redis); the
    # lifespan only owns its teardown.
    yield
    await close_redis()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

# Metrics counts every request, including rate-limit 429s — it must wrap the
# rate-limit middleware, so it is registered last (outermost).
app.add_middleware(RateLimitMiddleware)
app.add_middleware(MetricsMiddleware)
app.add_exception_handler(AppError, error_response_handler)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(tenants.router, prefix="/api/v1")
app.include_router(api_keys.router, prefix="/api/v1")
app.include_router(collections.router, prefix="/api/v1")

if settings.cors_allowed_origins.strip() == "*":
    allow_origins = ["*"]
else:
    allow_origins = [o.strip() for o in settings.cors_allowed_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
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
    (request counts, health-check outcomes). Scraped by infra probes.
    """
    return metrics_response()
