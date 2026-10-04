"""Per-file ingestion progress writer (Phase 1 — per-run rows).

Tiny module called by each step of the ingest_pdf + tiff_normalize Hatchet
workflows so the IngestionRunsController can show real per-file progress
bars instead of the time-elapsed heuristics Phase A shipped with.

Writes into silver.ingest_progress (the Phase B table extended by the
2026_05_25 reliability migration). Originally one row per (workspace_id,
minio_key); now one row per run with parent_run_id linking recovery work
to the original. All terminal-state writes use the conditional-update
pattern so a delayed worker can't overwrite a previously-set terminal
state.

Step model (5 logical steps):
    1  preflight
    2  parse
    3  persist
    4  embed_verify
    5  embedding         (set by the embed dispatcher when all chunks have ids)

State machine:
    queued  → started
    started → completed | failed | timed_out
    queued  → cancelled

Terminal states: completed, failed, cancelled, timed_out (immutable).

Best-effort error handling: a DB failure inside a progress helper must
never block the surrounding workflow task, so every helper swallows
exceptions and just logs. Hatchet's on_failure_task is the durable
backstop — if a progress write goes missing, the workflow-level hook
re-asserts the terminal state.

See [[ingestion-runs-ui-2026-05-24]] for design notes and
[[ingestion-reliability-spec]] for the per-run schema rationale.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass

import asyncpg

from app import ingest_status as _ingest_status
from app.db.dsn import build_dsn
from app.db.scoped_pool import bind_workspace_scope

log = logging.getLogger("georag.hatchet.progress")

# Ordered list — index in this list = step_index written to the DB.
STEPS: tuple[str, ...] = (
    "preflight",
    "parse",
    "persist",
    "embed_verify",
    "embedding",
)
TOTAL_STEPS = len(STEPS)

#: Re-exported from ``app.ingest_status`` so every existing
#: ``_progress.TERMINAL_STATUSES`` reference keeps working. The definition
#: moved to a leaf module because ``app.services.laravel_bridge`` needs to
#: know which statuses are terminal, and importing this package constructs a
#: Hatchet client as a side effect.
TERMINAL_STATUSES = _ingest_status.TERMINAL_STATUSES
TERMINAL_STATUS_SQL = _ingest_status.TERMINAL_STATUS_SQL
ALLOWED_TRIGGERS: tuple[str, ...] = (
    "upload",
    "embed_pending_sweep",
    "nightly_integrity_sweep",
    "manual_retry",
    "stale_run_sweep",
)


def recovery_max_attempts() -> int:
    """How deep a parent_run_id recovery chain may go before we stop.

    3 means: the original upload plus two automated retries, then leave
    the row in its terminal state for a human.

    Lives here rather than in either caller because BOTH sweeps create
    recovery runs and both must agree. stale_run_detector had this cap
    from the start; orphan_sweep did not, and minted a new recovery row
    every ten minutes for any permanently unembeddable document — about
    twelve dead rows a day, each firing a Reverb `timed_out` broadcast
    that the Ingestion Runs UI showed as a fresh failed ingestion the
    user never started.

    Two readers of one environment variable, each with its own default,
    is how the two sweeps would silently drift apart.
    """
    raw = os.environ.get("STALE_RUN_RECOVERY_MAX_ATTEMPTS", "3")
    try:
        value = int(raw)
    except ValueError:
        return 3
    return value if value > 0 else 3


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_dsn = build_dsn


# ---------------------------------------------------------------------------
# Module-level asyncpg pool — spec constraint #3: hooks/sweeps reuse the pool.
# ---------------------------------------------------------------------------
_pool: asyncpg.Pool | None = None
# Guards pool creation. get_pool() is also called from the FastAPI request
# path (shadow_trigger, mv_refresh_trigger, ingest_progress.start_run), where
# concurrent first requests on one worker used to each pass the None check,
# each create a pool, and leak all but the last for the process lifetime
# (API-12). Double-checked below so the steady state takes no lock.
_pool_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    """Return the lazily-initialised module pool.

    One module-level pool per process avoids the per-call connect overhead
    the Phase B implementation paid for every write. Creation is serialised
    by ``_pool_lock`` because this runs under concurrent FastAPI requests,
    not only in the single-process Hatchet worker.
    """
    global _pool
    if _pool is not None and not _pool.is_closing():
        return _pool
    async with _pool_lock:
        if _pool is None or _pool.is_closing():
            _pool = await asyncpg.create_pool(
                _dsn(),
                min_size=1,
                max_size=4,
                statement_cache_size=0,
            )
        return _pool


async def close_pool() -> None:
    """Close the module pool if one was created. Idempotent.

    Called from the FastAPI lifespan teardown so the connections are closed
    cleanly instead of being dropped by the server when the task stops.
    """
    global _pool
    async with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None and not pool.is_closing():
        await pool.close()


#: The prefix Laravel prepends to every uploaded object's name.
#:
#: UploadController builds ``{category}/{project}/{Ymd_His}_{name}`` and
#: DrillUploadController ``{prefix}/{workspace}/{Ymd_His}_{sha8}_{name}``, so
#: the timestamp — and sometimes eight hex characters of digest — sit inside
#: the FILENAME rather than in a path segment of their own. Everything that
#: shows a user which file they uploaded reads the last path segment, so the
#: Ingestion Runs page listed
#: ``20260824_204605_NEW_HYD.BX_Central_Clean.dxf``.
#:
#: Anchored, and both groups are fixed-width, so it only matches the shape
#: that is actually generated. A file the geologist themselves named
#: ``20260824_survey.csv`` keeps its name — the seconds field is required,
#: and their own prefix has none.
#: Three writers mint these keys and they do NOT agree on shape:
#: UploadController uses ``{Ymd_His}_{name}``, DrillUploadController inserts an
#: 8-hex digest, and ingest_zip_archive uses ``strftime('%Y%m%d_%H%M%S_%f')``,
#: whose third component is six DECIMAL digits. The pattern was written for the
#: first two only, so every file extracted from a ZIP kept
#: ``20260824_204518_123456_`` glued to the front of its display name — a
#: filename fix that still showed the user a machine string.
_GENERATED_PREFIX = re.compile(r"^\d{8}_\d{6}_(?:[0-9a-f]{8}_|\d{6}_)?")

#: tiff_normalize's derived-PDF key, `tiff-derived-{sha8}-{stem}.pdf`. The
#: sha8 disambiguates two different rasters that share a filename, and is
#: storage bookkeeping the user has no use for.
_DERIVED_PREFIX = re.compile(r"^tiff-derived-[0-9a-f]{8}-")


def _filename_from_key(minio_key: str) -> str:
    """The name the user recognises, not the name storage gave it.

    The storage key stays authoritative — it is what ``minio_key`` holds and
    what every lookup joins on. This is the display name only, so a prefix
    stripped here cannot make two objects collide.

    Falls back to the un-stripped segment whenever stripping would leave
    nothing, so a key that is somehow only a prefix still shows something.
    """
    segment = minio_key.rsplit("/", 1)[-1] if "/" in minio_key else minio_key
    # A TIFF/RRD normalises to a PDF at its own `tiff-derived-{sha8}-{stem}`
    # key, and that derived key is what downstream rows point at. Stripped
    # first so the upload-prefix rule below still applies to the stem beneath.
    segment = _DERIVED_PREFIX.sub("", segment)
    stripped = _GENERATED_PREFIX.sub("", segment)
    return stripped or segment


def _step_index(stage: str) -> int:
    """Return 1-indexed step number, or 0 for non-step stages (e.g. 'queued')."""
    try:
        return STEPS.index(stage) + 1
    except ValueError:
        return 0


def _record_terminal_metrics(
    *, status: str, triggered_by: str, duration_seconds: float,
) -> None:
    """Best-effort Prometheus instrumentation for terminal-state writes.

    Catches and swallows any error so a metrics-system outage cannot
    block the durable DB write — the invariant is the row, not the
    histogram bucket.
    """
    try:
        from app.metrics import INGESTION_RUN_DURATION, INGESTION_RUNS_TOTAL
        INGESTION_RUNS_TOTAL.labels(
            status=status, triggered_by=triggered_by,
        ).inc()
        INGESTION_RUN_DURATION.labels(
            status=status, triggered_by=triggered_by,
        ).observe(max(0.0, duration_seconds))
    except Exception:
        # Metrics are an observability layer; never fail the data write.
        pass


def _blocking_warnings(warnings: list[dict] | None) -> list[dict]:
    """The warnings that count against a run: everything not ``severity: info``.

    An informational entry (ingest_pdf stores a one-line "N pages recovered by
    OCR", the parse-mode summary, the detected languages) is kept on the row so
    a person can read it, but it is not a complaint, so it must not turn a
    clean run amber. Every other workflow writes warnings with no ``severity``
    key at all, which stay blocking exactly as before.
    """
    return [
        w for w in (warnings or [])
        if not (isinstance(w, dict) and w.get("severity") == "info")
    ]


def terminal_status(
    *, rows_written: int | None, warnings: list[dict] | None,
) -> str:
    """Which terminal status a finished run earned: 'completed' or 'partial'.

    'partial' means the run reached the end and something is still wrong:
    it wrote nothing, or it wrote something and also had complaints.
    ``rows_written`` is None for callers that do not report it, and None is
    deliberately not 0 — "did not say" is not "said zero".

    Warnings with ``severity == "info"`` do not count (see
    :func:`_blocking_warnings`).

    Shared rather than inlined because callers need to know which of the two
    the run was BEFORE they can name it in a broadcast, and re-deriving the
    rule at each call site is how the two answers drift apart.
    """
    return "partial" if (_blocking_warnings(warnings) or rows_written == 0) else "completed"


def terminal_message(
    *,
    rows_written: int | None,
    warnings: list[dict] | None,
    noun: str = "row",
) -> str:
    """One line a person can act on, for the completion toast.

    The warnings this surfaces are the ones `mark_completed_by_run`'s
    docstring cites as its whole reason for existing — "upload the collar
    file first, or pass hole_id explicitly" — text that used to live only
    inside the Hatchet run object, which no product surface reads. Putting
    the first one in the broadcast message is what finally carries it to
    somewhere a geologist looks.

    Capped at 500 characters because that is the Laravel endpoint's
    validation limit on `message`; a longer string is a 422, which is a
    dropped notification.
    """
    warnings = _blocking_warnings(warnings)
    if rows_written is None:
        head = "Finished"
    elif rows_written == 0:
        head = f"No {noun}s written"
    else:
        head = f"{rows_written:,} {noun}{'' if rows_written == 1 else 's'} written"

    if not warnings:
        return head[:500]

    first = str(warnings[0].get("detail") or warnings[0].get("code") or "").strip()
    more = f" (+{len(warnings) - 1} more)" if len(warnings) > 1 else ""
    return f"{head} — {first}{more}"[:500]


async def broadcast_terminal(
    *,
    workspace_id: str,
    project_id: str,
    run_id: str,
    stage: str,
    status: str,
    message: str | None = None,
) -> None:
    """Tell Laravel a run reached a terminal state, so the UI can react.

    This is the ONLY thing that moves an ingest from "finished in the
    database" to "visible in the product". The Laravel endpoint fans the
    event out over Reverb *and*, for a terminal status that wrote data,
    bumps silver.projects.data_version and queues the debounced
    materialised-view refresh. Skip it and the run completes into silence:
    no toast, no partial reload on Overview / Reports / Sources / the
    drillhole page, stale MVT tiles on the map, and stale MVs behind the
    KPI cards until the nightly refresh.

    ingest_pdf broadcasts from its own embed_verify sweep because a PDF is
    not really queryable until its passages carry embeddings. The tabular,
    spatial and well-log workflows have no such second phase — their rows
    ARE the deliverable the moment persist commits — and for want of these
    six lines they never notified anything at all. A geologist uploading a
    collar CSV watched the Ingestion Runs page tick over to "Completed"
    and then found the map unchanged.

    Best-effort by construction: ``post_ingestion_progress`` swallows its
    own transport errors, and the import is local to keep laravel_bridge
    out of this module's import cycle.
    """
    try:
        from app.services.laravel_bridge import (  # noqa: PLC0415
            post_ingestion_progress,
        )

        await post_ingestion_progress(
            workspace_id=workspace_id,
            project_id=project_id,
            run_id=run_id,
            stage=stage,
            status=status,
            message=message,
        )
    except Exception as exc:  # noqa: BLE001 — notification must not fail a run
        log.warning(
            "progress.broadcast_terminal failed run=%s status=%s: %s",
            run_id, status, exc,
            extra={"run_id": run_id, "status": status},
        )


# ---------------------------------------------------------------------------
# Per-run API (the new Phase 1 surface)
# ---------------------------------------------------------------------------
#: The queued-row INSERT shared by :func:`start_run` and :func:`claim_dispatch`.
_START_RUN_SQL = """
    INSERT INTO silver.ingest_progress (
        run_id, workspace_id, project_id, workflow_run_id,
        minio_key, filename,
        status, current_stage, current_step,
        step_index, total_steps,
        triggered_by, parent_run_id, recovery_reason,
        attempt_number,
        started_at, updated_at
    )
    SELECT
        $1::uuid, $2::uuid, $3::uuid, $4,
        $5, $6,
        'queued', NULL, 'queued',
        0, $7,
        $8, $9::uuid, $10,
        COALESCE((
            SELECT MAX(attempt_number) + 1
            FROM silver.ingest_progress
            WHERE workspace_id = $2::uuid AND minio_key = $5
        ), 1),
        now(), now()
    ON CONFLICT (run_id) DO NOTHING
    RETURNING run_id::text
"""


async def start_run(
    *,
    workspace_id: str,
    project_id: str,
    minio_key: str,
    triggered_by: str = "upload",
    parent_run_id: str | None = None,
    recovery_reason: str | None = None,
    workflow_run_id: str | None = None,
    run_id: str | None = None,
) -> str | None:
    """Insert an ingest_progress row and return its run_id.

    Pass ``run_id`` when the caller already minted one (Laravel stamps a
    UUID on every upload and forwards it to the workflow). The row is then
    created *under that id*, so the caller's id and the row's id are the
    same value and every later stage/completion UPDATE finds its row. This
    is an upsert — calling it twice with the same run_id is a no-op, so a
    trigger endpoint and a workflow preflight may both call it safely.

    Omit ``run_id`` and a fresh one is generated, which is what the
    recovery paths want: sweep / nightly_integrity / manual_retry create
    new rows linked to the original via parent_run_id rather than mutating
    a previously-terminal row. ``attempt_number`` is derived server-side
    from prior attempts on the same (workspace_id, minio_key) so the UI's
    "attempt 3 of N" badge stays accurate.

    Returns None on DB failure (best-effort).
    """
    if triggered_by not in ALLOWED_TRIGGERS:
        log.warning(
            "progress.start_run: unknown triggered_by=%r (forcing 'upload')",
            triggered_by,
            extra={"workspace_id": workspace_id, "minio_key": minio_key},
        )
        triggered_by = "upload"

    filename = _filename_from_key(minio_key)
    new_run_id = run_id or str(uuid.uuid4())

    sql = _START_RUN_SQL
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                new_run_id,
                workspace_id,
                project_id,
                workflow_run_id,
                minio_key,
                filename,
                TOTAL_STEPS,
                triggered_by,
                parent_run_id,
                recovery_reason,
            )
        # ON CONFLICT DO NOTHING returns no row when the run already
        # exists; that is a success, not a failure — the id is still ours.
        return row["run_id"] if row else new_run_id
    except Exception as e:
        log.warning(
            "progress.start_run failed (key=%s): %s", minio_key, e,
            extra={"workspace_id": workspace_id, "minio_key": minio_key},
        )
        return None


#: Serialises check-and-claim for one file. hashtextextended gives a 64-bit
#: key, so two different files colliding on the lock is vanishingly rare,
#: and a collision would only serialise them, not dedupe them.
_CLAIM_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))"

_ACTIVE_RUN_FOR_KEY_SQL = f"""
    SELECT run_id::text AS run_id, workflow_run_id
      FROM silver.ingest_progress
     WHERE workspace_id = $1::uuid AND minio_key = $2
       AND status NOT IN ({TERMINAL_STATUS_SQL})
     ORDER BY attempt_number DESC, started_at DESC
     LIMIT 1
"""


@dataclass(frozen=True)
class DispatchClaim:
    """Outcome of :func:`claim_dispatch`.

    ``claimed`` True: this caller inserted the queued row under ``run_id``
    and must dispatch, then call :func:`stamp_workflow_run_id` (or
    :func:`release_undispatched` if the dispatch raised).

    ``claimed`` False: a run for this file is already queued or running, or
    this exact ``run_id`` was already recorded. ``run_id`` and
    ``workflow_run_id`` identify that run; do not dispatch again.
    """

    claimed: bool
    run_id: str
    workflow_run_id: str | None = None


async def claim_dispatch(
    *,
    workspace_id: str,
    project_id: str,
    minio_key: str,
    run_id: str | None = None,
    triggered_by: str = "upload",
) -> DispatchClaim:
    """Record the queued progress row BEFORE dispatch, or report a duplicate.

    HAT-6/HAT-12 (2026-09-29). The trigger endpoints used to dispatch first
    and insert the progress row second. Laravel wraps every trigger call in
    ``retry(3, 500)`` with a 15 s timeout, so a slow ``aio_run_no_wait``
    got re-POSTed. The retry found no row yet and dispatched a second run.
    Two concurrent ingest_tabular runs each DELETE-then-INSERT in their own
    READ COMMITTED transaction, and both inserts survive, which doubles the
    interval rows. ingest_pdf's preflight could also race the endpoint and
    mint its own row.

    Now, under a per-file advisory lock, in one transaction with the
    workspace scope bound:

    1. an existing non-terminal row for (workspace, key) is returned as a
       duplicate;
    2. otherwise the queued row is inserted under ``run_id``. If that
       run_id is already recorded, ``ON CONFLICT DO NOTHING`` returns
       nothing, which is a duplicate too: the same caller-minted id arriving
       twice is a retry, even after the first run finished.

    Raises on a database error. The caller decides whether to fail open.
    """
    if triggered_by not in ALLOWED_TRIGGERS:
        triggered_by = "upload"
    new_run_id = run_id or str(uuid.uuid4())
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await bind_workspace_scope(
            conn, workspace_id=workspace_id, site="progress.claim_dispatch",
        )
        await conn.execute(
            _CLAIM_LOCK_SQL,
            f"ingest_dispatch:{workspace_id}:{project_id}:{minio_key}",
        )
        existing = await conn.fetchrow(
            _ACTIVE_RUN_FOR_KEY_SQL, workspace_id, minio_key,
        )
        if existing is not None:
            return DispatchClaim(
                claimed=False,
                run_id=existing["run_id"],
                workflow_run_id=existing["workflow_run_id"],
            )
        inserted = await conn.fetchrow(
            _START_RUN_SQL,
            new_run_id,
            workspace_id,
            project_id,
            None,
            minio_key,
            _filename_from_key(minio_key),
            TOTAL_STEPS,
            triggered_by,
            None,
            None,
        )
        if inserted is None:
            prior = await conn.fetchrow(
                "SELECT workflow_run_id FROM silver.ingest_progress "
                "WHERE run_id = $1::uuid",
                new_run_id,
            )
            return DispatchClaim(
                claimed=False,
                run_id=new_run_id,
                workflow_run_id=prior["workflow_run_id"] if prior else None,
            )
    return DispatchClaim(claimed=True, run_id=new_run_id)


async def stamp_workflow_run_id(*, run_id: str, workflow_run_id: str | None) -> None:
    """Attach the Hatchet run id to a row claimed before dispatch.

    Only fills a NULL, so a workflow that already stamped its own id (the
    run started before this UPDATE landed) is never overwritten.
    Best-effort, like every writer here.
    """
    if not workflow_run_id:
        return
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE silver.ingest_progress "
                "SET workflow_run_id = $2, updated_at = now() "
                "WHERE run_id = $1::uuid AND workflow_run_id IS NULL",
                run_id, workflow_run_id,
            )
    except Exception as e:
        log.warning(
            "progress.stamp_workflow_run_id failed run=%s: %s", run_id, e,
            extra={"run_id": run_id, "workflow_run_id": workflow_run_id},
        )


async def release_undispatched(*, run_id: str) -> None:
    """Remove a claimed row whose dispatch raised.

    Nothing was queued in Hatchet, so the row would sit at 'queued' until
    the stale sweep timed it out. Worse, a caller-minted run_id would make
    Laravel's own retry of the failed request look like a duplicate. The
    guard (still queued, no workflow_run_id) means a row a worker has
    already touched is never removed.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM silver.ingest_progress "
                "WHERE run_id = $1::uuid AND status = 'queued' "
                "AND workflow_run_id IS NULL",
                run_id,
            )
    except Exception as e:
        log.warning(
            "progress.release_undispatched failed run=%s: %s", run_id, e,
            extra={"run_id": run_id},
        )


async def mark_stage_started(
    *,
    run_id: str,
    stage: str,
    worker_id: str | None = None,
) -> None:
    """Mark the given stage as in-progress for this run.

    Uses the conditional-update pattern (Fix 1a): the update is a no-op
    if ``status`` is already terminal. This prevents a delayed step from
    re-opening a workflow the failure hook already closed.

    Also flips status from 'queued' → 'started' on the first stage transition.
    """
    if stage not in STEPS and stage != "queued":
        log.warning(
            "progress.mark_stage_started: unknown stage %r", stage,
            extra={"run_id": run_id, "stage": stage},
        )
        return

    sql = f"""
        UPDATE silver.ingest_progress
        SET current_stage         = $2,
            current_step          = $2,
            step_index            = $3,
            -- Sub-step progress belongs to the step that wrote it; a new
            -- step starts at 0 or the UI shows "Saving to database —
            -- extracting page 533/575".
            stage_pct             = NULL,
            stage_detail          = NULL,
            last_stage_started_at = now(),
            last_heartbeat_at     = now(),
            step_started_at       = now(),
            worker_id             = COALESCE($4, worker_id),
            status                = CASE WHEN status = 'queued' THEN 'started' ELSE status END,
            updated_at            = now()
        WHERE run_id = $1::uuid
          AND status NOT IN ({TERMINAL_STATUS_SQL})
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(sql, run_id, stage, _step_index(stage), worker_id)
    except Exception as e:
        log.warning(
            "progress.mark_stage_started failed (run=%s stage=%s): %s",
            run_id, stage, e,
            extra={"run_id": run_id, "stage": stage},
        )


async def mark_stage_progress(
    *,
    run_id: str,
    stage_pct: float,
    stage_detail: str | None = None,
) -> None:
    """Record fractional progress WITHIN the current step.

    stage_pct is 0..1 inside the current stage; the UI composes it with
    step_index/total_steps for a smooth bar instead of the old 5-step
    quantization (which sat at 40% through an entire multi-minute
    parse). Cheap single-row UPDATE, best-effort, and doubles as a
    heartbeat so page-level ticks keep the stale-run detector fed.
    """
    sql = """
        UPDATE silver.ingest_progress
        SET stage_pct         = $2,
            stage_detail      = $3,
            last_heartbeat_at = now(),
            updated_at        = now()
        WHERE run_id = $1::uuid AND status = 'started'
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                sql, run_id, max(0.0, min(1.0, float(stage_pct))), stage_detail,
            )
    except Exception as e:
        log.warning(
            "progress.mark_stage_progress failed (run=%s): %s", run_id, e,
            extra={"run_id": run_id},
        )


async def mark_heartbeat(*, run_id: str) -> None:
    """Bump last_heartbeat_at for a running task.

    The 15-min stale_run_detector cron uses this to detect dead workers.
    No-op if the run is no longer in 'started' state.
    """
    sql = """
        UPDATE silver.ingest_progress
        SET last_heartbeat_at = now(),
            updated_at        = now()
        WHERE run_id = $1::uuid AND status = 'started'
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(sql, run_id)
    except Exception as e:
        log.warning(
            "progress.mark_heartbeat failed (run=%s): %s", run_id, e,
            extra={"run_id": run_id},
        )


import contextlib  # noqa: E402


@contextlib.asynccontextmanager
async def heartbeat_loop(
    *,
    workspace_id: str | None = None,
    minio_key: str | None = None,
    run_id: str | None = None,
    interval_seconds: float = 30.0,
):
    """Async context manager that runs a background heartbeat ticker.

    Resolves the active run_id from (workspace_id, minio_key) once at
    entry — or takes ``run_id`` directly when the caller already holds it,
    which is every workflow whose input carries the id Laravel minted —
    then bumps last_heartbeat_at every ``interval_seconds`` until the
    with-block exits. Used in long-running ingest tasks so the
    stale_run_detector cron knows the worker is still alive.

    Usage::

        async with ingest_progress.heartbeat_loop(
            workspace_id=ws, minio_key=key,
        ):
            await do_long_work()

    Best-effort: if the run_id can't be resolved, the loop becomes a
    no-op. The surrounding task keeps running.
    """
    if run_id is None and workspace_id and minio_key:
        run_id = await lookup_active_run_id(
            workspace_id=workspace_id, minio_key=minio_key,
        )
    task: asyncio.Task | None = None
    if run_id is not None:

        async def _ticker() -> None:
            try:
                while True:
                    await asyncio.sleep(interval_seconds)
                    await mark_heartbeat(run_id=run_id)
            except asyncio.CancelledError:
                pass

        task = asyncio.create_task(_ticker(), name=f"hb-{run_id[:8]}")
    try:
        yield run_id
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def mark_report_id(
    *,
    workspace_id: str,
    minio_key: str,
    report_id: str,
    conn: asyncpg.Connection | None = None,
) -> bool:
    """Record the persisted report_id on the active (non-terminal) run row.

    Written at the end of ingest_pdf's persist step (F2, 2026-08-11) so the
    embed completion sweep and stale_run_detector can scope their
    "fully embedded?" predicates to THIS run's own document instead of the
    whole project — bulk imports serialize embeds per workspace, so a
    project-wide predicate timed out rows whose own document had already
    finished. Best-effort like every other helper here.

    ``conn``: when the caller passes its OPEN connection (persist does, from
    inside its transaction) the UPDATE rides that transaction, in a savepoint,
    so the report_id is committed WITH the report and its passages and a sweep
    cannot see the passages without it. Returns True iff the write ran.
    """
    sql = f"""
        UPDATE silver.ingest_progress
        SET report_id  = $3::uuid,
            updated_at = now()
        WHERE workspace_id = $1::uuid AND minio_key = $2
          AND status NOT IN ({TERMINAL_STATUS_SQL})
    """
    try:
        if conn is not None:
            async with conn.transaction():
                await conn.execute(sql, workspace_id, minio_key, report_id)
        else:
            pool = await get_pool()
            async with pool.acquire() as pooled:
                await pooled.execute(sql, workspace_id, minio_key, report_id)
        return True
    except Exception as e:
        log.warning(
            "progress.mark_report_id failed (key=%s): %s", minio_key, e,
            extra={"workspace_id": workspace_id, "minio_key": minio_key},
        )
        return False


def _decode_warnings(raw: object) -> list[dict]:
    """``silver.ingest_progress.warnings`` as a list of dicts.

    asyncpg hands a ``jsonb`` column back as ``str`` unless a codec is
    registered on the connection, and this module's pool registers none.
    Tolerates NULL, a malformed value and a non-list: a diagnostics read must
    never be the reason a run cannot be closed.
    """
    import json as _json  # noqa: PLC0415

    value = raw
    if isinstance(value, (str, bytes)):
        try:
            value = _json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [w for w in value if isinstance(w, dict)]


async def mark_run_diagnostics(
    *,
    workspace_id: str,
    minio_key: str,
    rows_written: int,
    warnings: list[dict],
    conn: asyncpg.Connection | None = None,
) -> bool:
    """Store what a run has produced so far on its NON-terminal row.

    ingest_pdf finishes in two places that are not the persist step: the
    ``embed_verify`` task, and — when embedding is still pending — the
    ``embed_pending_passages`` completion sweep or ``stale_run_detector``'s
    race recovery. The last two see nothing but the row, so persist leaves
    its verdict here and :func:`mark_completed_by_run` reads it back when the
    caller passes neither ``rows_written`` nor ``warnings``. Without this the
    sweeps wrote a bare ``completed`` over a document whose OCR had failed.

    Idempotent overwrite (a retried persist writes the same values). Returns
    True iff a row was updated; best-effort like every helper here.

    ``conn``: pass the caller's OPEN connection to write inside ITS
    transaction (in a savepoint, so a failed UPDATE cannot abort it). persist
    does, so the verdict commits with the passages: written after the commit
    there was a window in which a sweep that saw every passage embedded could
    close the run with no stored diagnostics, i.e. a bare ``completed`` over a
    document whose OCR had failed.
    """
    import json as _json  # noqa: PLC0415

    sql = f"""
        UPDATE silver.ingest_progress
        SET rows_written = $3,
            warnings     = $4::jsonb,
            updated_at   = now()
        WHERE workspace_id = $1::uuid AND minio_key = $2
          AND status NOT IN ({TERMINAL_STATUS_SQL})
    """
    try:
        if conn is not None:
            async with conn.transaction():
                result = await conn.execute(
                    sql, workspace_id, minio_key, int(rows_written),
                    _json.dumps(warnings),
                )
        else:
            pool = await get_pool()
            async with pool.acquire() as pooled:
                result = await pooled.execute(
                    sql, workspace_id, minio_key, int(rows_written),
                    _json.dumps(warnings),
                )
        return not str(result).endswith(" 0")
    except Exception as e:
        log.warning(
            "progress.mark_run_diagnostics failed (key=%s): %s", minio_key, e,
            extra={"workspace_id": workspace_id, "minio_key": minio_key},
        )
        return False


def image_passages_unembedded_warning(count: int) -> dict:
    """The INFO warning a run carries when page-image passages were not embedded.

    Image passages are best-effort: the Embed 5 image request shape is
    unverified, and a failing image embed must not hold the run open (the
    "fully embedded?" predicates exclude ``modality = 'image'``) nor turn it
    amber. It is still said, on the row, so a missing page image is not a
    silent coverage hole.
    """
    return {
        "code": "image_passages_unembedded",
        "severity": "info",
        "detail": (
            f"{count} page-image passage(s) have no embedding yet (image "
            f"embedding is best-effort and does not block the run); those "
            f"pages are searchable by their text only."
        ),
        "count": int(count),
    }


async def append_run_warning(*, run_id: str, warning: dict) -> bool:
    """Append one warning to a NON-terminal run's stored diagnostics.

    Idempotent per ``warning["code"]``: a second call with the same code (a
    sweep ticking again before the run closes) is a no-op rather than a
    duplicate entry. Call it BEFORE :func:`mark_completed_by_run`, which reads
    the stored warnings back. Returns True iff a row was updated; best-effort.
    """
    import json as _json  # noqa: PLC0415

    sql = f"""
        UPDATE silver.ingest_progress
        SET warnings   = COALESCE(warnings, '[]'::jsonb) || $2::jsonb,
            updated_at = now()
        WHERE run_id = $1::uuid
          AND status NOT IN ({TERMINAL_STATUS_SQL})
          AND NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(COALESCE(warnings, '[]'::jsonb)) AS w
                WHERE w ->> 'code' = $3
          )
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                sql, run_id, _json.dumps([warning]), str(warning.get("code")),
            )
        return not str(result).endswith(" 0")
    except Exception as e:
        log.warning(
            "progress.append_run_warning failed (run=%s): %s", run_id, e,
            extra={"run_id": run_id},
        )
        return False


async def terminal_outcome(
    *, run_id: str, default_message: str, noun: str = "passage",
) -> tuple[str, str]:
    """``(status, message)`` for the broadcast that follows a completion.

    Reads the row :func:`mark_completed_by_run` just closed, so a sweep that
    never held the warnings in memory still tells the UI the truth: the
    status the row actually earned, and for ``partial`` a line a person can
    act on instead of "Ingestion complete; all chunks embedded.". A clean
    run keeps ``default_message``. If the row cannot be read the answer is
    ``completed`` — the row is already terminal either way and the UI
    reconciles on its next poll.
    """
    row = await get_run(run_id=run_id)
    if not row:
        return "completed", default_message
    status = str(row.get("status") or "completed")
    if status != "partial":
        return status, default_message
    return status, terminal_message(
        rows_written=row.get("rows_written"),
        warnings=_decode_warnings(row.get("warnings")),
        noun=noun,
    )


async def mark_completed_by_run(
    *,
    run_id: str,
    report_id: str | None = None,
    rows_written: int | None = None,
    warnings: list[dict] | None = None,
) -> bool | None:
    """Terminal write — sets status=completed, or 'partial' when warranted.

    Pass ``rows_written`` and ``warnings`` and this decides which of the two
    the run really was. A run that finished cleanly but produced nothing, or
    finished with diagnostics attached, is not a completion in any sense the
    user cares about — and it used to render as an unqualified green
    "Completed" while the actionable warning text ("upload the collar file
    first, or pass hole_id explicitly") lived only inside the Hatchet run
    object, which the product UI never reads.

    Omit BOTH and the run's own stored diagnostics are used (see
    :func:`mark_run_diagnostics`): ingest_pdf's persist step records its
    passage count and OCR/parse warnings on the row, and the three places
    that close a PDF run (``embed_verify``, the embed completion sweep,
    ``stale_run_detector`` race recovery) all pass nothing. A row with no
    stored diagnostics behaves exactly as it did before: ``completed``.

    Returns a TRI-STATE:

      True   the row transitioned — fire the side effects.
      False  the row was already terminal — a retried hook, skip them.
      None   THE WRITE FAILED — the row is still non-terminal.

    None used to be False, which made a failed write look exactly like a
    harmless duplicate hook. Callers that gate side effects (mv_refresh,
    data_version bump, the workspace.data_updated / post_ingestion_progress
    broadcast) all branch on this, so a failed terminal write silently
    suppressed the very broadcast that tells the UI an ingest finished —
    leaving a run that never completes on the user's screen with no error
    anywhere except one WARNING line.

    Existing `if transitioned:` / `if not transitioned:` sites keep their
    exact behaviour, because None is falsy: nothing double-fires. What
    changes is that the failure is now distinguishable for callers that
    care, and is logged at ERROR.
    """
    import json as _json  # noqa: PLC0415

    sql = f"""
        UPDATE silver.ingest_progress
        SET status        = $4,
            current_step  = 'completed',
            current_stage = 'completed',
            step_index    = total_steps,
            completed_at  = now(),
            updated_at    = now(),
            report_id     = COALESCE($2::uuid, report_id),
            rows_written  = COALESCE($3, rows_written),
            warnings      = $5::jsonb,
            error_text    = NULL,
            failed_at     = NULL
        WHERE run_id = $1::uuid
          AND status NOT IN ({TERMINAL_STATUS_SQL})
        RETURNING run_id, triggered_by,
                  EXTRACT(EPOCH FROM (now() - started_at))::float AS duration_seconds
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if warnings is None and rows_written is None:
                stored = await conn.fetchrow(
                    "SELECT rows_written, warnings FROM silver.ingest_progress "
                    "WHERE run_id = $1::uuid",
                    run_id,
                )
                if stored is not None:
                    rows_written = stored["rows_written"]
                    warnings = _decode_warnings(stored["warnings"])
            warnings = warnings or []
            status = terminal_status(rows_written=rows_written, warnings=warnings)
            row = await conn.fetchrow(
                sql, run_id, report_id, rows_written, status, _json.dumps(warnings),
            )
        transitioned = row is not None
        if not transitioned:
            log.info(
                "progress.mark_completed: no-op (already terminal) run=%s",
                run_id, extra={"run_id": run_id},
            )
            return False
        if status == "partial":
            log.warning(
                "progress.mark_completed: run=%s finished PARTIAL "
                "(rows_written=%s, %d warning(s)): %s",
                run_id, rows_written, len(warnings),
                "; ".join(
                    str(w.get("detail") or w.get("code") or w)
                    for w in _blocking_warnings(warnings)[:3]
                ),
                extra={"run_id": run_id, "outcome": "partial"},
            )
        _record_terminal_metrics(
            status=status,
            triggered_by=row["triggered_by"] or "upload",
            duration_seconds=float(row["duration_seconds"] or 0.0),
        )
        return True
    except Exception as e:
        # ERROR, not WARNING, and None, not False. Returning False here made
        # a failed terminal write indistinguishable from "already terminal",
        # so every caller that gates a side effect on the return value —
        # including post_ingestion_progress, the broadcast that tells the UI
        # an ingest finished — silently skipped it. The run then sits
        # non-terminal on the user's Ingestion Runs page forever, with this
        # one log line as the only trace.
        log.error(
            "progress.mark_completed FAILED — run %s is still NON-TERMINAL "
            "and its completion was not broadcast: %s", run_id, e,
            extra={"run_id": run_id, "alert": True},
        )
        return None


async def mark_failed_by_run(
    *,
    run_id: str,
    stage: str | None = None,
    error: str,
) -> bool:
    """Terminal write — sets status=failed via conditional update.

    Records current_stage so the IngestionRuns UI can show "failed at
    persist" instead of just "failed". Returns True iff the row actually
    transitioned.
    """
    sql = f"""
        UPDATE silver.ingest_progress
        SET status        = 'failed',
            current_step  = 'failed',
            current_stage = COALESCE($2, current_stage),
            failed_at     = now(),
            updated_at    = now(),
            error_text    = $3
        WHERE run_id = $1::uuid
          AND status NOT IN ({TERMINAL_STATUS_SQL})
        RETURNING run_id, triggered_by,
                  EXTRACT(EPOCH FROM (now() - started_at))::float AS duration_seconds
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, run_id, stage, (error or "")[:2000])
        transitioned = row is not None
        if not transitioned:
            log.info(
                "progress.mark_failed: no-op (already terminal) run=%s",
                run_id, extra={"run_id": run_id},
            )
            return False
        _record_terminal_metrics(
            status="failed",
            triggered_by=row["triggered_by"] or "upload",
            duration_seconds=float(row["duration_seconds"] or 0.0),
        )
        return True
    except Exception as e:
        log.warning(
            "progress.mark_failed failed (run=%s): %s", run_id, e,
            extra={"run_id": run_id},
        )
        return False


async def mark_timed_out(*, run_id: str, reason: str = "stale_heartbeat") -> bool:
    """Terminal write — sets status=timed_out via conditional update.

    Called by the 15-min stale_run_detector cron when a row has been in
    'started' state without a recent heartbeat.
    """
    sql = f"""
        UPDATE silver.ingest_progress
        SET status        = 'timed_out',
            current_step  = 'failed',
            failed_at     = now(),
            updated_at    = now(),
            error_text    = $2
        WHERE run_id = $1::uuid
          AND status NOT IN ({TERMINAL_STATUS_SQL})
        RETURNING run_id, triggered_by,
                  EXTRACT(EPOCH FROM (now() - started_at))::float AS duration_seconds
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                run_id,
                f'{{"reason":"{reason}","detected_by":"stale_run_sweep"}}',
            )
        if row is None:
            return False
        _record_terminal_metrics(
            status="timed_out",
            triggered_by=row["triggered_by"] or "upload",
            duration_seconds=float(row["duration_seconds"] or 0.0),
        )
        # Counter for the dedicated stale-run alert.
        try:
            from app.metrics import INGESTION_STALE_RUNS_TOTAL
            INGESTION_STALE_RUNS_TOTAL.inc()
        except Exception:
            pass
        return True
    except Exception as e:
        log.warning(
            "progress.mark_timed_out failed (run=%s): %s", run_id, e,
            extra={"run_id": run_id},
        )
        return False


async def mark_cancelled(*, run_id: str, reason: str = "user_cancelled") -> bool:
    """Terminal write — sets status=cancelled. Used by the on_failure_task hook
    when Hatchet cancels a workflow (concurrency expiry, explicit cancel)."""
    sql = f"""
        UPDATE silver.ingest_progress
        SET status        = 'cancelled',
            current_step  = 'failed',
            failed_at     = now(),
            updated_at    = now(),
            error_text    = $2
        WHERE run_id = $1::uuid
          AND status NOT IN ({TERMINAL_STATUS_SQL})
        RETURNING run_id
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, run_id, reason[:2000])
        return row is not None
    except Exception as e:
        log.warning(
            "progress.mark_cancelled failed (run=%s): %s", run_id, e,
            extra={"run_id": run_id},
        )
        return False


async def lookup_active_run_id(
    *,
    workspace_id: str,
    minio_key: str,
) -> str | None:
    """Return the run_id of the active (non-terminal) row for this file, if any.

    Used by the backward-compat shims below — task code that only knows
    (workspace_id, minio_key) can resolve to the per-run id without
    threading it through every workflow output.
    """
    sql = f"""
        SELECT run_id::text AS run_id
        FROM silver.ingest_progress
        WHERE workspace_id = $1::uuid AND minio_key = $2
          AND status NOT IN ({TERMINAL_STATUS_SQL})
        ORDER BY attempt_number DESC, started_at DESC
        LIMIT 1
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, workspace_id, minio_key)
        return row["run_id"] if row else None
    except Exception as e:
        log.warning(
            "progress.lookup_active_run_id failed (key=%s): %s", minio_key, e,
            extra={"workspace_id": workspace_id, "minio_key": minio_key},
        )
        return None


async def get_run(*, run_id: str) -> dict | None:
    """Read a single run row by run_id. Used by the on_failure_task hook to
    resolve current_stage when reporting the failure upstream."""
    sql = """
        SELECT
            run_id::text AS run_id,
            workspace_id::text AS workspace_id,
            project_id::text AS project_id,
            minio_key,
            filename,
            status,
            current_stage,
            current_step,
            step_index,
            total_steps,
            started_at,
            completed_at,
            failed_at,
            error_text,
            attempt_number,
            triggered_by,
            parent_run_id::text AS parent_run_id,
            recovery_reason,
            -- The three columns the TERMINAL write sets. They were absent
            -- from this SELECT, so nothing could read back what
            -- mark_completed_by_run had just written: report_id (which the
            -- completion links the run to), rows_written, and warnings —
            -- the field whose whole purpose is to carry actionable text
            -- ("upload the collar file first") out to the Ingestion Runs
            -- page. Additive; every existing caller keys by name.
            report_id::text AS report_id,
            rows_written,
            warnings
        FROM silver.ingest_progress
        WHERE run_id = $1::uuid
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, run_id)
        return dict(row) if row else None
    except Exception as e:
        log.warning(
            "progress.get_run failed (run=%s): %s", run_id, e,
            extra={"run_id": run_id},
        )
        return None


# ---------------------------------------------------------------------------
# Backward-compat shims — keep existing ingest_pdf.py / tiff_normalize.py
# call sites working until they're migrated to the per-run API.
# ---------------------------------------------------------------------------
async def mark_started(
    *,
    workspace_id: str,
    project_id: str,
    minio_key: str,
    step: str,
    workflow_run_id: str | None = None,
) -> None:
    """LEGACY shim — resolves to the active run_id and calls mark_stage_started.

    If no active run exists (first call for this file), creates one with
    triggered_by='upload'. This preserves the original "one helper, one
    side effect" contract while threading through the per-run schema.
    """
    run_id = await lookup_active_run_id(workspace_id=workspace_id, minio_key=minio_key)
    if run_id is None:
        run_id = await start_run(
            workspace_id=workspace_id,
            project_id=project_id,
            minio_key=minio_key,
            workflow_run_id=workflow_run_id,
        )
        if run_id is None:
            return  # DB failure — best-effort
    await mark_stage_started(run_id=run_id, stage=step)


async def mark_completed(
    *,
    workspace_id: str,
    minio_key: str,
    report_id: str | None = None,
) -> None:
    """LEGACY shim — resolves to the active run_id and calls
    mark_completed_by_run. Preserves the existing ingest_pdf.py /
    tiff_normalize.py call signature.
    """
    run_id = await lookup_active_run_id(workspace_id=workspace_id, minio_key=minio_key)
    if run_id is None:
        log.warning(
            "progress.mark_completed (legacy): no active run for (ws=%s, key=%s) — skipping",
            workspace_id, minio_key,
            extra={"workspace_id": workspace_id, "minio_key": minio_key},
        )
        return
    await mark_completed_by_run(run_id=run_id, report_id=report_id)


async def mark_failed(
    *,
    workspace_id: str,
    minio_key: str,
    error: str,
    stage: str | None = None,
) -> None:
    """LEGACY shim — resolves to the active run_id and calls
    mark_failed_by_run. Preserves the existing ingest_pdf.py /
    tiff_normalize.py call signature.
    """
    run_id = await lookup_active_run_id(workspace_id=workspace_id, minio_key=minio_key)
    if run_id is None:
        log.warning(
            "progress.mark_failed (legacy): no active run for (ws=%s, key=%s) — skipping",
            workspace_id, minio_key,
            extra={"workspace_id": workspace_id, "minio_key": minio_key},
        )
        return
    await mark_failed_by_run(run_id=run_id, stage=stage, error=error)


async def close_run_after_workflow_failure(
    *,
    workflow_name: str,
    workspace_id: str | None,
    project_id: str | None,
    minio_key: str | None,
    run_id: str | None,
    ctx: object | None,
) -> dict:
    """Drive an ingest_progress row terminal from a workflow's failure hook.

    Hatchet's ``on_failure_task`` is the ONLY thing that fires when the
    engine cancels a workflow before its body runs — concurrency-queue
    expiry, a manual cancel, a worker SIGTERM. In that case ``start_run``
    has created a row and no task ever reaches the code that would close
    it, so the row sits at 'queued' until the 15-minute stale sweep finds
    it. That is the exact Cameco failure mode (529 runs silently CANCELLED)
    the hooks on ingest_pdf / ingest_zip_archive / tiff_normalize were added
    for.

    Shared rather than copied because it already exists three times with
    small divergences; the three geology workflows call this instead of
    growing a fourth, fifth and sixth variant. (The three older hooks
    predate it and still carry their own copies.)

    Safe to call when the body already closed the row: ``mark_failed_by_run``
    is a conditional update that no-ops on a terminal row and returns False.
    """
    resolved = run_id
    if resolved is None and workspace_id and minio_key:
        resolved = await lookup_active_run_id(
            workspace_id=workspace_id, minio_key=minio_key,
        )
    if resolved is None:
        log.warning(
            "%s.on_failure: no active run for (ws=%s, key=%s) — the body never "
            "reached start_run, so a cancellation fired before dispatch",
            workflow_name, workspace_id, minio_key,
            extra={
                "workflow": workflow_name,
                "workspace_id": workspace_id,
                "minio_key": minio_key,
            },
        )
        return {"updated": False, "reason": "no_active_run"}

    row = await get_run(run_id=resolved)
    current_stage = (row or {}).get("current_stage") or "unknown"

    # Hatchet's own per-task error map, populated specifically for use
    # inside an on_failure hook. Without it every failure — a real bug, a
    # worker restart, a cancellation — records the same uninformative
    # string, and root-causing a recurring failure from the Ingestion Runs
    # UI alone becomes impossible.
    try:
        task_errors = getattr(ctx, "task_run_errors", None) or {}
    except Exception as exc:  # noqa: BLE001 — diagnostics must not block the hook
        log.warning(
            "%s.on_failure: could not read task_run_errors: %s", workflow_name, exc,
            extra={"workflow": workflow_name, "run_id": resolved},
        )
        task_errors = {}
    if task_errors:
        error_detail = "; ".join(f"{name}: {msg}" for name, msg in task_errors.items())
    else:
        error_detail = (
            "no task_run_errors available (worker crash/cancellation with no "
            "captured exception)"
        )

    transitioned = await mark_failed_by_run(
        run_id=resolved, stage=current_stage, error=error_detail,
    )

    if transitioned and project_id and workspace_id:
        try:
            from app.services.laravel_bridge import (  # noqa: PLC0415
                post_ingestion_progress,
            )

            await post_ingestion_progress(
                workspace_id=workspace_id,
                project_id=project_id,
                run_id=resolved,
                stage=current_stage,
                status="failed",
                message="Workflow exhausted retries or was cancelled.",
            )
        except Exception as exc:
            log.warning(
                "%s.on_failure: broadcast failed run=%s: %s",
                workflow_name, resolved, exc,
                extra={"workflow": workflow_name, "run_id": resolved},
            )

    return {
        "updated": transitioned,
        "run_id": resolved,
        "current_stage": current_stage,
    }
