"""Phase 6 of the reliability spec — Laravel → FastAPI metric bridge.

Lets the Laravel-side ``DebounceWorkspaceMvRefresh`` job record the
``workspace_data_updated_emission_latency_seconds`` histogram (and any
future Laravel-originated reliability metric) into the FastAPI
Prometheus registry — keeps the metric surface in one place so the
existing ``/metrics`` scrape sees everything.

This endpoint is intentionally narrow:
  - Service-key auth only (mirrors the ingestion-progress broadcast route).
  - Validated metric whitelist — only metrics declared in app.metrics
    can be recorded, with the right cardinality.
  - Best-effort observability: never raises, just logs + returns 200.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.config import settings

log = logging.getLogger("georag.metrics_ingestion_events")

router = APIRouter(prefix="/internal/v1/metrics", tags=["metrics_ingestion"])


_METRIC_WHITELIST = {
    "workspace_data_updated_emission_latency_seconds",
}


def _check_service_key(x_service_key: str | None = Header(default=None)) -> None:
    expected = settings.FASTAPI_SERVICE_KEY
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="FASTAPI_SERVICE_KEY not configured",
        )
    if x_service_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid X-Service-Key",
        )


class IngestionMetricEvent(BaseModel):
    metric: str = Field(..., description="Whitelisted metric name")
    value: float = Field(..., description="Observed value (seconds, count, etc.)")


@router.post(
    "/ingestion-event",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(_check_service_key)],
)
async def record(payload: IngestionMetricEvent) -> dict:
    if payload.metric not in _METRIC_WHITELIST:
        log.warning("metrics.ingestion-event: rejected unknown metric=%s", payload.metric)
        return {"ok": False, "reason": "metric_not_whitelisted"}

    try:
        from app.metrics import WORKSPACE_DATA_UPDATED_EMISSION_LATENCY
        if payload.metric == "workspace_data_updated_emission_latency_seconds":
            WORKSPACE_DATA_UPDATED_EMISSION_LATENCY.observe(max(0.0, payload.value))
    except Exception as exc:
        log.warning("metrics.ingestion-event: observe failed metric=%s err=%s",
                    payload.metric, exc)
        return {"ok": False, "reason": "observe_failed"}

    return {"ok": True}


__all__ = ["router"]
