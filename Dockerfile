# syntax=docker/dockerfile:1
# Multi-stage build: uv (the project's pinned dependency manager) resolves
# from uv.lock in the builder, and the runtime image ships only the resolved
# .venv + application source — no uv, no dev dependencies, no build tools.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Layer-cache the dependency resolution: only the lockfile + project metadata
# participate in this layer, so dependency bumps are the only thing that
# invalidates it.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now the full source, and a second sync that installs the project itself
# (the `vectorhub-platform` package the entrypoints import).
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY deploy/migrate.sh ./deploy/migrate.sh
RUN uv sync --frozen --no-dev


FROM python:3.13-slim

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Runtime user: non-root per the deployment requirements. The venv is
# read-only for it; nothing in the image needs to be written at runtime
# (batch results go to object storage, the control plane to Postgres).
RUN useradd --create-home --uid 10001 appuser

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/app /app/app
COPY --from=builder --chown=appuser:appuser /app/alembic /app/alembic
COPY --from=builder --chown=appuser:appuser /app/alembic.ini /app/alembic.ini
COPY --from=builder --chown=appuser:appuser /app/deploy/migrate.sh /usr/local/bin/migrate.sh
RUN chmod +x /usr/local/bin/migrate.sh

USER appuser

# Public API on 8000; the internal admin app (health/metrics) binds
# ADMIN_HOST:ADMIN_PORT (9091) in the same process. Only 8000 is "exposed" —
# the admin port is reachable on the container network for probes/scrapers
# but must never be published (compose) or given a Service/Ingress (k8s).
EXPOSE 8000

# The liveness/readiness signal is the admin app's /health (Postgres + Redis
# + worker heartbeats + per-backend adapter checks), served on the admin port
# in this same process. ADMIN_HOST=0.0.0.0 in containers so the probe path is
# reachable on the container network; 127.0.0.1 binds loopback only.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://' + os.environ.get('ADMIN_HOST', '127.0.0.1') + ':' + os.environ.get('ADMIN_PORT', '9091') + '/health', timeout=3)"

# One process, two apps (app.main:run serves the public API and the internal
# admin app together — the Prometheus registry is process-local). Scale
# horizontally at the orchestrator level (k8s HPA / compose scale), not with
# multiple uvicorn workers here.
CMD ["python", "-m", "app.main"]
