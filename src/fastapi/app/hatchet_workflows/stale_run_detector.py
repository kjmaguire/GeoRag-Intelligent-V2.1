"""Stale-run detector cron — Phase 1 / Fix 1e of the reliability spec.

Every 15 minutes, scan silver.ingest_progress for rows that are
``status='started'`` but whose ``last_heartbeat_at`` is older than 15
minutes. These are the carcasses of:

  - Hatchet worker crashes (SIGKILL, OOM, Docker restart)
  - Hung task subprocess that never raised but never returned
  - Concurrency-cancelled runs that didn't fire on_failure_task

For each stale candidate we apply one of three resolutions:

  1. **Race recovery** — if the run is at ``embed_verify``/``embedding`` and
     the project actually has zero unembedded passages, the embed already
     finished but nothing flipped ``status='completed'`` (the embed
     completion sweep races against the heartbeat clock). Mark completed
     instead of timing out so the UI reflects reality.
  2. **Retry dispatch** — if the run died at ``preflight``/``parse``/
     ``persist`` (the actual file-processing stages) AND we have not
     already retried it ``RECOVERY_MAX_ATTEMPTS`` times, mark this row
     ``timed_out`` and spawn a fresh run of the workflow that OWNS the
     file (routed from the bronze key prefix - see
     ``recovery_workflow_for_key``; a key we cannot route gets no retry)
     with ``triggered_by='stale_run_sweep'`` and ``parent_run_id`` set. This
     gives observable lineage: every retry is an auditable attempt with a
     known parent + reason. ``attempt_number`` is derived server-side
     inside ``start_run`` so the cap is enforced even with concurrent
     sweep instances.
  3. **Mark timed_out** — every other case (out of retries, terminal
     stage that we can't recover, etc.) just marks the row terminal so
     the UI flips and the alert metrics fire.

The conditional update inside ``mark_timed_out`` / ``mark_completed_by_run``
silently no-ops if the row already transitioned (e.g. on_failure_task beat
us to it). This is the durable backstop for Bug 1 (silent stalls).

See [[ingestion-reliability-spec]] for the full state-machine contract.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows import hatchet
from app.services.laravel_bridge import post_ingestion_progress

log = logging.getLogger("georag.hatchet.stale_run_detector")


# Default 15 minutes — matches the spec. Configurable via env so we can
# tighten it in tests without redeploying.
def _stale_after_minutes() -> int:
    raw = os.environ.get("STALE_RUN_DETECTOR_MINUTES", "15")
    try:
        v = int(raw)
        return v if v > 0 else 15
    except ValueError:
        return 15


def _embed_stale_after_minutes() -> int:
    """Longer staleness window for rows at embedding/embed_verify (F2b).

    Embeds serialize per workspace (embed_pending_passages max_runs=1 with
    GROUP_ROUND_ROBIN), so on a bulk import a row can legitimately sit at
    'embedding' far longer than 15 minutes while other documents' embeds
    drain the queue. Parse/persist keep the tight window.
    """
    raw = os.environ.get("STALE_RUN_DETECTOR_EMBED_MINUTES", "120")
    try:
        v = int(raw)
        return v if v > 0 else 120
    except ValueError:
        return 120


def _recovery_max_attempts() -> int:
    """Cap the parent-chain depth so a chronically broken file can't loop forever.

    Counts the attempt_number of the doomed row; 3 means: original
    upload + 2 sweep-driven retries before we give up and just leave the
    row timed_out for manual investigation.

    Delegates rather than re-reading the env var: orphan_sweep needs the
    same cap (it had none until 2026-08-22), and two independent readings
    of one variable, each with its own fallback, is exactly how the two
    sweeps would end up disagreeing about when to stop.
    """
    return ingest_progress.recovery_max_attempts()


# Stages where a stale heartbeat genuinely means the parse work was lost
# and a re-dispatch will produce useful progress. Stages downstream of
# persist already have rows in silver.reports; re-running the ingest
# workflow for them would just duplicate work or hit the dedupe path. For
# embed_verify/embedding the embed_pending_passages cron is the recovery
# path, not a re-ingest.
#
# These stage names are shared by EVERY ingest workflow, which is why
# _RECOVERY_WORKFLOW_BY_PREFIX below exists: knowing the run is stale at
# 'parse' says nothing about what kind of file it is.
RETRY_STAGES: frozenset[str] = frozenset({"preflight", "parse", "persist"})


# Which workflow owns a bronze key, keyed by its first path segment.
#
# Every ingest workflow writes the SAME stage names ("preflight", "parse",
# "persist") into silver.ingest_progress, and the row does not record which
# workflow wrote them. The sweep used to read a stale row at one of those
# stages and unconditionally dispatch ingest_pdf - so a GeoPackage that
# wedged in GDAL was "recovered" by handing it to the PDF parser, which
# downloaded it, found no %PDF- magic and failed the recovery run with
# `preflight_rejected: missing %PDF- magic bytes`. The user was left with two
# failed rows, the second carrying a reason that had nothing to do with their
# file, and the upload was never retried by the workflow that could actually
# read it.
#
# The prefixes are the ones Laravel's UploadController mints
# (`{category}/{project_id}/{ts}_{name}`, with `tiff` and `tabular` as the two
# non-category prefixes) - see UploadController::bronzePrefixes(). A prefix
# missing from this map gets no recovery attempt at all, which is the
# behaviour we want for a key shape nobody here recognises: marking the row
# timed_out and stopping is honest, while dispatching a guessed workflow
# manufactures a second failure and a misleading error message.
#
# Re-dispatch is safe for all of these. ingest_pdf and tiff_normalize dedupe
# on source_file_sha256; ingest_spatial, ingest_tabular and ingest_well_logs
# all delete-then-insert scoped to (project, source_file) in one transaction,
# so a re-run replaces rather than accumulates.
_RECOVERY_WORKFLOW_BY_PREFIX: dict[str, str] = {
    "reports": "ingest_pdf",
    "tiff": "tiff_normalize",
    "archive": "ingest_zip_archive",
    "spatial": "ingest_spatial",
    "well_logs": "ingest_well_logs",
    # ingest_tabular handles both the typed drill categories and the two
    # generic workbook prefixes. For the typed ones the prefix IS the
    # sheet_type hint the geologist chose at upload time, and passing it back
    # matters: a CSV whose headers do not self-identify only routed correctly
    # the first time because of that hint.
    "collars": "ingest_tabular",
    "surveys": "ingest_tabular",
    "lithology": "ingest_tabular",
    "samples": "ingest_tabular",
    "excel": "ingest_tabular",
    "tabular": "ingest_tabular",
    # 2026-08-23: standalone .dbf. A dBASE table with no same-stem .shp is an
    # attribute table, not a shapefile sidecar, and lands in
    # silver.attribute_tables via ingest_tabular. It needs an entry here for
    # the same reason every other category does — without one the sweep marks
    # a stalled upload timed_out and never retries it.
    "tables": "ingest_tabular",
}

#: Prefixes that are also a sheet_type hint for ingest_tabular.
_TABULAR_SHEET_TYPE_PREFIXES: frozenset[str] = frozenset(
    {"collars", "surveys", "lithology", "samples"}
)


def _key_prefix(minio_key: str | None) -> str:
    """First path segment of a bronze key, or '' when there isn't one."""
    if not minio_key:
        return ""
    key = minio_key.lstrip("/")
    # Some call sites carry the bucket name; the prefix we want is the one
    # after it.
    if key.startswith("bronze/"):
        key = key[len("bronze/"):]
    head, _, rest = key.partition("/")
    return head if rest else ""


def recovery_workflow_for_key(minio_key: str | None) -> str | None:
    """Name of the workflow that should re-run ``minio_key``, or None.

    None means "do not attempt recovery" - see _RECOVERY_WORKFLOW_BY_PREFIX.
    """
    return _RECOVERY_WORKFLOW_BY_PREFIX.get(_key_prefix(minio_key))


def recoverable_bronze_prefixes() -> frozenset[str]:
    """Every bronze prefix this module can route to a workflow.

    Exported for the nightly bronze sweep, which needs the same answer one
    step earlier: it filters unroutable prefixes out of its orphan SELECT
    rather than examining and re-noting them every night forever. Two
    sweeps, one routing table.
    """
    return frozenset(_RECOVERY_WORKFLOW_BY_PREFIX)


class StaleRunDetectorInput(BaseModel):
    stale_minutes: int = Field(
        default=15, ge=1, le=240,
        description="Mark a 'started' run timed_out if last_heartbeat is "
                    "older than this many minutes.",
    )


class StaleRunDetectorOutput(BaseModel):
    runs_scanned: int
    runs_marked_completed: int = 0
    runs_marked_timed_out: int
    recovery_runs_dispatched: int = 0
    broadcasts_emitted: int
    sampled_at: datetime


stale_run_detector = hatchet.workflow(
    name="stale_run_detector",
    on_crons=["*/15 * * * *"],
    input_validator=StaleRunDetectorInput,
)


# F3 (2026-08-11) — keep in lockstep with the eligibility predicate in
# app/services/ingest/passage_embedder.py (embed_pending_passages SELECT):
# passages with ocr_status 'rejected'/'pending_reocr' are never embedded,
# so counting them as "unembedded" would keep runs un-completable forever.
_EMBEDDABLE_OCR_PREDICATE = (
    "(p.ocr_status IS NULL OR p.ocr_status NOT IN ('rejected', 'pending_reocr'))"
)


async def _project_is_fully_embedded(
    pool, project_id: str | None, report_id: str | None = None,
) -> bool:
    """True when every embeddable passage in scope has an embedding_id.

    F2 (2026-08-11): when the run row carries a report_id, scope the test
    to that run's OWN document — embeds serialize per workspace, so a
    project-wide test held rows hostage to every other document in a bulk
    import. NULL report_id (row died before persist, or legacy rows) falls
    back to the original project-wide behaviour.

    Returns False on any DB error so we err on the side of timing out
    (the safe, observable outcome) rather than silently marking
    completed.
    """
    if not project_id and not report_id:
        return False
    try:
        async with pool.acquire() as conn:
            if report_id:
                row = await conn.fetchrow(
                    f"""
                    SELECT EXISTS (
                        SELECT 1
                        FROM silver.document_passages p
                        WHERE p.document_id = $1::uuid
                          AND p.embedding_id IS NULL
                          AND {_EMBEDDABLE_OCR_PREDICATE}
                    ) AS has_unembedded
                    """,
                    report_id,
                )
            else:
                row = await conn.fetchrow(
                    f"""
                    SELECT EXISTS (
                        SELECT 1
                        FROM silver.document_passages p
                        JOIN silver.reports r ON r.report_id = p.document_id
                        WHERE r.project_id = $1::uuid
                          AND p.embedding_id IS NULL
                          AND {_EMBEDDABLE_OCR_PREDICATE}
                    ) AS has_unembedded
                    """,
                    project_id,
                )
        return not bool(row and row["has_unembedded"])
    except Exception as exc:
        log.warning(
            "stale_run_detector: unembedded-check failed for project=%s: %s",
            project_id, exc,
        )
        return False


def _build_recovery_payload(
    *,
    workflow_name: str,
    stale_row: dict,
    recovery_run_id: str,
    correlation_token: str,
):
    """Return ``(workflow, input_model)`` for a recovery dispatch.

    Two shapes of workflow live here, and the difference is how each one
    finds its progress row:

    * ingest_pdf and tiff_normalize take no ``run_id``. They call
      `lookup_active_run_id(workspace_id, minio_key)` and adopt whichever
      non-terminal row they find - which is the one `start_run` created a
      moment ago.
    * the three geology workflows and ingest_zip_archive take ``run_id``
      explicitly and upsert the row under it.

    ``file_size`` is informational for the PDF/TIFF pair: preflight
    re-downloads and re-derives the real size against the 2 GB cap, so 0 is
    safe here. (input.file_size has no other reference in either module.)
    """
    workspace_id = stale_row["workspace_id"]
    project_id = stale_row["project_id"]
    minio_key = stale_row["minio_key"]

    if workflow_name == "ingest_pdf":
        from app.hatchet_workflows.ingest_pdf import IngestPdfInput, ingest_pdf

        return ingest_pdf, IngestPdfInput(
            workspace_id=workspace_id,
            project_id=project_id,
            minio_key=minio_key,
            file_size=0,
            correlation_token=correlation_token,
        )

    if workflow_name == "tiff_normalize":
        from app.hatchet_workflows.tiff_normalize import (
            TiffNormalizeInput,
            tiff_normalize,
        )

        return tiff_normalize, TiffNormalizeInput(
            workspace_id=workspace_id,
            project_id=project_id,
            minio_key=minio_key,
            file_size=0,
            correlation_token=correlation_token,
        )

    if workflow_name == "ingest_zip_archive":
        from app.hatchet_workflows.ingest_zip_archive import (
            IngestZipArchiveInput,
            ingest_zip_archive,
        )

        return ingest_zip_archive, IngestZipArchiveInput(
            workspace_id=workspace_id,
            project_id=project_id,
            minio_key=minio_key,
            run_id=recovery_run_id,
        )

    if workflow_name == "ingest_spatial":
        from app.hatchet_workflows.ingest_spatial import (
            IngestSpatialInput,
            ingest_spatial,
        )

        return ingest_spatial, IngestSpatialInput(
            workspace_id=workspace_id,
            project_id=project_id,
            minio_key=minio_key,
            run_id=recovery_run_id,
        )

    if workflow_name == "ingest_well_logs":
        from app.hatchet_workflows.ingest_well_logs import (
            IngestWellLogsInput,
            ingest_well_logs,
        )

        return ingest_well_logs, IngestWellLogsInput(
            workspace_id=workspace_id,
            project_id=project_id,
            minio_key=minio_key,
            run_id=recovery_run_id,
        )

    if workflow_name == "ingest_tabular":
        from app.hatchet_workflows.ingest_tabular import (
            IngestTabularInput,
            ingest_tabular,
        )

        prefix = _key_prefix(minio_key)
        return ingest_tabular, IngestTabularInput(
            workspace_id=workspace_id,
            project_id=project_id,
            minio_key=minio_key,
            run_id=recovery_run_id,
            sheet_type=(
                prefix if prefix in _TABULAR_SHEET_TYPE_PREFIXES else None
            ),
        )

    # Unreachable while this stays in lockstep with
    # _RECOVERY_WORKFLOW_BY_PREFIX; raising rather than silently returning
    # None means a new prefix added to the map without a branch here shows up
    # as a logged dispatch failure instead of a run that is quietly never
    # recovered.
    raise ValueError(f"no recovery payload builder for workflow {workflow_name!r}")


async def _dispatch_recovery_run(
    *,
    stale_row: dict,
) -> str | None:
    """Re-run the workflow that OWNS this file, tied to the stale row.

    Which workflow that is comes from the bronze key prefix; see
    `recovery_workflow_for_key`. Returns the new run_id on success, None on
    any failure or when the key belongs to no known workflow (caller falls
    back to plain timed_out without recovery).
    """
    workflow_name = recovery_workflow_for_key(stale_row.get("minio_key"))
    if workflow_name is None:
        log.info(
            "stale_run_detector: no recovery workflow for key=%s — leaving "
            "run=%s timed_out without a retry",
            stale_row.get("minio_key"), stale_row.get("run_id"),
        )
        return None

    try:
        # Imported lazily — keeps the stale_run_detector worker bootable
        # even when one of the ingest workflows has an import-time error.
        from uuid import uuid4

        # Reserve the per-run row BEFORE dispatching, so on_failure_task
        # (if dispatch fails immediately) can still find a row to update.
        recovery_run_id = await ingest_progress.start_run(
            workspace_id=stale_row["workspace_id"],
            project_id=stale_row["project_id"],
            minio_key=stale_row["minio_key"],
            triggered_by="stale_run_sweep",
            parent_run_id=stale_row["run_id"],
            recovery_reason="stale_heartbeat",
        )
        if recovery_run_id is None:
            log.warning(
                "stale_run_detector: start_run returned None — skipping "
                "recovery dispatch for run=%s", stale_row["run_id"],
            )
            return None

        workflow, payload = _build_recovery_payload(
            workflow_name=workflow_name,
            stale_row=stale_row,
            recovery_run_id=recovery_run_id,
            correlation_token=f"stale-sweep-{uuid4()}",
        )
        ref = await workflow.aio_run_no_wait(payload)
        log.info(
            "stale_run_detector: dispatched recovery %s "
            "parent=%s recovery=%s workflow_run_id=%s key=%s",
            workflow_name, stale_row["run_id"], recovery_run_id,
            ref.workflow_run_id, stale_row["minio_key"],
        )
        return recovery_run_id
    except Exception as exc:
        log.warning(
            "stale_run_detector: recovery dispatch failed for run=%s key=%s "
            "workflow=%s: %s",
            stale_row["run_id"], stale_row.get("minio_key"), workflow_name, exc,
        )
        return None


@stale_run_detector.task(execution_timeout="2m", schedule_timeout="1h", retries=1)
async def detect(input: StaleRunDetectorInput, ctx: Context) -> StaleRunDetectorOutput:
    stale_minutes = input.stale_minutes or _stale_after_minutes()
    embed_stale_minutes = max(_embed_stale_after_minutes(), stale_minutes)
    max_attempts = _recovery_max_attempts()

    pool = await ingest_progress.get_pool()
    runs_marked_completed = 0
    runs_marked_timed_out = 0
    recovery_runs_dispatched = 0
    broadcasts_emitted = 0

    # Select runs to sweep. Done outside the per-row UPDATE so we can
    # log + broadcast each one individually without holding a long lock.
    # current_step is read (not current_stage) because it's the field
    # written by mark_stage_started — current_stage is set lazily by the
    # task body and is often NULL on rows that died early in a stage.
    # F5 (2026-08-11): 'queued' rows are included — a dispatch that never
    # reached preflight (worker down, Hatchet queue drop) previously left the
    # row queued forever with no heartbeat to age out. Age-test falls back to
    # started_at because queued rows never receive a heartbeat.
    # F2b (2026-08-11): embedding/embed_verify rows get the longer window —
    # embeds serialize per workspace, so a 15-min clock timed out healthy
    # bulk-import rows.
    select_sql = f"""
        SELECT run_id::text          AS run_id,
               workspace_id::text    AS workspace_id,
               project_id::text      AS project_id,
               report_id::text       AS report_id,
               minio_key,
               filename,
               current_stage,
               current_step,
               attempt_number,
               triggered_by
          FROM silver.ingest_progress
         WHERE status IN ('queued','started')
           AND COALESCE(last_heartbeat_at, started_at) < now()
               - CASE WHEN current_step IN ('embedding','embed_verify')
                      THEN interval '{int(embed_stale_minutes)} minutes'
                      ELSE interval '{int(stale_minutes)} minutes'
                 END
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(select_sql)

    log.info("stale_run_detector: %d candidate run(s) older than %dm", len(rows), stale_minutes)

    # Phase 6 — publish the active 'started' count gauge so the
    # IngestionRunStuckStarted alert can fire even when no transitions
    # happen on this tick.
    try:
        async with pool.acquire() as gauge_conn:
            # F5 — count queued rows too; they're now part of the sweep.
            active_row = await gauge_conn.fetchrow(
                "SELECT count(*)::int AS n FROM silver.ingest_progress "
                "WHERE status IN ('queued','started')"
            )
        from app.metrics import INGESTION_STALE_RUNS_DETECTED
        INGESTION_STALE_RUNS_DETECTED.set(int(active_row["n"]) if active_row else 0)
    except Exception:
        pass

    for row in rows:
        run_id = row["run_id"]
        current_step = row["current_step"] or "unknown"

        # Resolution 1 — race recovery. The embed completion sweep didn't
        # win the race against this 15-min tick, but the embeddings are
        # actually all in. Mark completed instead of timing out.
        if current_step in {"embed_verify", "embedding"} and \
                await _project_is_fully_embedded(
                    pool, row["project_id"], report_id=row["report_id"],
                ):
            transitioned = await ingest_progress.mark_completed_by_run(run_id=run_id)
            if transitioned:
                runs_marked_completed += 1
                if row["project_id"]:
                    try:
                        await post_ingestion_progress(
                            workspace_id=row["workspace_id"],
                            project_id=row["project_id"],
                            run_id=run_id,
                            stage="embedding",
                            status="completed",
                            message="Recovered by stale sweep — embeddings already complete.",
                        )
                        broadcasts_emitted += 1
                    except Exception as exc:
                        log.warning("stale_run_detector: race-recovery broadcast failed run=%s: %s", run_id, exc)
                log.info(
                    "stale_run_detector: race-recovered run=%s (project already fully embedded)",
                    run_id,
                )
                continue
            # Lost the race to another writer — fall through to the
            # normal terminal path below.

        # Resolution 2 — retry-eligible. Mark the doomed row timed_out
        # AND spawn a fresh ingest_pdf run linked by parent_run_id.
        # `recovery_workflow_for_key` is part of the predicate, not just of
        # the dispatch: a key we cannot route is not retry-eligible at all,
        # and deciding that here keeps `start_run` from minting a recovery
        # row that nothing will ever pick up.
        will_retry = bool(
            current_step in RETRY_STAGES
            and (row["attempt_number"] or 1) < max_attempts
            and row["minio_key"]
            and row["workspace_id"]
            and row["project_id"]
            and recovery_workflow_for_key(row["minio_key"])
        )

        # Resolution 3 (default) — mark timed_out. Always happens for
        # rows we decline to recover/retry, AND happens BEFORE the
        # recovery dispatch so the original row reaches its terminal
        # state regardless of dispatch success.
        transitioned = await ingest_progress.mark_timed_out(
            run_id=run_id, reason="stale_heartbeat",
        )
        if not transitioned:
            # Lost the race to on_failure_task or another sweep instance —
            # don't dispatch a recovery on a row someone else closed.
            continue
        runs_marked_timed_out += 1

        try:
            await post_ingestion_progress(
                workspace_id=row["workspace_id"],
                project_id=row["project_id"],
                run_id=run_id,
                stage=row["current_stage"] or current_step,
                status="timed_out",
                message=f"No heartbeat for {stale_minutes}m; marked timed_out by sweep.",
            )
            broadcasts_emitted += 1
        except Exception as e:
            log.warning("stale_run_detector: broadcast failed run=%s: %s", run_id, e)

        if will_retry:
            recovery_run_id = await _dispatch_recovery_run(stale_row=dict(row))
            if recovery_run_id is not None:
                recovery_runs_dispatched += 1

    return StaleRunDetectorOutput(
        runs_scanned=len(rows),
        runs_marked_completed=runs_marked_completed,
        runs_marked_timed_out=runs_marked_timed_out,
        recovery_runs_dispatched=recovery_runs_dispatched,
        broadcasts_emitted=broadcasts_emitted,
        sampled_at=datetime.utcnow(),
    )
