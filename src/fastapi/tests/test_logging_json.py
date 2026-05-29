"""V1.5-05 — JSON log formatter tests.

Asserts the JsonFormatter emits one valid JSON document per record,
preserves `extra={...}` keys at the top level, and skips internal
LogRecord attributes that would clutter the payload.
"""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

from app.logging_config import JsonFormatter


@pytest.fixture
def captured_handler() -> tuple[logging.Logger, StringIO]:
    """Build a fresh logger that writes JSON to an in-memory buffer."""
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test_logging_json")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, buf


def test_single_line_per_record(captured_handler):
    logger, buf = captured_handler
    logger.info("hello world")
    output = buf.getvalue()
    # One line, valid JSON.
    assert output.count("\n") == 1
    payload = json.loads(output)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test_logging_json"


def test_extra_fields_surface_at_top_level(captured_handler):
    logger, buf = captured_handler
    logger.info(
        "request",
        extra={
            "request_id": "abc-123",
            "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
            "trace_id": "0123456789abcdef0123456789abcdef",
            "duration_ms": 42.5,
        },
    )
    payload = json.loads(buf.getvalue())
    assert payload["request_id"] == "abc-123"
    assert payload["traceparent"].startswith("00-")
    assert payload["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert payload["duration_ms"] == 42.5


def test_reserved_keys_not_leaked(captured_handler):
    """Internal LogRecord attributes shouldn't clutter the payload.

    Python itself rejects `extra={"args": ...}` with KeyError at the
    logging API level — we never even reach the formatter — so this test
    only asserts the *normal* LogRecord internals don't leak.
    """
    logger, buf = captured_handler
    logger.info("simple message")
    payload = json.loads(buf.getvalue())
    # We map levelname → level and msg → message; raw names should be absent.
    assert "msg" not in payload
    assert "levelname" not in payload
    assert "args" not in payload
    assert "filename" not in payload
    assert "pathname" not in payload
    assert "process" not in payload
    assert "thread" not in payload


def test_exception_traceback_included(captured_handler):
    logger, buf = captured_handler
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("caught")
    payload = json.loads(buf.getvalue())
    assert payload["level"] == "ERROR"
    assert "traceback" in payload
    assert "ValueError: boom" in payload["traceback"]


def test_timestamp_is_iso8601_utc(captured_handler):
    logger, buf = captured_handler
    logger.info("now")
    payload = json.loads(buf.getvalue())
    # 2026-04-22T19:03:47.123456+00:00 shape.
    assert payload["timestamp"].endswith("+00:00")
    # Must round-trip parse.
    from datetime import datetime
    datetime.fromisoformat(payload["timestamp"])


def test_non_serialisable_extras_use_repr_fallback(captured_handler):
    """Datetime / Path / etc. shouldn't crash the formatter."""
    logger, buf = captured_handler
    from pathlib import Path
    logger.info("file", extra={"path": Path("/tmp/foo")})
    payload = json.loads(buf.getvalue())
    assert "/tmp/foo" in payload["path"]
