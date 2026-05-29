"""Phase 5 Step 4 / R-P3-7 — lazy OTel TracerProvider bootstrap.

Both the Dagster daemon and the Hatchet AI worker call
``georag_dagster.parsers.pdf_report.parse_pdf_report``. This module gives
them a shared, idempotent way to wire up an OTLP exporter when the
``OTEL_EXPORTER_OTLP_ENDPOINT`` env var is set, and a no-op tracer when
it isn't — so emitting spans never crashes a parse just because the
collector isn't reachable.

Bootstrap is opt-in: callers invoke :func:`install_tracer_provider`
once at process start (e.g. from a worker's main()). After that,
:func:`get_tracer` returns a real tracer that exports to the OTLP
endpoint. With no bootstrap, ``get_tracer`` still works but spans land
on opentelemetry's default no-op provider (visible only via Python's
opentelemetry-api with no exporter).

Env contract:
  OTEL_EXPORTER_OTLP_ENDPOINT  — e.g. http://otel-collector:4318
  OTEL_SERVICE_NAME            — e.g. dagster-daemon, hatchet-worker-ai
  OTEL_EXPORTER_OTLP_PROTOCOL  — "http/protobuf" (default) | "grpc"

Idempotent: re-calling install_tracer_provider() is a no-op once a real
provider is installed, so it's safe to invoke from multiple entry points.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def install_tracer_provider(default_service_name: str | None = None) -> bool:
    """Install a global TracerProvider that exports via OTLP. Returns
    True if a real provider was installed (or already had been), False
    if instrumentation is unavailable / disabled.
    """
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return True

        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        if not endpoint:
            return False

        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError as exc:
            logger.warning("otel: SDK not installed, span export disabled (%s)", exc)
            return False

        protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf").strip()
        try:
            if protocol == "grpc":
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )
            else:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
        except ImportError as exc:
            logger.warning("otel: %s exporter not installed (%s)", protocol, exc)
            return False

        service_name = (
            os.environ.get("OTEL_SERVICE_NAME", "").strip()
            or default_service_name
            or "georag-worker"
        )
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _INSTALLED = True
        logger.info(
            "otel: tracer provider installed service=%s endpoint=%s protocol=%s",
            service_name, endpoint, protocol,
        )
        return True


def get_tracer(instrumenting_name: str, version: str | None = None):
    """Return an OTel tracer, falling back to the no-op tracer if the
    opentelemetry-api package itself isn't installed."""
    try:
        from opentelemetry import trace
    except ImportError:
        return _NullTracer()
    return trace.get_tracer(instrumenting_name, version or "")


class _NullSpan:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def set_attribute(self, *args, **kwargs):
        return None

    def record_exception(self, *args, **kwargs):
        return None


class _NullTracer:
    def start_as_current_span(self, *args, **kwargs):
        return _NullSpan()


__all__ = ["install_tracer_provider", "get_tracer"]
