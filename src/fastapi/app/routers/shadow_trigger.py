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
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.db import bind_workspace_scope
from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows.ingest_geophysics import (
    IngestGeophysicsInput,
    ingest_geophysics,
)
from app.hatchet_workflows.ingest_pdf import IngestPdfInput, ingest_pdf
from app.hatchet_workflows.ingest_spatial import (
    IngestSpatialInput,
    ingest_spatial,
)
from app.hatchet_workflows.ingest_tabular import (
    IngestTabularInput,
    ingest_tabular,
)
from app.hatchet_workflows.ingest_well_logs import (
    IngestWellLogsInput,
    ingest_well_logs,
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
from app.services.auth import service_key_matches

log = logging.getLogger("georag.shadow_trigger")

router = APIRouter(prefix="/internal/v1/shadow", tags=["shadow"])


def _check_service_key(x_service_key: str | None = Header(default=None)) -> None:
    # Same policy as app.services.auth.verify_service_key (constant-time,
    # previous key accepted during a rotation); kept as a local dependency
    # only because the header is optional here so a missing one is a 401
    # rather than FastAPI's 422.
    expected = settings.FASTAPI_SERVICE_KEY
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="FASTAPI_SERVICE_KEY not configured",
        )
    if not service_key_matches(x_service_key):
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

    # F4 (2026-08-11) / HAT-6+12 (2026-09-29) — dedupe against an in-flight
    # run for the same file, with the progress row written BEFORE dispatch.
    # Laravel's bridge wraps this call in retry(3, 500): a first dispatch
    # that succeeded but responded slowly gets re-POSTed. The old order
    # (dispatch, then insert the row) let that retry find no row and dispatch
    # again, and let preflight race the endpoint and mint a second row. See
    # _claim_and_dispatch.
    outcome = await _claim_and_dispatch(
        ingest_pdf, payload, site="ingest_pdf", request=request,
    )
    if not outcome.dispatched:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=TriggerIngestPdfResponse(
                workflow_run_id=outcome.workflow_run_id,
                correlation_token=payload.correlation_token,
                dispatched=False,
            ).model_dump(),
        )

    return TriggerIngestPdfResponse(
        workflow_run_id=outcome.workflow_run_id,
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

    # F6 (2026-08-11) — the progress row for the SOURCE tiff key exists from
    # dispatch time (status='queued'), so a saturation-cancelled or crashed
    # normalize run is visible in the IngestionRuns UI instead of vanishing.
    # tiff_normalize's on_failure hook and the normalize task's own terminal
    # writes resolve this row; the derived PDF gets its own row from
    # ingest_pdf preflight. HAT-6 (2026-09-29): written before dispatch and
    # deduped, like every trigger here.
    outcome = await _claim_and_dispatch(
        tiff_normalize, payload, site="tiff_normalize", request=request,
    )
    if not outcome.dispatched:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=TriggerIngestPdfResponse(
                workflow_run_id=outcome.workflow_run_id,
                correlation_token=payload.correlation_token,
                dispatched=False,
            ).model_dump(),
        )

    return TriggerIngestPdfResponse(
        workflow_run_id=outcome.workflow_run_id,
        correlation_token=payload.correlation_token,
    )


class TriggerZipArchiveResponse(BaseModel):
    workflow_run_id: str
    run_id: str
    #: False when the endpoint deduped against a run already recorded for
    #: this archive instead of dispatching (HAT-6).
    dispatched: bool = True


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

    outcome = await _claim_and_dispatch(
        ingest_zip_archive, payload, site="ingest_zip_archive", request=request,
    )
    return _respond(
        TriggerZipArchiveResponse(
            workflow_run_id=outcome.workflow_run_id,
            run_id=outcome.run_id or payload.run_id,
            dispatched=outcome.dispatched,
        ),
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
    dispatched: bool = True


class TriggerIngestTabularResponse(BaseModel):
    workflow_run_id: str
    run_id: str | None
    dispatched: bool = True


@dataclass(frozen=True)
class _DispatchOutcome:
    workflow_run_id: str
    run_id: str | None
    dispatched: bool


def _respond(body: BaseModel) -> Any:
    """202 for a fresh dispatch, 200 for a dedupe hit (Laravel accepts both)."""
    if getattr(body, "dispatched", True):
        return body
    return JSONResponse(status_code=status.HTTP_200_OK, content=body.model_dump())


#: Lets an internal sweep record the progress row as its own (triggered_by)
#: rather than as an upload. Validated against _progress.ALLOWED_TRIGGERS.
INGEST_TRIGGER_HEADER = "X-Ingest-Trigger"


def _triggered_by(request: Request | None) -> str:
    headers = getattr(request, "headers", None)
    value = headers.get(INGEST_TRIGGER_HEADER) if headers is not None else None
    return value if value in ingest_progress.ALLOWED_TRIGGERS else "upload"


async def _claim_and_dispatch(
    workflow: Any,
    payload: Any,
    *,
    site: str,
    request: Request | None = None,
) -> _DispatchOutcome:
    """Record the queued progress row, then dispatch, deduping retries.

    HAT-4/6/12 (2026-09-29). Every trigger used to dispatch first and write
    its ingest_progress row second, and only ingest_pdf deduped at all.
    Laravel wraps each call in ``timeout(15)->retry(3, 500)``, so a slow
    ``aio_run_no_wait`` (hatchet-lite's queue is Postgres-backed, so queue
    pressure is DB pressure) got re-POSTed with the same payload:

    * geology: two ingest_tabular runs for one file each DELETE-then-INSERT
      in their own READ COMMITTED transaction, and both inserts survive,
      which doubles the lithology/sample/assay rows. A double ZIP fans the
      whole archive out twice;
    * ingest_pdf: the retry arrived while the first request was still inside
      the slow dispatch, before the row existed, so it dispatched again.
      Preflight could also beat the endpoint's INSERT and mint its own row.

    Now :func:`_progress.claim_dispatch` takes a per-file advisory lock,
    returns any non-terminal run for (workspace, key) as a duplicate, and
    otherwise inserts the queued row, under the caller's run_id when it sent
    one. A caller run_id that is already recorded is a duplicate even after
    that run finished. Only a claimed row is dispatched. Its Hatchet id is
    stamped afterwards, and a dispatch that raises takes its row back out so
    Laravel's own retry of the failed request is not mistaken for a
    duplicate.

    The row still exists from dispatch time, which keeps the Cameco
    guarantee: a run Hatchet cancels before any task body runs still leaves
    a row for on_failure to close and for the sweeps to see
    ([[cameco-recovery-2026-06-02]]).

    Fails open on a database error in the claim (dispatch without dedupe,
    row written afterwards, the pre-2026-09-29 behaviour). Refusing uploads
    because the progress table is unreachable would be worse than the rare
    double.
    """
    workspace_id = str(payload.workspace_id) if payload.workspace_id else ""
    project_id = str(payload.project_id) if payload.project_id else ""
    caller_run_id = getattr(payload, "run_id", None)
    triggered_by = _triggered_by(request)

    if not (workspace_id and project_id):
        ref = await workflow.aio_run_no_wait(payload)
        return _DispatchOutcome(ref.workflow_run_id, caller_run_id, True)

    try:
        claim = await ingest_progress.claim_dispatch(
            workspace_id=workspace_id,
            project_id=project_id,
            minio_key=payload.minio_key,
            run_id=caller_run_id,
            triggered_by=triggered_by,
        )
    except Exception as exc:
        log.warning(
            "trigger_%s: dispatch claim failed (%s) — dispatching without dedupe",
            site, exc,
        )
        ref = await workflow.aio_run_no_wait(payload)
        await ingest_progress.start_run(
            workspace_id=workspace_id,
            project_id=project_id,
            minio_key=payload.minio_key,
            triggered_by=triggered_by,
            workflow_run_id=ref.workflow_run_id,
            run_id=caller_run_id,
        )
        return _DispatchOutcome(ref.workflow_run_id, caller_run_id, True)

    if not claim.claimed:
        log.info(
            "trigger_%s: dedupe hit run=%s workflow=%s key=%s — returning the "
            "existing run, not re-dispatching",
            site, claim.run_id, claim.workflow_run_id, payload.minio_key,
        )
        return _DispatchOutcome(
            claim.workflow_run_id or claim.run_id, claim.run_id, False,
        )

    try:
        ref = await workflow.aio_run_no_wait(payload)
    except BaseException:
        await ingest_progress.release_undispatched(run_id=claim.run_id)
        raise
    await ingest_progress.stamp_workflow_run_id(
        run_id=claim.run_id, workflow_run_id=ref.workflow_run_id,
    )
    return _DispatchOutcome(ref.workflow_run_id, claim.run_id, True)


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

    outcome = await _claim_and_dispatch(
        ingest_spatial, payload, site="ingest_spatial", request=request,
    )
    return _respond(
        TriggerIngestSpatialResponse(
            workflow_run_id=outcome.workflow_run_id,
            run_id=outcome.run_id or payload.run_id,
            dispatched=outcome.dispatched,
        ),
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

    outcome = await _claim_and_dispatch(
        ingest_tabular, payload, site="ingest_tabular", request=request,
    )
    return _respond(
        TriggerIngestTabularResponse(
            workflow_run_id=outcome.workflow_run_id,
            run_id=outcome.run_id or payload.run_id,
            dispatched=outcome.dispatched,
        ),
    )


class TriggerIngestWellLogsResponse(BaseModel):
    workflow_run_id: str
    run_id: str | None
    dispatched: bool = True


@router.post(
    "/ingest_well_logs/trigger",
    response_model=TriggerIngestWellLogsResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_check_service_key)],
)
async def trigger_ingest_well_logs(
    payload: IngestWellLogsInput,
    request: Request,
) -> TriggerIngestWellLogsResponse:
    """Trigger ingest_well_logs — LAS downhole curves.

    Writes silver.well_log_curves, one row per curve with depth/value arrays.
    Curves attach to a collar; a LAS file whose hole has no collar completes
    with `orphaned` set rather than failing.
    """
    log.info(
        "trigger_ingest_well_logs: workspace_id=%s project_id=%s key=%s hole_id=%s",
        payload.workspace_id, payload.project_id, payload.minio_key,
        payload.hole_id,
    )
    await _guard_active_project(request, payload)

    outcome = await _claim_and_dispatch(
        ingest_well_logs, payload, site="ingest_well_logs", request=request,
    )
    return _respond(
        TriggerIngestWellLogsResponse(
            workflow_run_id=outcome.workflow_run_id,
            run_id=outcome.run_id or payload.run_id,
            dispatched=outcome.dispatched,
        ),
    )


class TriggerIngestGeophysicsResponse(BaseModel):
    workflow_run_id: str
    run_id: str | None
    dispatched: bool = True


@router.post(
    "/ingest_geophysics/trigger",
    response_model=TriggerIngestGeophysicsResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_check_service_key)],
)
async def trigger_ingest_geophysics(
    payload: IngestGeophysicsInput,
    request: Request,
) -> TriggerIngestGeophysicsResponse:
    """Trigger ingest_geophysics — Geosoft XYZ line data and DCIP2D exports.

    Writes silver.geophysics_surveys plus its lines/channels (XYZ) or its
    DC/IP readings and inversion models (DCIP2D). A re-upload of the same
    file replaces its survey in place (ING-19).
    """
    log.info(
        "trigger_ingest_geophysics: workspace_id=%s project_id=%s key=%s",
        payload.workspace_id, payload.project_id, payload.minio_key,
    )
    await _guard_active_project(request, payload)

    outcome = await _claim_and_dispatch(
        ingest_geophysics, payload, site="ingest_geophysics", request=request,
    )
    return _respond(
        TriggerIngestGeophysicsResponse(
            workflow_run_id=outcome.workflow_run_id,
            run_id=outcome.run_id or payload.run_id,
            dispatched=outcome.dispatched,
        ),
    )
