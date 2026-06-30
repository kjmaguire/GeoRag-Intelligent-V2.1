"""Phase 1 Step 5 — internal route that triggers the ingest_pdf Hatchet
workflow on behalf of Laravel's ShadowRouter.

Laravel's PHP side doesn't have a Hatchet client; FastAPI does. This is a
thin pass-through: Laravel POSTs the IngestPdfInput here, we hand it to
the SDK's `aio_run_no_wait()`, and return the workflow_run_id.

Auth: shares the existing X-Service-Key gate used by other /internal
routes. Both Laravel and FastAPI know `FASTAPI_SERVICE_KEY`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from app.config import settings
from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows.ingest_pdf import IngestPdfInput, ingest_pdf
from app.hatchet_workflows.ingest_zip_archive import (
    IngestZipArchiveInput,
    ingest_zip_archive,
)
from app.hatchet_workflows.tiff_normalize import (
    TiffNormalizeInput,
    tiff_normalize,
)
from app.middleware.project_lifecycle import require_active_project

log = logging.getLogger("georag.shadow_trigger")

router = APIRouter(prefix="/internal/v1/shadow", tags=["shadow"])


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


class TriggerIngestPdfResponse(BaseModel):
    workflow_run_id: str
    correlation_token: str


@router.post(
    "/ingest_pdf/trigger",
    response_model=TriggerIngestPdfResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_check_service_key)],
)
async def trigger_ingest_pdf(
    payload: IngestPdfInput,
    request: Request,
) -> TriggerIngestPdfResponse:
    """Trigger the ingest_pdf Hatchet workflow with the given input.

    Returns 202 Accepted with the workflow_run_id. Caller does NOT wait
    for completion.

    CC-03 Item 8: rejected with 403/402 when the project is not in the
    'active' lifecycle state (hibernated / archived / past_due).

    Historical context: silver.shadow_runs was the v1.49-vs-Hatchet
    diff-pairing table; Phase 4 Step 6 dropped it. The endpoint still
    exists as the Laravel→FastAPI handoff for kicking off Hatchet runs;
    the shadow-runs correlation it used to support is gone.
    """
    log.info(
        "trigger_ingest_pdf: workspace_id=%s correlation=%s key=%s",
        payload.workspace_id, payload.correlation_token, payload.minio_key,
    )

    # CC-03 Item 8 — lifecycle guard. Block ingest on non-active projects.
    # workspace_id GUC set so the RLS policy admits the silver.projects row.
    # Parameter-bound — never f-string interpolate (audit pass 5+ caught the
    # zip-archive sibling using `str` workspace_id without UUID validation,
    # which is the textbook SQL-injection shape).
    if payload.project_id:
        _pg_pool = request.app.state.pg_pool
        async with _pg_pool.acquire() as _conn:
            async with _conn.transaction():
                if payload.workspace_id:
                    await _conn.execute(
                        "SELECT set_config('app.workspace_id', $1, true)",
                        str(payload.workspace_id),
                    )
                await require_active_project(
                    project_id=str(payload.project_id), conn=_conn
                )

    ref = await ingest_pdf.aio_run_no_wait(payload)

    # Cancellation observability — insert the silver.ingest_progress row at
    # dispatch time (status='queued') so queue-saturation CANCELLED events,
    # which fire BEFORE the preflight task runs, still leave a breadcrumb the
    # IngestionRuns UI can render. The on_failure_task hook in ingest_pdf.py
    # already resolves and transitions whatever row it finds via
    # lookup_active_run_id; previously that lookup returned None for ~41% of
    # failures because preflight's mark_started() never fired. See
    # [[cameco-recovery-2026-06-02]] for the diagnosis.
    if payload.workspace_id and payload.project_id:
        await ingest_progress.start_run(
            workspace_id=str(payload.workspace_id),
            project_id=str(payload.project_id),
            minio_key=payload.minio_key,
            triggered_by="upload",
            workflow_run_id=ref.workflow_run_id,
        )

    return TriggerIngestPdfResponse(
        workflow_run_id=ref.workflow_run_id,
        correlation_token=payload.correlation_token,
    )


@router.post(
    "/tiff_normalize/trigger",
    response_model=TriggerIngestPdfResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_check_service_key)],
)
async def trigger_tiff_normalize(
    payload: TiffNormalizeInput,
    request: Request,
) -> TriggerIngestPdfResponse:
    """Trigger the tiff_normalize Hatchet workflow (ADR-0005).

    The workflow streams the TIFF from MinIO, wraps losslessly to a
    derived PDF under ``bronze/reports/...``, then internally triggers
    the existing ``ingest_pdf`` workflow against that derived PDF. The
    returned workflow_run_id is the *normalize* run; the downstream
    ingest_pdf run id is captured in the normalize output.

    CC-03 Item 8: rejected with 403/402 when the project is not in the
    'active' lifecycle state (hibernated / archived / past_due).
    """
    log.info(
        "trigger_tiff_normalize: workspace_id=%s correlation=%s key=%s",
        payload.workspace_id, payload.correlation_token, payload.minio_key,
    )

    # CC-03 Item 8 — lifecycle guard. Block ingest on non-active projects.
    # Parameter-bound; see ingest_pdf trigger above.
    if payload.project_id:
        _pg_pool = request.app.state.pg_pool
        async with _pg_pool.acquire() as _conn:
            async with _conn.transaction():
                if payload.workspace_id:
                    await _conn.execute(
                        "SELECT set_config('app.workspace_id', $1, true)",
                        str(payload.workspace_id),
                    )
                await require_active_project(
                    project_id=str(payload.project_id), conn=_conn
                )

    ref = await tiff_normalize.aio_run_no_wait(payload)
    return TriggerIngestPdfResponse(
        workflow_run_id=ref.workflow_run_id,
        correlation_token=payload.correlation_token,
    )


class TriggerZipArchiveResponse(BaseModel):
    workflow_run_id: str
    run_id: str


@router.post(
    "/ingest_zip_archive/trigger",
    response_model=TriggerZipArchiveResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_check_service_key)],
)
async def trigger_ingest_zip_archive(
    payload: IngestZipArchiveInput,
    request: Request,
) -> TriggerZipArchiveResponse:
    """Trigger the ingest_zip_archive Hatchet workflow.

    The workflow downloads the ZIP from MinIO, extracts all entries, and
    fans each file out to the appropriate ingester (LAS, LOG, TIFF, XLSX,
    PDF). Individual file errors are swallowed so one corrupt file does
    not abort the rest of the archive.

    Returns 202 Accepted with the Hatchet workflow_run_id.
    """
    log.info(
        "trigger_ingest_zip_archive: workspace_id=%s project_id=%s key=%s run_id=%s",
        payload.workspace_id,
        payload.project_id,
        payload.minio_key,
        payload.run_id,
    )

    # Lifecycle guard — block ingest on non-active projects.
    # Parameter-bound; see ingest_pdf trigger above. Especially load-bearing
    # here because IngestZipArchiveInput.workspace_id is typed `str` (not
    # `UUID`) — Pydantic doesn't validate the shape, so an f-string interp
    # would be a textbook SQL-injection vector if Laravel ever forwarded
    # malformed input.
    if payload.project_id:
        _pg_pool = request.app.state.pg_pool
        async with _pg_pool.acquire() as _conn:
            async with _conn.transaction():
                if payload.workspace_id:
                    await _conn.execute(
                        "SELECT set_config('app.workspace_id', $1, true)",
                        str(payload.workspace_id),
                    )
                await require_active_project(
                    project_id=str(payload.project_id), conn=_conn
                )

    ref = await ingest_zip_archive.aio_run_no_wait(payload)
    return TriggerZipArchiveResponse(
        workflow_run_id=ref.workflow_run_id,
        run_id=payload.run_id,
    )
