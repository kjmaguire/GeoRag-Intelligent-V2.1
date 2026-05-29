"""Observability helpers shared across Dagster + Hatchet workers.

Currently a thin OTel TracerProvider bootstrap (Phase 5 Step 4 / R-P3-7).
Importable from any module that wants to emit spans without forcing a hard
opentelemetry dep — when OTLP isn't configured, calls collapse to no-ops.
"""

from .otel import get_tracer, install_tracer_provider

__all__ = ["get_tracer", "install_tracer_provider"]
