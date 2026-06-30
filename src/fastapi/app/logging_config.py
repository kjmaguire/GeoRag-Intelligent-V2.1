"""V1.5-05 — JSON log formatter for FastAPI.

Replaces uvicorn's default text formatter so Loki / Promtail can
ingest one JSON document per line. Pairs with `JsonFormatter` on the
Laravel side (V1.5-04) — both services emit the same shape.

Why hand-rolled
---------------
python-json-logger is a fine library but the formatter we need is
~40 lines. Avoiding the transitive dep keeps the FastAPI image lean
and keeps us aligned with Module 9's "no new deps unless really
needed" stance.

The formatter
-------------
For every LogRecord:
  * `timestamp`     ISO 8601 UTC with millisecond precision
  * `level`         INFO / WARNING / ERROR / etc.
  * `logger`        logger name
  * `message`       the rendered message
  * `module`, `func`, `line`  source location
  * `traceback`     stack trace text when `exc_info` is set
  * any keyword from `extra={...}` is added at the top level

The traceparent / trace_id / request_id fields populated by
`StructuredAccessLogMiddleware` flow through unchanged because they're
attached via `extra=`.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Reserved LogRecord attributes that we never want to leak into the
# JSON payload (they're already represented elsewhere or are noisy).
_RESERVED: frozenset[str] = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
})


class JsonFormatter(logging.Formatter):
    """Emits one JSON document per record.

    Octane-safe analog: the formatter holds no per-instance state; each
    call produces a fresh dict from the record.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }

        # Surface any custom fields passed via `logger.info(..., extra={...})`.
        # StructuredAccessLogMiddleware attaches request_id / traceparent /
        # trace_id this way; pulling them up to the top level lets Loki
        # promote them to labels via the json pipeline stage.
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info:
            payload["traceback"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=_safe_default, ensure_ascii=False)


def _safe_default(obj: Any) -> str:
    """Fallback for non-JSON-serialisable extras (datetimes, paths, etc.)."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    return repr(obj)


def get_log_config(level: str = "INFO") -> dict[str, Any]:
    """Return a dictConfig payload uvicorn understands.

    Use as:
        import uvicorn
        from app.logging_config import get_log_config
        uvicorn.run(app, log_config=get_log_config())

    Or pre-apply on import so logger.info() inside our app emits JSON
    even before the uvicorn worker boots:
        from app.logging_config import configure_json_logging
        configure_json_logging()
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {"()": "app.logging_config.JsonFormatter"},
        },
        "handlers": {
            "stdout_json": {
                "class": "logging.StreamHandler",
                "stream": sys.stdout,
                "formatter": "json",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["stdout_json"], "level": level, "propagate": False},
            "uvicorn.error": {"handlers": ["stdout_json"], "level": level, "propagate": False},
            "uvicorn.access": {"handlers": ["stdout_json"], "level": level, "propagate": False},
        },
        "root": {"handlers": ["stdout_json"], "level": level},
    }


def configure_json_logging(level: str = "INFO") -> None:
    """Apply the JSON dictConfig to the live root logger.

    Idempotent — safe to call from main.py at module import.
    """
    import logging.config

    logging.config.dictConfig(get_log_config(level))
