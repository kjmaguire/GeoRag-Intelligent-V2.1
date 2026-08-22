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
- Terminal ingestion statuses are the one exception to fire-and-forget: they
  are retried. See ``post_ingestion_progress``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from app.ingest_status import TERMINAL_STATUSES

log = logging.getLogger(__name__)

# Herd's local hostname. Correct for `php artisan serve` on a dev laptop and
# nowhere else — inside any container it resolves to nothing.
_DEFAULT_LARAVEL_URL = "http://laravel.test"

# One-shot latch so the misconfiguration warning below is emitted once per
# process instead of once per callback.
_warned_unset_base = False

#: Backoff between retries of a TERMINAL ingestion callback, in seconds.
#: Three attempts total. Sized against a container restart rather than a
#: prolonged outage: past a few seconds the run is better served by the
#: nightly MV refresh than by a workflow step sitting on a socket.
_INGESTION_RETRY_DELAYS: tuple[float, ...] = (0.5, 2.0)


def _laravel_base() -> str:
    """Resolve the Laravel base URL, complaining loudly if it isn't configured.

    Incident 2026-08-18: LARAVEL_INTERNAL_URL was never set on fastapi-cc or
    hatchet-worker-cc, so every bridge call in production silently fell back
    to http://laravel.test and died on DNS. The only symptom was a per-call
    warning reading `err=[Errno -2] Name or service not known` — which looks
    like a transient network blip, not a missing environment variable, so it
    went unread for weeks while the ENTIRE real-time layer (ingestion
    progress, workspace-data-updated, admin surfaces, user inbox) was dead.

    The default is kept so local Herd development keeps working, but falling
    back to it is now reported once, at ERROR, naming the variable.
    """
    global _warned_unset_base

    configured = os.environ.get("LARAVEL_INTERNAL_URL")
    if configured:
        return configured.rstrip("/")

    if not _warned_unset_base:
        _warned_unset_base = True
        log.error(
            "laravel_bridge: LARAVEL_INTERNAL_URL is not set; falling back to %s. "
            "Every callback into Laravel (ingestion progress, workspace-data-updated, "
            "admin surfaces, report-build progress, user inbox) will fail unless this "
            "host resolves. In Azure Container Apps set it to http://laravel-octane-cc.",
            _DEFAULT_LARAVEL_URL,
        )

    return _DEFAULT_LARAVEL_URL.rstrip("/")


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

    Best-effort on progress events: the durable record is the
    silver.ingest_progress row and the broadcast is a latency optimisation,
    and a dropped one is superseded by the next.

    NOT best-effort on a terminal status, which is why those are retried.
    The Laravel endpoint does three things beyond broadcasting when the
    status is in DATA_LANDED_STATUSES, and this POST is the only trigger for
    any of them:

      * ``WorkspaceDataVersionBumper::bump()`` — the cache-invalidation token
        the MVT tile proxy and the answer-run cache compare against. Nothing
        else ever increments it, so a dropped POST means tile caches keep
        serving pre-ingest geometry indefinitely, not until some later sweep.
      * ``DebounceWorkspaceMvRefresh`` — the materialised-view refresh. The
        only other trigger is the 03:00 UTC ``mv_refresh_silver`` cron, so
        the data is missing from every MV-derived surface until tomorrow.
      * ``WorkspaceDataUpdated`` — the SPA's partial reload.

    A Laravel rollout restarting its container for twenty seconds was enough
    to lose all three for every document that finished in that window, with
    a single WARNING line as the only trace.

    Retries are bounded and only cover failures that can plausibly clear:
    transport errors and 5xx. A 4xx means Laravel understood us and said no,
    and replaying it just makes the same rejection three times.
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

    attempts = _INGESTION_RETRY_DELAYS if status in TERMINAL_STATUSES else ()
    last_error: str | None = None

    for attempt in range(len(attempts) + 1):
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.post(
                    url,
                    json=payload,
                    headers={"X-Service-Key": key, "Accept": "application/json"},
                )
            if r.status_code < 400:
                if attempt:
                    log.info(
                        "laravel_bridge: ingestion broadcast succeeded on retry "
                        "run=%s status=%s attempt=%d",
                        run_id, status, attempt + 1,
                    )
                return
            last_error = f"http={r.status_code} body={r.text[:200]}"
            if r.status_code < 500:
                # Laravel understood the request and refused it. Replaying
                # changes nothing.
                break
        except Exception as exc:  # noqa: BLE001
            last_error = f"err={exc}"

        if attempt < len(attempts):
            await asyncio.sleep(attempts[attempt])

    log.warning(
        "laravel_bridge: ingestion broadcast failed run=%s status=%s "
        "attempts=%d %s",
        run_id, status, len(attempts) + 1, last_error,
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


__all__ = [
    "post_report_build_progress",
    "post_ingestion_progress",
    "post_workspace_data_updated",
    "post_admin_surface_updated",
    "post_workspace_activity",
    "post_user_inbox_updated",
]
