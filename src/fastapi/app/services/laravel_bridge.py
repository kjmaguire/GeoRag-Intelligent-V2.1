"""FastAPI → Laravel callback bridge.

Symmetric to the Laravel → FastAPI service-key auth: Laravel exposes a
small set of internal endpoints that FastAPI calls back into via the
same shared secret. Today this is used for Reverb-fanned real-time
progress events (§7 Report Builder + future §8 TRG runs); the bridge
keeps the Hatchet workflow body decoupled from broadcast wiring.

Failure semantics
-----------------
- All callbacks are best-effort. We log + swallow on error. A broadcast
  failure must NEVER fail the workflow that's making real progress.
- Timeout is short (3 s) — if Laravel is down, we don't want to stall.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

_DEFAULT_LARAVEL_URL = "http://laravel.test"


def _laravel_base() -> str:
    return os.environ.get("LARAVEL_INTERNAL_URL", _DEFAULT_LARAVEL_URL).rstrip("/")


def _service_key() -> str | None:
    return os.environ.get("FASTAPI_SERVICE_KEY")


async def post_report_build_progress(
    build_id: str,
    stage: str,
    *,
    section_id: str | None = None,
    message: str | None = None,
    sections_completed: int | None = None,
    sections_total: int | None = None,
) -> None:
    """Push a progress event for a §15 report build. Best-effort."""
    key = _service_key()
    if not key:
        log.debug("laravel_bridge: FASTAPI_SERVICE_KEY not set; skipping push")
        return

    url = f"{_laravel_base()}/api/internal/admin/reports/{build_id}/progress"
    payload: dict[str, Any] = {"stage": stage}
    if section_id is not None:
        payload["section_id"] = section_id
    if message is not None:
        payload["message"] = message
    if sections_completed is not None:
        payload["sections_completed"] = sections_completed
    if sections_total is not None:
        payload["sections_total"] = sections_total

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(
                url, json=payload, headers={"X-Service-Key": key, "Accept": "application/json"},
            )
        if r.status_code >= 400:
            log.warning(
                "laravel_bridge: progress post non-2xx build=%s stage=%s status=%s",
                build_id, stage, r.status_code,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "laravel_bridge: progress post failed build=%s stage=%s err=%s",
            build_id, stage, exc,
        )


async def post_ingestion_progress(
    *,
    workspace_id: str,
    project_id: str,
    run_id: str,
    stage: str,
    status: str,
    message: str | None = None,
    pct: int | None = None,
) -> None:
    """Push an ingestion progress event into Laravel for Reverb fan-out.

    Used by:
      - ingest_pdf's on_failure_task hook (status='failed' | 'cancelled')
      - stale_run_detector cron (status='timed_out')
      - ingest_pdf persist task (status='completed', stage='persist')

    The Laravel endpoint validates the X-Service-Key header, then
    broadcasts ``ingestion.progress`` on the ``project.{project_id}``
    Reverb channel so IngestionRuns.tsx can flip the row state
    immediately instead of waiting for its next poll.

    Best-effort: a broadcast failure must not cascade — the durable
    record is the DB row, the broadcast is the latency optimisation.
    """
    key = _service_key()
    if not key:
        log.debug("laravel_bridge: FASTAPI_SERVICE_KEY not set; skipping ingestion broadcast")
        return

    url = f"{_laravel_base()}/api/internal/v1/ingest-progress/broadcast"
    payload: dict[str, Any] = {
        "workspace_id": workspace_id,
        "project_id": project_id,
        "pipeline_run_id": run_id,
        "stage": stage,
        "status": status,
    }
    if message is not None:
        payload["message"] = message
    if pct is not None:
        payload["pct"] = pct

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(
                url,
                json=payload,
                headers={"X-Service-Key": key, "Accept": "application/json"},
            )
        if r.status_code >= 400:
            log.warning(
                "laravel_bridge: ingestion broadcast non-2xx run=%s status=%s http=%s body=%s",
                run_id, status, r.status_code, r.text[:200],
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "laravel_bridge: ingestion broadcast failed run=%s status=%s err=%s",
            run_id, status, exc,
        )


async def post_workspace_data_updated(
    *,
    workspace_id: str,
    project_id: str,
    pipeline_run_id: str,
    affected_types: list[str],
) -> None:
    """Push a WorkspaceDataUpdated event into Laravel for Reverb fan-out.

    Used by non-ingestion workflows whose completion writes project-
    scoped tables the SPA reads directly (no MV refresh in the path):

      - score_targets.execute (on success, affected_types=['targets'])

    Distinct from post_ingestion_progress: ingestion goes through
    /api/internal/v1/ingest-progress/broadcast which does the data_version
    bump + debounced MV refresh + emits WorkspaceDataUpdated from the job
    AFTER refresh confirms. This endpoint emits WorkspaceDataUpdated
    directly because there's nothing to refresh.

    Best-effort: a broadcast failure must not cascade — the durable
    record is the DB write, the broadcast is the latency optimisation.
    """
    key = _service_key()
    if not key:
        log.debug(
            "laravel_bridge: FASTAPI_SERVICE_KEY not set; skipping workspace.data_updated broadcast",
        )
        return

    url = f"{_laravel_base()}/api/internal/v1/workspace-data-updated"
    payload: dict[str, Any] = {
        "workspace_id": workspace_id,
        "project_id": project_id,
        "pipeline_run_id": pipeline_run_id,
        "affected_types": affected_types,
    }

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(
                url,
                json=payload,
                headers={"X-Service-Key": key, "Accept": "application/json"},
            )
        if r.status_code >= 400:
            log.warning(
                "laravel_bridge: workspace.data_updated broadcast non-2xx run=%s "
                "types=%s http=%s body=%s",
                pipeline_run_id, affected_types, r.status_code, r.text[:200],
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "laravel_bridge: workspace.data_updated broadcast failed run=%s "
            "types=%s err=%s",
            pipeline_run_id, affected_types, exc,
        )


async def post_admin_surface_updated(
    *,
    surface: str,
    affected_props: list[str],
    surface_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Push an AdminSurfaceUpdated event into Laravel for Reverb fan-out.

    Phase 2 generic admin-side bridge. Used by Hatchet workflows + Dagster
    + the central emit_audit helper to notify admin pages that their data
    source changed.

    `surface` must match an entry in ALLOWED_SURFACES on the Laravel side
    (AdminSurfaceUpdatedBridgeController) and a registered channel in
    routes/channels.php. Unknown surfaces are rejected with 422.

    `affected_props` is the prop-key list the receiving page passes to
    Inertia's router.reload({ only: [...] }). Match the controller's
    Inertia::render(...) keys exactly — a typo silently no-ops the reload.

    `surface_id` triggers per-resource channel routing (e.g.
    `admin.target-run.{run_id}`); omit for shared list-page channels.

    `payload` is optional richer context (kind, status, run_id, count, etc.).
    The receiving page can filter on it. Keep it small — this is a hint,
    not a data transport.

    Best-effort: a broadcast failure must not cascade — the durable
    record is the DB write that triggered the broadcast.
    """
    key = _service_key()
    if not key:
        log.debug(
            "laravel_bridge: FASTAPI_SERVICE_KEY not set; skipping admin.surface_updated broadcast",
        )
        return

    url = f"{_laravel_base()}/api/internal/v1/admin-surface-updated"
    body: dict[str, Any] = {
        "surface": surface,
        "affected_props": affected_props,
    }
    if surface_id is not None:
        body["surface_id"] = surface_id
    if payload is not None:
        body["payload"] = payload

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(
                url,
                json=body,
                headers={"X-Service-Key": key, "Accept": "application/json"},
            )
        if r.status_code >= 400:
            log.warning(
                "laravel_bridge: admin.surface_updated broadcast non-2xx surface=%s "
                "surface_id=%s http=%s body=%s",
                surface, surface_id, r.status_code, r.text[:200],
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "laravel_bridge: admin.surface_updated broadcast failed surface=%s "
            "surface_id=%s err=%s",
            surface, surface_id, exc,
        )


async def post_workspace_activity(
    *,
    workspace_id: str,
    affected_types: list[str],
    payload: dict[str, Any] | None = None,
) -> None:
    """Push a WorkspaceActivityBroadcast event into Laravel for Reverb fan-out.

    Phase 3 — drives the workspace-scoped Foundry pages (Portfolio,
    Projects) so they re-fetch when any project inside the workspace gets
    new data. Distinct from `post_workspace_data_updated`: that helper is
    project-scoped (project.{projectId}.ingestion channel); this one fires
    on the workspace channel (workspace.{workspace_id}.activity).

    `affected_types` is the receiver's filter key. Recognised values:
    'projects', 'kpis', 'activity', 'cost' (LlmCost), 'tickets' /
    'traces' (SupportCockpit), but any string is accepted on the wire —
    the page-side hook ignores unknown values.

    Best-effort: a broadcast failure must not cascade — the durable
    record is the DB write that triggered the broadcast.
    """
    key = _service_key()
    if not key:
        log.debug(
            "laravel_bridge: FASTAPI_SERVICE_KEY not set; skipping workspace.activity broadcast",
        )
        return

    url = f"{_laravel_base()}/api/internal/v1/workspace-activity"
    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "affected_types": affected_types,
    }
    if payload is not None:
        body["payload"] = payload

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(
                url,
                json=body,
                headers={"X-Service-Key": key, "Accept": "application/json"},
            )
        if r.status_code >= 400:
            log.warning(
                "laravel_bridge: workspace.activity broadcast non-2xx ws=%s "
                "types=%s http=%s body=%s",
                workspace_id, affected_types, r.status_code, r.text[:200],
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "laravel_bridge: workspace.activity broadcast failed ws=%s "
            "types=%s err=%s",
            workspace_id, affected_types, exc,
        )


async def post_user_inbox_updated(
    *,
    user_id: int,
    kind: str,
    count_delta: int = 1,
    payload: dict[str, Any] | None = None,
) -> None:
    """Push a UserInboxUpdated event into Laravel for Reverb fan-out.

    Phase 3 — drives the Foundry/Inbox page + nav-bar inbox badge.
    `kind` must be one of 'mention', 'review', 'refusal' (matches the
    three inbox source tables in InboxController).

    Best-effort: a broadcast failure must not cascade.
    """
    key = _service_key()
    if not key:
        log.debug(
            "laravel_bridge: FASTAPI_SERVICE_KEY not set; skipping user.inbox_updated broadcast",
        )
        return

    url = f"{_laravel_base()}/api/internal/v1/user-inbox-updated"
    body: dict[str, Any] = {
        "user_id": user_id,
        "kind": kind,
        "count_delta": count_delta,
    }
    if payload is not None:
        body["payload"] = payload

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(
                url,
                json=body,
                headers={"X-Service-Key": key, "Accept": "application/json"},
            )
        if r.status_code >= 400:
            log.warning(
                "laravel_bridge: user.inbox_updated broadcast non-2xx user=%s "
                "kind=%s http=%s body=%s",
                user_id, kind, r.status_code, r.text[:200],
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "laravel_bridge: user.inbox_updated broadcast failed user=%s "
            "kind=%s err=%s",
            user_id, kind, exc,
        )


async def post_public_geoscience_tiles_invalidated(
    *,
    jurisdiction_epoch: int,
    source_ids: list[str] | None = None,
) -> None:
    """Push a PublicGeoscienceTilesInvalidated event into Laravel for Reverb fan-out.

    Phase 4 — fires when public_geoscience_pull (or SMDI overnight, P3
    follow-up) refreshes public_geo.* data. The browser-side
    PublicGeoscienceMap re-issues MapLibre setTiles() with the new
    ?v={jurisdiction_epoch} cache-bust, forcing the in-memory tile
    cache to drop and the proxy's ETag check to fire on the next fetch.

    `jurisdiction_epoch` should be the post-write MAX(updated_at)
    epoch_s from public_geo.jurisdictions — the same value
    TileProxyController::computePgeoEtag uses for the server ETag. Keeping
    them aligned guarantees: new event → new URL → new ETag → real refetch.

    `source_ids` (optional) limits the invalidation to specific PGEO
    sources (e.g. ['pg_mines'] when a SMDI-style workflow only touched
    one view). Null = invalidate every source the map subscribes to.

    Best-effort: a broadcast failure must not cascade — the durable
    record is the upstream public_geo.* write that triggered it.
    """
    key = _service_key()
    if not key:
        log.debug(
            "laravel_bridge: FASTAPI_SERVICE_KEY not set; skipping pgeo.tiles_invalidated broadcast",
        )
        return

    url = f"{_laravel_base()}/api/internal/v1/public-geoscience-tiles-invalidated"
    body: dict[str, Any] = {"jurisdiction_epoch": int(jurisdiction_epoch)}
    if source_ids is not None:
        body["source_ids"] = source_ids

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(
                url,
                json=body,
                headers={"X-Service-Key": key, "Accept": "application/json"},
            )
        if r.status_code >= 400:
            log.warning(
                "laravel_bridge: pgeo.tiles_invalidated broadcast non-2xx "
                "epoch=%s http=%s body=%s",
                jurisdiction_epoch, r.status_code, r.text[:200],
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "laravel_bridge: pgeo.tiles_invalidated broadcast failed epoch=%s err=%s",
            jurisdiction_epoch, exc,
        )


__all__ = [
    "post_report_build_progress",
    "post_ingestion_progress",
    "post_workspace_data_updated",
    "post_admin_surface_updated",
    "post_workspace_activity",
    "post_user_inbox_updated",
    "post_public_geoscience_tiles_invalidated",
]
