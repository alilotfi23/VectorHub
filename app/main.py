import asyncio
import contextlib
import signal
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from types import FrameType

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_fastapi_instrumentator import metrics as instrumentator_metrics

from app.admin import app as admin_app
from app.api.v1 import api_keys, auth, collections, tenants
from app.core.cache import close_redis
from app.core.config import get_settings
from app.core.exceptions import AppError, error_response_handler
from app.core.logging import setup_logging
from app.core.tracing import setup_tracing
from app.middleware.metrics import MetricsMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.tracing import TraceMiddleware

settings = get_settings()

setup_logging()
setup_tracing()


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

# Request-duration histograms and request/response size metrics (Phase 7
# pull-forward). Registers on the shared default registry, so the admin app's
# /metrics route renders these series alongside the platform counters.
_instrumentator = Instrumentator(should_group_status_codes=True)
_instrumentator.add(instrumentator_metrics.default())
_instrumentator.instrument(app)

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

# Request-ID + OpenTelemetry correlation wraps everything (including CORS
# and rate-limit 429s), so every request gets a trace and a logged request
# ID no matter where it stops. Registered last = outermost.
app.add_middleware(TraceMiddleware)

# The public app deliberately exposes NO /health or /metrics route — those
# live on app.admin (the internal admin app), so public traffic can't reach
# the probe/scrape endpoints. See app/admin.py for the boundary rationale.


class _SignalNeutralServer(uvicorn.Server):
    """uvicorn.Server without its own signal capture.

    Two servers in one process can't both install signal handlers — the
    second ``capture_signals()`` would override the first's, so SIGINT/SIGTERM
    would stop only one server and shutdown would hang. ``run()`` owns the
    process's signals and stops both servers via ``should_exit``.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


def run() -> None:
    """Serve the public API and the internal admin app in one process.

    - Public app (``app.main:app``): the /api/v1 surface on API_HOST:API_PORT.
      No /health, no /metrics.
    - Admin app (``app.admin:app``): /health and /metrics on
      ADMIN_HOST:ADMIN_PORT (default 127.0.0.1 — internal only).

    The admin app must share this process with the public app: the Prometheus
    registry is process-local, so the scrape endpoint only sees real counters
    here. Run via ``python -m app.main``.
    """
    cfg = get_settings()
    servers = [
        _SignalNeutralServer(
            uvicorn.Config(app, host=cfg.api_host, port=cfg.api_port, log_level="info")
        ),
        _SignalNeutralServer(
            uvicorn.Config(admin_app, host=cfg.admin_host, port=cfg.admin_port, log_level="info")
        ),
    ]

    def _stop_all(_signum: int, _frame: FrameType | None) -> None:
        for server in servers:
            server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _stop_all)

    async def _serve() -> None:
        await asyncio.gather(*(server.serve() for server in servers))

    asyncio.run(_serve())


if __name__ == "__main__":
    run()
