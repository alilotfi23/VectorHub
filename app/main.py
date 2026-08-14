from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import api_keys, auth, collections, tenants
from app.core.cache import close_redis
from app.core.config import get_settings
from app.core.exceptions import AppError, error_response_handler

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # The Redis client is built lazily on first use (see get_redis); the
    # lifespan only owns its teardown.
    yield
    await close_redis()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

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


@app.get("/health")
async def health() -> dict[str, str]:
    """Phase 1: process liveness only. Dependency checks (Postgres, Redis,
    workers, adapters) land in Phase 7 per the /health spec."""
    return {"status": "ok"}
