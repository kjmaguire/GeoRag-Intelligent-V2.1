"""Phase 2 of the reliability spec — internal route that runs the
per-completion materialised-view refresh on behalf of Laravel's
DebounceWorkspaceMvRefresh job.

The Laravel job dispatches with a 30 s delay + Redis lock so bursts of
ingestion completions coalesce into one refresh per workspace per
window. When the delayed job fires, it POSTs here; this endpoint runs
the actual refresh under per-view advisory locks (so concurrent
workspaces don't trample each other) and logs every attempt to
gold.mv_refresh_log.

Auth: X-Service-Key, same as /internal/v1/shadow.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.config import settings
from app.hatchet_workflows import _progress as ingest_progress
from app.services.mv_refresh import refresh_views_with_advisory_lock

log = logging.getLogger("georag.mv_refresh_trigger")

router = APIRouter(prefix="/internal/v1/mv-refresh", tags=["mv_refresh"])


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


class MvRefreshRunInput(BaseModel):
    workspace_id: Optional[str] = Field(
        default=None,
        description="UUID. Scopes the gold.mv_refresh_log row + dependency "
                    "staleness check. NULL = global refresh (nightly cron).",
    )
    triggered_by: str = Field(
        default="ingestion",
        description="One of: ingestion, nightly_integrity, manual.",
    )
    force: bool = Field(
        default=False,
        description="Bypass the staleness check and refresh unconditionally.",
    )


class MvRefreshViewResult(BaseModel):
    view_name: str
    status: str
    duration_ms: int
    rows_before: Optional[int] = None
    rows_after: Optional[int] = None
    error: Optional[str] = None


class MvRefreshRunOutput(BaseModel):
    workspace_id: Optional[str]
    triggered_by: str
    results: list[MvRefreshViewResult]


@router.post(
    "/run",
    response_model=MvRefreshRunOutput,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(_check_service_key)],
)
async def run(payload: MvRefreshRunInput) -> MvRefreshRunOutput:
    """Refresh every registered MV, gated by per-view advisory locks.

    Skipped views (lock not acquired OR no dependency changes) are
    included in the response so the caller can tell which views were
    checked.
    """
    log.info(
        "mv_refresh.run start workspace_id=%s triggered_by=%s force=%s",
        payload.workspace_id, payload.triggered_by, payload.force,
    )

    pool = await ingest_progress.get_pool()
    results = await refresh_views_with_advisory_lock(
        pool=pool,
        workspace_id=payload.workspace_id,
        triggered_by=payload.triggered_by,
        force=payload.force,
    )

    return MvRefreshRunOutput(
        workspace_id=payload.workspace_id,
        triggered_by=payload.triggered_by,
        results=[
            MvRefreshViewResult(
                view_name=r.view_name,
                status=r.status,
                duration_ms=r.duration_ms,
                rows_before=r.rows_before,
                rows_after=r.rows_after,
                error=r.error,
            )
            for r in results
        ],
    )


__all__ = ["router"]
