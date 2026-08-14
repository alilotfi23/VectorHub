"""Structured-logging behaviors: env-driven JSON renderer and the per-request
access log line. Both are pinned deterministically — the renderer test emits
through a captured stdlib handler, the access log through a stub logger."""

import json
import logging
from typing import Any

import pytest
import structlog

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.middleware import metrics as metrics_module
from app.middleware.metrics import _record_request


class _CaptureHandler(logging.Handler):
    def __init__(self, captured: list[str]) -> None:
        super().__init__()
        self._captured = captured

    def emit(self, record: logging.LogRecord) -> None:
        self._captured.append(record.getMessage())


def _emit_through_stdlib(logger_name: str, event: str, **kwargs: Any) -> list[str]:
    """Emit one structlog line and return the rendered record messages."""
    captured: list[str] = []
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    handler = _CaptureHandler(captured)
    logger.addHandler(handler)
    try:
        structlog.get_logger(logger_name).info(event, **kwargs)
    finally:
        logger.removeHandler(handler)
    return captured


def test_json_renderer_emits_parseable_json_lines() -> None:
    """LOG_FORMAT=json renders one JSON object per line with the event and
    its fields; the processor chain stays identical to the console path."""
    settings = get_settings()
    original = settings.log_format
    try:
        settings.log_format = "json"
        setup_logging()

        lines = _emit_through_stdlib("test-json-renderer", "hello", foo="bar", n=1)
        assert len(lines) == 1
        line = json.loads(lines[0])
        assert line["event"] == "hello"
        assert line["foo"] == "bar"
        assert line["n"] == 1
        assert "level" in line
        assert "timestamp" in line
    finally:
        settings.log_format = original
        setup_logging()  # restore the console renderer for the rest of the process


def test_console_renderer_is_default() -> None:
    """The default renderer is the human-readable console renderer."""
    settings = get_settings()
    original = settings.log_format
    try:
        settings.log_format = "console"
        setup_logging()
        processors = structlog.get_config()["processors"]
        assert isinstance(processors[-1], structlog.dev.ConsoleRenderer)
    finally:
        settings.log_format = original
        setup_logging()


class _StubLogger:
    """Records every call as (level, event, kwargs) for deterministic asserts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.calls.append(("info", event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self.calls.append(("warning", event, kwargs))


def test_request_access_log_emits_method_path_status_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every request records one INFO line naming method, templated path,
    status, and duration_ms (with slow=false) — through the middleware's
    _record_request."""
    stub = _StubLogger()
    monkeypatch.setattr(metrics_module, "logger", stub)
    _record_request(
        "GET", "/api/v1/collections/{name}", 200, duration_ms=25, slow_threshold_ms=1000
    )

    assert len(stub.calls) == 1
    level, event, fields = stub.calls[0]
    assert level == "info"
    assert event == "request_completed"
    assert fields == {
        "method": "GET",
        "path": "/api/v1/collections/{name}",
        "status": 200,
        "duration_ms": 25,
        "slow": False,
    }


def test_slow_request_logs_warning_with_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request at or above the threshold logs the same event at WARNING with
    slow=true, so latency outliers surface in the log stream."""
    stub = _StubLogger()
    monkeypatch.setattr(metrics_module, "logger", stub)
    _record_request(
        "POST", "/api/v1/collections/{name}/query", 200, duration_ms=1500, slow_threshold_ms=1000
    )

    assert len(stub.calls) == 1
    level, event, fields = stub.calls[0]
    assert level == "warning"
    assert event == "request_completed"
    assert fields == {
        "method": "POST",
        "path": "/api/v1/collections/{name}/query",
        "status": 200,
        "duration_ms": 1500,
        "slow": True,
    }


def test_request_id_flows_into_json_lines_via_contextvars() -> None:
    """With LOG_FORMAT=json and the tracing middleware's contextvars bound
    (request_id + trace_id), every line emitted during the request — the
    rate-limit 429 line and the access-log line included — carries the same
    request ID. That is the "traceable in one place" contract: grep the JSON
    stream for one request_id and every line of that request surfaces
    together.

    A fresh logger name is used deliberately: structlog's
    cache_logger_on_first_use freezes a module logger's processor chain at
    first use, so reconfiguring the renderer mid-process does not touch
    already-cached module loggers. The chain itself is what matters here
    (merge_contextvars -> JSONRenderer), and the module-level event shapes
    are pinned by the stub tests.
    """
    settings = get_settings()
    original = settings.log_format
    try:
        settings.log_format = "json"
        setup_logging()
        structlog.contextvars.bind_contextvars(request_id="req-abc123", trace_id="a" * 32)
        try:
            lines = _emit_through_stdlib(
                "test-correlation", "request_completed", method="POST", path="/x", status=429
            )
        finally:
            structlog.contextvars.clear_contextvars()
    finally:
        settings.log_format = original
        setup_logging()

    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["event"] == "request_completed"
    assert data["method"] == "POST"
    assert data["status"] == 429
    assert data["request_id"] == "req-abc123"
    assert data["trace_id"] == "a" * 32
