"""Tests for the Phase 3 orphan-sweep helpers.

Locks the spec's recovery-run invariants:

  - Sweep creates new silver.ingest_progress rows linked via
    parent_run_id (never mutates existing terminal rows).
  - Recovery row carries the spec's required metadata:
    triggered_by='embed_pending_sweep',
    recovery_reason='embedding_id_null',
    attempt_number = parent attempt + 1.
  - Per-document advisory lock prevents two concurrent sweep instances
    from double-dispatching the same document.
  - Documents whose passages are all younger than 5 min are excluded
    (still inside the persist→embed_verify happy path window).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import asyncpg
import pytest

if not os.environ.get("POSTGRES_USER"):
    pytest.skip("postgres env not configured", allow_module_level=True)

from app.hatchet_workflows import _progress as ingest_progress  # noqa: E402
from app.services.ingest.orphan_sweep import (  # noqa: E402
    claim_and_record_recovery,
    select_orphan_documents,
    try_claim_document,
    release_document,
)

_TEST_WORKSPACE = "a0000000-0000-0000-0000-0000000bd333"
_TEST_PROJECT = "b2000000-0000-0000-0000-0000000bd333"


@pytest.fixture(autouse=True)
async def _reset_pool():
    if ingest_progress._pool is not None:
        try:
            await ingest_progress._pool.close()
        except Exception:
            pass
        ingest_progress._pool = None
    yield
    if ingest_progress._pool is not None:
        try:
            await ingest_progress._pool.close()
        except Exception:
            pass
        ingest_progress._pool = None


async def _ensure_workspace_and_project():
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            """
            INSERT INTO silver.workspaces (workspace_id, name, slug)
            VALUES ($1::uuid, 'orphan-sweep-tests', 'orphan-' || substring($1::text from 1 for 8))
            ON CONFLICT (workspace_id) DO NOTHING
            """,
            _TEST_WORKSPACE,
        )
        await conn.execute(
            """
            INSERT INTO silver.projects (
                project_id, project_name, slug, workspace_id,
                crs_datum, orientation_reference, status
            ) VALUES (
                $1::uuid, 'orphan-sweep-tests',
                'orphan-sweep-' || substring($1::text from 1 for 8),
                $2::uuid, 'EPSG:4326', 'grid', 'active'
            )
            ON CONFLICT (project_id) DO NOTHING
            """,
            _TEST_PROJECT, _TEST_WORKSPACE,
        )
    finally:
        await conn.close()


async def _make_report_with_orphan_passages(
    *,
    minio_key: str,
    n_passages: int = 3,
    passage_age_minutes: int = 10,
) -> tuple[str, str]:
    """Create a silver.reports row + N un-embedded passages predated to
    look orphaned. Returns (report_id, minio_key)."""
    report_id = str(uuid.uuid4())
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            """
            INSERT INTO silver.reports (
                report_id, project_id, workspace_id, title,
                parser_used, parse_quality_pct, is_scanned, version, qp_name
            ) VALUES ($1::uuid, $2::uuid, $3::uuid, 'orphan-sweep-fixture',
                      'fitz', 0.5, false, 1, '{}'::text[])
            """,
            report_id, _TEST_PROJECT, _TEST_WORKSPACE,
        )
        for i in range(n_passages):
            text = f"orphan body {report_id} #{i}"
            await conn.execute(
                """
                INSERT INTO silver.document_passages (
                    passage_id, document_id, workspace_id, revision_number,
                    text, text_hash, ordinal, created_at, updated_at
                ) VALUES (
                    gen_random_uuid(), $1::uuid, $2::uuid, 1,
                    $3, $4, $5, now() - interval '1 minute' * $6, now()
                )
                """,
                report_id, _TEST_WORKSPACE,
                text, hashlib.sha256(text.encode()).hexdigest(), i,
                passage_age_minutes,
            )
    finally:
        await conn.close()
    return report_id, minio_key


async def _cleanup_fixture(report_id: str) -> None:
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        # ingest_progress doesn't have a FK to reports — delete by report_id.
        await conn.execute(
            "DELETE FROM silver.ingest_progress WHERE report_id = $1::uuid",
            report_id,
        )
        await conn.execute(
            "DELETE FROM silver.document_passages WHERE document_id = $1::uuid",
            report_id,
        )
        await conn.execute(
            "DELETE FROM silver.reports WHERE report_id = $1::uuid",
            report_id,
        )
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def _bootstrap():
    await _ensure_workspace_and_project()
    yield


# ---------------------------------------------------------------------------
# Orphan SELECT
# ---------------------------------------------------------------------------
async def test_select_orphan_documents_includes_old_unembedded_only():
    """Passages > 5 min old + NULL embedding_id appear; younger ones don't."""
    minio_key_old = f"reports/orphan/{uuid.uuid4()}_old.pdf"
    minio_key_young = f"reports/orphan/{uuid.uuid4()}_young.pdf"

    old_report, _ = await _make_report_with_orphan_passages(
        minio_key=minio_key_old, n_passages=2, passage_age_minutes=10,
    )
    young_report, _ = await _make_report_with_orphan_passages(
        minio_key=minio_key_young, n_passages=2, passage_age_minutes=1,
    )

    # Create the parent ingest_progress rows so the sweep's JOIN finds them.
    await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE, project_id=_TEST_PROJECT,
        minio_key=minio_key_old,
    )
    await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE, project_id=_TEST_PROJECT,
        minio_key=minio_key_young,
    )
    # Tie report → minio_key on the parent rows.
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            "UPDATE silver.ingest_progress SET report_id = $1::uuid "
            "WHERE minio_key = $2",
            old_report, minio_key_old,
        )
        await conn.execute(
            "UPDATE silver.ingest_progress SET report_id = $1::uuid "
            "WHERE minio_key = $2",
            young_report, minio_key_young,
        )

        orphans = await select_orphan_documents(conn)
    finally:
        await conn.close()

    orphan_doc_ids = {o.document_id for o in orphans}
    assert old_report in orphan_doc_ids, "10-min-old unembedded passages must be orphans"
    assert young_report not in orphan_doc_ids, \
        "1-min-old passages are still inside the persist→embed_verify window"

    await _cleanup_fixture(old_report)
    await _cleanup_fixture(young_report)


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------
async def test_advisory_lock_blocks_second_acquirer_on_different_connection():
    """T-spec-style: two concurrent sweep instances can't double-dispatch
    the same doc. The lock is session-scoped — a second connection
    requesting the same lock must fail to acquire."""
    pool = await ingest_progress.get_pool()
    doc_id = str(uuid.uuid4())

    async with pool.acquire() as conn_a, pool.acquire() as conn_b:
        assert await try_claim_document(conn_a, doc_id) is True
        # Second acquirer on a different session must NOT get the lock
        # while conn_a still holds it.
        assert await try_claim_document(conn_b, doc_id) is False
        # Release on conn_a; conn_b can now claim.
        await release_document(conn_a, doc_id)
        assert await try_claim_document(conn_b, doc_id) is True
        await release_document(conn_b, doc_id)


# ---------------------------------------------------------------------------
# Recovery run creation
# ---------------------------------------------------------------------------
async def test_sweep_creates_recovery_run_with_parent_lineage():
    """T-spec — recovery runs link to the original via parent_run_id and
    carry the spec's required metadata (triggered_by, recovery_reason,
    attempt_number)."""
    minio_key = f"reports/orphan/{uuid.uuid4()}_lineage.pdf"
    report_id, _ = await _make_report_with_orphan_passages(
        minio_key=minio_key, n_passages=3, passage_age_minutes=10,
    )

    parent_run_id = await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE, project_id=_TEST_PROJECT,
        minio_key=minio_key,
    )
    assert parent_run_id is not None

    # Tie the report_id onto the parent row so the orphan SELECT picks it up.
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            "UPDATE silver.ingest_progress SET report_id = $1::uuid "
            "WHERE run_id = $2::uuid",
            report_id, parent_run_id,
        )
    finally:
        await conn.close()

    # Mark the parent as completed (terminal) so we're testing the
    # "recovery after parent finished but embed failed" path. The
    # parent must stay immutable — recovery run is an attempt #2.
    await ingest_progress.mark_completed_by_run(run_id=parent_run_id)

    pool = await ingest_progress.get_pool()
    claimed, skipped = await claim_and_record_recovery(pool)

    # Find the claim for our report.
    our_claim = next(
        (c for c in claimed if c.orphan.document_id == report_id),
        None,
    )
    assert our_claim is not None, f"expected to find orphan {report_id} in claimed list"
    assert our_claim.recovery_run_id is not None

    # Inspect the recovery run.
    recovery_row = await ingest_progress.get_run(run_id=our_claim.recovery_run_id)
    assert recovery_row is not None
    assert recovery_row["triggered_by"] == "embed_pending_sweep"
    assert recovery_row["recovery_reason"] == "embedding_id_null"
    assert recovery_row["parent_run_id"] == parent_run_id
    assert recovery_row["attempt_number"] == 2  # parent was attempt 1
    assert recovery_row["status"] == "started"  # we advanced to 'embedding'
    assert recovery_row["current_stage"] == "embedding"

    # The parent's terminal state MUST remain immutable.
    parent_row = await ingest_progress.get_run(run_id=parent_run_id)
    assert parent_row is not None
    assert parent_row["status"] == "completed"

    await _cleanup_fixture(report_id)


async def test_sweep_skip_when_lock_held_concurrently():
    """If another sweep instance has already claimed a document's
    advisory lock, the current sweep adds it to `skipped` and creates
    NO recovery run."""
    minio_key = f"reports/orphan/{uuid.uuid4()}_lock_blocked.pdf"
    report_id, _ = await _make_report_with_orphan_passages(
        minio_key=minio_key, n_passages=2, passage_age_minutes=10,
    )

    parent_run_id = await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE, project_id=_TEST_PROJECT,
        minio_key=minio_key,
    )
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            "UPDATE silver.ingest_progress SET report_id = $1::uuid "
            "WHERE run_id = $2::uuid",
            report_id, parent_run_id,
        )
    finally:
        await conn.close()

    pool = await ingest_progress.get_pool()

    # Hold the lock on a separate connection — simulates another sweep
    # instance mid-claim.
    blocker = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        assert await try_claim_document(blocker, report_id) is True

        claimed, skipped = await claim_and_record_recovery(pool)

        our_claim = next(
            (c for c in claimed if c.orphan.document_id == report_id), None,
        )
        our_skip = next(
            (s for s in skipped if s.document_id == report_id), None,
        )
        assert our_claim is None, \
            "sweep must not have claimed a doc while blocker held the lock"
        assert our_skip is not None, "sweep must report the doc in skipped"

        # No recovery run was created for the blocked doc.
        check = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
        try:
            rows = await check.fetch(
                """
                SELECT run_id::text AS run_id
                FROM silver.ingest_progress
                WHERE report_id = $1::uuid
                  AND triggered_by = 'embed_pending_sweep'
                """,
                report_id,
            )
        finally:
            await check.close()
        assert len(rows) == 0
    finally:
        await release_document(blocker, report_id)
        await blocker.close()

    await _cleanup_fixture(report_id)
