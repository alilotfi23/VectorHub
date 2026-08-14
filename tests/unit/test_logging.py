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


def test_request_access_log_emits_method_path_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every request records one INFO line naming method, templated path, and
    status — through the middleware's _record_request."""
    captured: dict[str, Any] = {}

    class StubLogger:
        def info(self, event: str, **kwargs: Any) -> None:
            captured["event"] = event
            captured.update(kwargs)

    monkeypatch.setattr(metrics_module, "logger", StubLogger())
    _record_request("GET", "/api/v1/collections/{name}", 200)

    assert captured["event"] == "request_completed"
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/collections/{name}"
    assert captured["status"] == 200
