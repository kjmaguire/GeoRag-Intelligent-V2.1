"""Operator trigger for the ``public_geo_sync`` Hatchet workflow.

Before this existed the only way to refresh ``public_geo.*`` outside the
Sunday cron was an ``aws ecs run-task`` from CloudShell. Laravel has no
Hatchet client, FastAPI does — so, same thin pass-through shape as
``shadow_trigger``: Laravel POSTs the optional narrowing here, this hands it
to the SDK's ``aio_run_no_wait()`` and returns the workflow run id without
waiting for the (multi-hour) sync.

Callers: the admin-only "Sync now" button on the Public Geo page
(``PublicGeoscienceSyncController``) and ``php artisan public-geo:sync``,
both through Laravel's ``PublicGeoSyncTrigger`` service.

Auth: ``X-Service-Key`` via the shared ``verify_service_key`` dependency.
The admin decision is Laravel's (the ``admin`` gate); this route trusts any
caller holding the service key, like every other ``/internal`` route.

Double-trigger: the workflow is declared with a one-run concurrency group and
``CANCEL_NEWEST``, so a second dispatch while one is in flight is cancelled
by Hatchet itself. This endpoint therefore always dispatches and returns the
new run id; Laravel additionally rate-limits the button.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.hatchet_workflows.public_geo_sync import PublicGeoSyncInput, public_geo_sync
from app.services.auth import verify_service_key
from app.services.public_geo.registry import JURISDICTIONS, sources_for
from app.services.public_geo.sync import SPECS

log = logging.getLogger("georag.public_geo_trigger")

router = APIRouter(prefix="/internal/v1/public-geo", tags=["public_geo"])


class PublicGeoSyncTriggerRequest(BaseModel):
    jurisdiction_codes: list[str] | None = Field(
        default=None,
        description="Restrict to these jurisdictions (CA-BC, CA-SK, …). Empty = all.",
        max_length=20,
    )
    max_features_per_source: int | None = Field(default=None, ge=1, le=1_000_000)
    requested_by: str | None = Field(
        default=None,
        max_length=255,
        description="Who asked — logged for the operator trail, not trusted for auth.",
    )

    @field_validator("jurisdiction_codes")
    @classmethod
    def _known_jurisdictions(cls, v: list[str] | None) -> list[str] | None:
        if not v:
            return None
        unknown = sorted({c for c in v if c not in JURISDICTIONS})
        if unknown:
            raise ValueError(f"unknown jurisdiction code(s): {', '.join(unknown)}")
        return sorted(set(v))


class PublicGeoSyncTriggerResponse(BaseModel):
    workflow_run_id: str
    workflow: str = "public_geo_sync"
    jurisdiction_codes: list[str] | None
    feeds: int


@router.post(
    "/sync/trigger",
    response_model=PublicGeoSyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_service_key)],
)
async def trigger_public_geo_sync(
    payload: PublicGeoSyncTriggerRequest,
) -> PublicGeoSyncTriggerResponse:
    """Enqueue ``public_geo_sync`` and return its run id (202, does not wait)."""
    feeds = sources_for(
        canonical_types=list(SPECS), jurisdiction_codes=payload.jurisdiction_codes
    )
    if not feeds:
        # Not an upstream fault — the filter names a jurisdiction with no
        # registered feeds (e.g. CA-AB). Refuse rather than enqueue a run that
        # can only end FAILED.
        raise HTTPException(
            status_code=422,
            detail=f"no public-geo feeds are registered for {payload.jurisdiction_codes}",
        )

    ref = await public_geo_sync.aio_run_no_wait(
        PublicGeoSyncInput(
            jurisdiction_codes=payload.jurisdiction_codes,
            max_features_per_source=payload.max_features_per_source,
        )
    )
    log.info(
        "public_geo_sync dispatched: run=%s jurisdictions=%s feeds=%d requested_by=%s",
        ref.workflow_run_id, payload.jurisdiction_codes or "all", len(feeds),
        payload.requested_by,
    )
    return PublicGeoSyncTriggerResponse(
        workflow_run_id=ref.workflow_run_id,
        jurisdiction_codes=payload.jurisdiction_codes,
        feeds=len(feeds),
    )
