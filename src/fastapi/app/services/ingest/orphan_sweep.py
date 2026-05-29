"""Phase 3 of the reliability spec — embed-pending orphan sweep helpers.

The existing ``embed_pending_passages_wf`` Hatchet workflow runs every 10
minutes, walking projects that have un-embedded passages and pushing
them to Qdrant. Phase 1 + the embed-verify dispatcher already cover the
happy path; this module adds the spec's recovery-tracking layer on top
of it:

  1. **Per-document advisory lock.** Before doing recovery work for a
     given document, claim ``pg_try_advisory_lock('embed_sweep:' || doc_id)``.
     This prevents two concurrent sweep instances (or a sweep tick racing
     the embed_verify dispatcher) from double-dispatching the same work.

  2. **Recovery run creation.** Every document we recover for gets a
     fresh row in ``silver.ingest_progress`` with ``triggered_by =
     'embed_pending_sweep'``, ``recovery_reason = 'embedding_id_null'``,
     and ``parent_run_id`` linking back to the original ingest's most
     recent run. Per the spec's core invariant, we never mutate
     terminal rows — recovery work is its own attempt.

The advisory locks released at sweep end (or on connection close, since
they're session-scoped). Recovery rows are not deleted on success; they
become an audit trail of every safety-net dispatch.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import asyncpg

from app.hatchet_workflows import _progress as ingest_progress

log = logging.getLogger("georag.ingest.orphan_sweep")


# Documents younger than this threshold are still in their initial
# persist→embed dispatch window — leave them alone. Five minutes matches
# the spec.
STALE_AFTER_INTERVAL = "5 minutes"


@dataclass
class OrphanDocument:
    document_id: str
    project_id: Optional[str]
    workspace_id: Optional[str]
    minio_key: Optional[str]
    orphan_count: int
    parent_run_id: Optional[str]
    parent_attempt_number: int
    parent_workspace_id: Optional[str]
    parent_project_id: Optional[str]
    parent_minio_key: Optional[str]


SELECT_ORPHANS_SQL = """
    SELECT
        dp.document_id::text                       AS document_id,
        r.project_id::text                         AS project_id,
        r.workspace_id::text                       AS workspace_id,
        COUNT(*)                                    AS orphan_count,
        (
            SELECT ip.run_id::text
            FROM silver.ingest_progress ip
            WHERE ip.workspace_id = r.workspace_id
              AND ip.report_id    = dp.document_id
            ORDER BY ip.attempt_number DESC, ip.started_at DESC
            LIMIT 1
        )                                           AS parent_run_id,
        (
            SELECT ip.attempt_number
            FROM silver.ingest_progress ip
            WHERE ip.workspace_id = r.workspace_id
              AND ip.report_id    = dp.document_id
            ORDER BY ip.attempt_number DESC, ip.started_at DESC
            LIMIT 1
        )                                           AS parent_attempt_number,
        (
            SELECT ip.minio_key
            FROM silver.ingest_progress ip
            WHERE ip.workspace_id = r.workspace_id
              AND ip.report_id    = dp.document_id
            ORDER BY ip.attempt_number DESC, ip.started_at DESC
            LIMIT 1
        )                                           AS parent_minio_key
    FROM silver.document_passages dp
    JOIN silver.reports r ON r.report_id = dp.document_id
    WHERE dp.embedding_id IS NULL
      AND dp.created_at   < now() - interval '5 minutes'
    GROUP BY dp.document_id, r.project_id, r.workspace_id
"""


async def select_orphan_documents(conn: asyncpg.Connection) -> list[OrphanDocument]:
    """Return documents with at least one un-embedded passage older than
    STALE_AFTER_INTERVAL, plus their most recent ingest_progress row for
    recovery linkage."""
    rows = await conn.fetch(SELECT_ORPHANS_SQL)
    return [
        OrphanDocument(
            document_id=r["document_id"],
            project_id=r["project_id"],
            workspace_id=r["workspace_id"],
            minio_key=r["parent_minio_key"],
            orphan_count=int(r["orphan_count"]),
            parent_run_id=r["parent_run_id"],
            parent_attempt_number=int(r["parent_attempt_number"] or 0),
            parent_workspace_id=r["workspace_id"],
            parent_project_id=r["project_id"],
            parent_minio_key=r["parent_minio_key"],
        )
        for r in rows
    ]


async def try_claim_document(conn: asyncpg.Connection, document_id: str) -> bool:
    """Try to acquire the per-document advisory lock for sweep dispatch.

    Returns True iff the lock was acquired. Lock is session-scoped on
    the asyncpg connection that called it; release explicitly via
    ``release_document`` (lock auto-releases on connection close too).
    """
    lock_key = f"embed_sweep:{document_id}"
    row = await conn.fetchrow(
        "SELECT pg_try_advisory_lock(hashtext($1)::bigint) AS got",
        lock_key,
    )
    return bool(row and row["got"])


async def release_document(conn: asyncpg.Connection, document_id: str) -> None:
    """Release the per-document sweep lock. Best-effort — connection
    close also frees it."""
    lock_key = f"embed_sweep:{document_id}"
    try:
        await conn.execute(
            "SELECT pg_advisory_unlock(hashtext($1)::bigint)", lock_key,
        )
    except Exception as exc:
        log.debug("orphan_sweep: unlock failed doc=%s err=%s", document_id, exc)


async def create_recovery_run(orphan: OrphanDocument) -> Optional[str]:
    """Create a new silver.ingest_progress row for this recovery attempt.

    Per the spec's invariant: recovery work always creates new rows
    linked via parent_run_id, never mutates existing terminal rows.

    Returns the new run_id, or None if we don't have enough
    information (missing minio_key or workspace_id, e.g. a legacy
    document with no progress row at all).
    """
    if not orphan.workspace_id or not orphan.project_id or not orphan.minio_key:
        log.info(
            "orphan_sweep: skipping recovery-run creation (missing scope) doc=%s",
            orphan.document_id,
        )
        return None

    run_id = await ingest_progress.start_run(
        workspace_id=orphan.workspace_id,
        project_id=orphan.project_id,
        minio_key=orphan.minio_key,
        triggered_by="embed_pending_sweep",
        recovery_reason="embedding_id_null",
        parent_run_id=orphan.parent_run_id,
    )
    if run_id:
        # Advance the recovery row to the 'embedding' stage so the
        # ingest_progress sweep at the end of the workflow flips it to
        # completed once its project's passages are fully embedded.
        await ingest_progress.mark_stage_started(
            run_id=run_id, stage="embedding", worker_id="embed_pending_sweep",
        )
        log.info(
            "orphan_sweep: recovery run created doc=%s parent=%s new_run=%s",
            orphan.document_id, orphan.parent_run_id, run_id,
        )
    return run_id


@dataclass
class SweepClaim:
    """One document successfully claimed by the sweep."""
    orphan: OrphanDocument
    recovery_run_id: Optional[str]


async def claim_and_record_recovery(
    pool: asyncpg.Pool,
) -> tuple[list[SweepClaim], list[OrphanDocument]]:
    """Walk orphan documents, claim advisory locks, create recovery runs.

    Returns ``(claimed, skipped)`` where ``claimed`` are documents we
    successfully locked + recovered for, and ``skipped`` are documents
    where another sweep already holds the lock.

    The returned connection has the advisory locks open — caller MUST
    invoke ``release_all_claims`` on it (passing it the same list) once
    embed work completes, OR allow the connection to be returned to the
    pool which will auto-release on connection close. We model this as a
    context-manager-friendly helper in the workflow.
    """
    claimed: list[SweepClaim] = []
    skipped: list[OrphanDocument] = []

    async with pool.acquire() as conn:
        orphans = await select_orphan_documents(conn)

        for orphan in orphans:
            got = await try_claim_document(conn, orphan.document_id)
            if not got:
                skipped.append(orphan)
                continue

            try:
                recovery_run_id = await create_recovery_run(orphan)
            except Exception as exc:
                log.warning(
                    "orphan_sweep: recovery-run creation threw doc=%s err=%s",
                    orphan.document_id, exc,
                )
                await release_document(conn, orphan.document_id)
                continue

            claimed.append(SweepClaim(orphan=orphan, recovery_run_id=recovery_run_id))

        # Release every advisory lock we acquired BEFORE returning the
        # connection to the pool. This is important because the
        # subsequent per-project embed loop uses a *different* asyncpg
        # connection — if we held the locks on this one until it was
        # destroyed, the embed loop's persist task could not re-claim.
        for claim in claimed:
            await release_document(conn, claim.orphan.document_id)

    return claimed, skipped
