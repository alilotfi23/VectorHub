"""Structured logging (Phase 7 pull-forward: the first structured log lines).

structlog bound loggers with stdlib integration — records flow through
Python's logging system, so pytest's caplog and operational log pipelines
consume them uniformly. Phase 7 builds on this: request-ID correlation via
contextvars (``merge_contextvars`` is already in the chain) and a JSON
renderer for production.
"""

import logging
from typing import cast

import structlog

from app.core.config import get_settings


def setup_logging() -> None:
    """Configure structlog at app startup (idempotent re-configurable).

    The terminal renderer is env-driven: ``LOG_FORMAT=json`` selects the
    JSON renderer (one object per line, for staging/prod log pipelines);
    anything else uses the human-readable console renderer (dev default).
    The processor chain is otherwise identical, so the event fields are the
    same either way.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """A bound logger for a module (``name`` becomes the stdlib logger name)."""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
