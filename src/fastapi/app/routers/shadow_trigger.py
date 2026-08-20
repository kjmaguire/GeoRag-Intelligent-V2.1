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
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.db import bind_workspace_scope
from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows.ingest_pdf import IngestPdfInput, ingest_pdf
from app.hatchet_workflows.ingest_spatial import (
    IngestSpatialInput,
    ingest_spatial,
)
from app.hatchet_workflows.ingest_tabular import (
    IngestTabularInput,
    ingest_tabular,
)
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
    # F4 (2026-08-11) — False when the endpoint deduped against an
    # existing non-terminal run instead of dispatching a new workflow.
    dispatched: bool = True


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
    # Goes through bind_workspace_scope rather than a hand-rolled
    # set_config: same parameter-bound SET LOCAL form, but it also
    # rejects a non-UUID workspace_id up front — which is exactly the
    # gap audit pass 5+ flagged on the zip-archive sibling below.
    if payload.project_id:
        _pg_pool = request.app.state.pg_pool
        async with _pg_pool.acquire() as _conn:
            async with _conn.transaction():
                if payload.workspace_id:
                    await bind_workspace_scope(
                        _conn,
                        workspace_id=str(payload.workspace_id),
                        site="routers.shadow_trigger.ingest_pdf",
                    )
                await require_active_project(
                    project_id=str(payload.project_id), conn=_conn
                )

    # F4 (2026-08-11) — dedupe against an in-flight run for the same file.
    # Laravel's bridge wraps this call in retry(3, 500): a first dispatch
    # that succeeded but responded slowly gets re-POSTed, and nothing ever
    # read correlation_token for dedupe, so bulk imports double-ingested.
    # A non-terminal ingest_progress row for (workspace, key) means a run
    # is already queued/started — return its identifiers with 200 instead
    # of dispatching again. Fails open: a lookup error just dispatches.
    if payload.workspace_id:
        try:
            _pool = await ingest_progress.get_pool()
            async with _pool.acquire() as _c:
                _existing = await _c.fetchrow(
                    "SELECT run_id::text AS run_id, workflow_run_id "
                    "FROM silver.ingest_progress "
                    "WHERE workspace_id = $1::uuid AND minio_key = $2 "
                    "  AND status NOT IN ('completed','failed','cancelled','timed_out') "
                    "LIMIT 1",
                    str(payload.workspace_id), payload.minio_key,
                )
        except Exception as _dedupe_exc:
            log.warning(
                "trigger_ingest_pdf: dedupe lookup failed (%s) — dispatching anyway",
                _dedupe_exc,
            )
            _existing = None
        if _existing is not None:
            log.info(
                "trigger_ingest_pdf: dedupe hit run=%s workflow=%s key=%s — "
                "returning existing run, not re-dispatching",
                _existing["run_id"], _existing["workflow_run_id"], payload.minio_key,
            )
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=TriggerIngestPdfResponse(
                    workflow_run_id=_existing["workflow_run_id"] or _existing["run_id"],
                    correlation_token=payload.correlation_token,
                    dispatched=False,
                ).model_dump(),
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
    # Scoped via bind_workspace_scope; see ingest_pdf trigger above.
    if payload.project_id:
        _pg_pool = request.app.state.pg_pool
        async with _pg_pool.acquire() as _conn:
            async with _conn.transaction():
                if payload.workspace_id:
                    await bind_workspace_scope(
                        _conn,
                        workspace_id=str(payload.workspace_id),
                        site="routers.shadow_trigger.tiff_normalize",
                    )
                await require_active_project(
                    project_id=str(payload.project_id), conn=_conn
                )

    ref = await tiff_normalize.aio_run_no_wait(payload)

    # F6 (2026-08-11) — mirror the ingest_pdf sibling above: insert the
    # ingest_progress row at dispatch time (status='queued') for the SOURCE
    # tiff key so a saturation-cancelled or crashed normalize run is visible
    # in the IngestionRuns UI instead of vanishing. tiff_normalize's
    # on_failure hook and the normalize task's own terminal writes resolve
    # this row; the derived PDF gets its own row from ingest_pdf preflight.
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
    # Especially load-bearing here because IngestZipArchiveInput.workspace_id
    # is typed `str` (not `UUID`), so Pydantic never validates the shape.
    # bind_workspace_scope closes that gap: it raises BareConnectionError on
    # anything that isn't a UUID, so malformed input from Laravel is refused
    # at the boundary instead of being bound as an opaque GUC value.
    if payload.project_id:
        _pg_pool = request.app.state.pg_pool
        async with _pg_pool.acquire() as _conn:
            async with _conn.transaction():
                if payload.workspace_id:
                    await bind_workspace_scope(
                        _conn,
                        workspace_id=str(payload.workspace_id),
                        site="routers.shadow_trigger.ingest_zip_archive",
                    )
                await require_active_project(
                    project_id=str(payload.project_id), conn=_conn
                )

    ref = await ingest_zip_archive.aio_run_no_wait(payload)
    return TriggerZipArchiveResponse(
        workflow_run_id=ref.workflow_run_id,
        run_id=payload.run_id,
    )


# ---------------------------------------------------------------------------
# Geology data formats — restored 2026-08-20
# ---------------------------------------------------------------------------
# The `spatial`, `excel` and drill-CSV upload categories answered
# 422 retired_pipeline from 2026-07-28 (Dagster removal) until these two
# workflows gave them a live consumer again. Same thin pass-through shape as
# the three above: Laravel has no Hatchet client, FastAPI does.


class TriggerIngestSpatialResponse(BaseModel):
    workflow_run_id: str
    run_id: str | None


class TriggerIngestTabularResponse(BaseModel):
    workflow_run_id: str
    run_id: str | None


async def _guard_active_project(request: Request, payload) -> None:
    """Refuse ingest into a non-active project, with the workspace bound.

    Both inputs type workspace_id/project_id as `str` for downstream
    ergonomics, so bind_workspace_scope is what actually enforces UUID shape —
    it raises on anything malformed rather than binding it as an opaque GUC.
    """
    if not payload.project_id:
        return
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn, conn.transaction():
        if payload.workspace_id:
            await bind_workspace_scope(
                conn,
                workspace_id=str(payload.workspace_id),
                site="routers.shadow_trigger.geology_ingest",
            )
        await require_active_project(project_id=str(payload.project_id), conn=conn)


@router.post(
    "/ingest_spatial/trigger",
    response_model=TriggerIngestSpatialResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_check_service_key)],
)
async def trigger_ingest_spatial(
    payload: IngestSpatialInput,
    request: Request,
) -> TriggerIngestSpatialResponse:
    """Trigger ingest_spatial — SHP / GeoJSON / GPKG / GML / DXF / QGIS.

    Writes silver.spatial_features. A QGIS project whose data was not bundled
    completes successfully with `manifest_only` set rather than failing;
    see the workflow's docstring for why that is not a parse error.
    """
    log.info(
        "trigger_ingest_spatial: workspace_id=%s project_id=%s key=%s",
        payload.workspace_id, payload.project_id, payload.minio_key,
    )
    await _guard_active_project(request, payload)

    ref = await ingest_spatial.aio_run_no_wait(payload)
    return TriggerIngestSpatialResponse(
        workflow_run_id=ref.workflow_run_id,
        run_id=payload.run_id,
    )


@router.post(
    "/ingest_tabular/trigger",
    response_model=TriggerIngestTabularResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_check_service_key)],
)
async def trigger_ingest_tabular(
    payload: IngestTabularInput,
    request: Request,
) -> TriggerIngestTabularResponse:
    """Trigger ingest_tabular — drill CSV and multi-sheet XLSX.

    Writes silver.collars first, then surveys / lithology_logs / samples
    against it. Intervals whose hole has no collar are reported as orphaned,
    not dropped.
    """
    log.info(
        "trigger_ingest_tabular: workspace_id=%s project_id=%s key=%s sheet_type=%s",
        payload.workspace_id, payload.project_id, payload.minio_key,
        payload.sheet_type,
    )
    await _guard_active_project(request, payload)

    ref = await ingest_tabular.aio_run_no_wait(payload)
    return TriggerIngestTabularResponse(
        workflow_run_id=ref.workflow_run_id,
        run_id=payload.run_id,
    )
