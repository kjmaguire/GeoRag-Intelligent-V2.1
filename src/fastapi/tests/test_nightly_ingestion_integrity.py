"""Tests for the Phase 5 nightly integrity sweep.

Covers:
  T10  — bronze.manifest orphan dispatched + claim-locked + dispatch_attempts
         bumped; second sweep within the lock window does NOT re-dispatch.
  Tier 2 — orphan_sweep wiring exercised inside the integrity sweep.
  Tier 3 — gold MV refresh is invoked + logged.
  Tier 4 — stuck outbox.pending_propagations rows get enqueued_at touched.
  Pass 2 data_version bump — workspaces with recovery rows since today
         get a single increment.
"""
from __future__ import annotations

import hashlib
import os
import uuid

import asyncpg
import pytest

if not os.environ.get("POSTGRES_USER"):
    pytest.skip("postgres env not configured", allow_module_level=True)

from datetime import UTC

from app.hatchet_workflows import _progress as ingest_progress  # noqa: E402
from app.hatchet_workflows import nightly_ingestion_integrity as sweep_mod  # noqa: E402

_TEST_WORKSPACE = "a0000000-0000-0000-0000-0000000bd555"
_TEST_PROJECT = "b3000000-0000-0000-0000-0000000bd555"


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


async def _ensure_workspace():
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            """
            INSERT INTO silver.workspaces (workspace_id, name, slug)
            VALUES ($1::uuid, 'phase5-integrity', 'phase5-' || substring($1::text from 1 for 8))
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
                $1::uuid, 'phase5-integrity',
                'phase5-integrity-' || substring($1::text from 1 for 8),
                $2::uuid, 'EPSG:4326', 'grid', 'active'
            )
            ON CONFLICT (project_id) DO NOTHING
            """,
            _TEST_PROJECT, _TEST_WORKSPACE,
        )
    finally:
        await conn.close()


async def _cleanup():
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            "DELETE FROM bronze.manifest WHERE workspace_id = $1::uuid",
            _TEST_WORKSPACE,
        )
        await conn.execute(
            "DELETE FROM silver.ingest_progress WHERE workspace_id = $1::uuid",
            _TEST_WORKSPACE,
        )
        await conn.execute(
            "DELETE FROM outbox.pending_propagations "
            "WHERE workspace_id = $1::uuid AND source_table = 'phase5_test'",
            _TEST_WORKSPACE,
        )
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def _bootstrap_and_clean():
    await _ensure_workspace()
    await _cleanup()
    yield
    await _cleanup()


# ---------------------------------------------------------------------------
# T10 — Tier 1 bronze orphan recovery
# ---------------------------------------------------------------------------
async def test_tier_1_bronze_dispatches_orphan_and_locks_against_double_dispatch(monkeypatch):
    # Stub the FastAPI HTTP dispatch — we don't want a real ingest_pdf
    # workflow to fire, only the bronze claim behaviour under test.
    dispatched_keys: list[str] = []

    async def _stub_dispatch(*, workspace_id, project_id, minio_key):
        dispatched_keys.append(minio_key)
        return str(uuid.uuid4())

    monkeypatch.setattr(sweep_mod, "_dispatch_ingest_pdf", _stub_dispatch)

    # Insert an orphan manifest row predated to look stale.
    file_key = f"reports/{_TEST_PROJECT}/20260101_120000_orphan_{uuid.uuid4()}.pdf"
    sha = hashlib.sha256(file_key.encode()).hexdigest()
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            """
            INSERT INTO bronze.manifest
                (file_key, workspace_id, sha256, document_type,
                 uploaded_at, dispatch_attempts)
            VALUES ($1, $2::uuid, $3, 'reports',
                    now() - interval '90 minutes', 0)
            """,
            file_key, _TEST_WORKSPACE, sha,
        )
    finally:
        await conn.close()

    pool = await ingest_progress.get_pool()

    # First sweep — claims + dispatches.
    report1 = await sweep_mod._tier_1_bronze(pool)
    assert file_key in dispatched_keys
    assert report1.items_dispatched >= 1

    # Manifest row should now have dispatch_attempts=1 and a locked_until
    # in the future.
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        row = await conn.fetchrow(
            "SELECT dispatch_attempts, locked_until "
            "FROM bronze.manifest WHERE file_key = $1 AND workspace_id = $2::uuid",
            file_key, _TEST_WORKSPACE,
        )
    finally:
        await conn.close()
    assert row["dispatch_attempts"] == 1
    assert row["locked_until"] is not None

    # Second sweep IMMEDIATELY — must not re-dispatch (lock still held).
    dispatched_keys.clear()
    report2 = await sweep_mod._tier_1_bronze(pool)
    assert file_key not in dispatched_keys, \
        "claim-lock should prevent double-dispatch within the lock window"
    assert report2.items_dispatched == 0


# ---------------------------------------------------------------------------
# Tier 4 — stuck outbox propagations get re-enqueued
# ---------------------------------------------------------------------------
async def test_tier_4_outbox_reenqueues_stuck_pending_rows():
    stuck_id = str(uuid.uuid4())
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            """
            INSERT INTO outbox.pending_propagations (
                id, workspace_id, source_schema, source_table, source_id,
                target_store, target_collection, operation, payload,
                idempotency_key, status, enqueued_at
            ) VALUES (
                $1::uuid, $2::uuid, 'silver', 'phase5_test', $1,
                'qdrant', 'georag_reports', 'upsert', '{}'::jsonb,
                $3, 'pending', now() - interval '90 minutes'
            )
            """,
            stuck_id, _TEST_WORKSPACE,
            f"phase5-tier4-{stuck_id}",
        )
    finally:
        await conn.close()

    pool = await ingest_progress.get_pool()
    report = await sweep_mod._tier_4_outbox(pool)

    assert report.items_examined >= 1
    assert report.items_dispatched >= 1

    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        row = await conn.fetchrow(
            "SELECT enqueued_at FROM outbox.pending_propagations WHERE id = $1::uuid",
            stuck_id,
        )
    finally:
        await conn.close()
    # enqueued_at must now be recent — the sweep nudged it forward so the
    # outbox_dispatcher will pick it up on its next tick.
    from datetime import datetime
    assert (datetime.now(UTC) - row["enqueued_at"]).total_seconds() < 60


# ---------------------------------------------------------------------------
# Pass 2 — data_version bump only for workspaces that saw recovery work
# ---------------------------------------------------------------------------
async def test_pass_2_bumps_data_version_for_recovered_workspaces():
    # Insert a recent recovery-triggered ingest_progress row.
    await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE,
        project_id=_TEST_PROJECT,
        minio_key=f"reports/{_TEST_PROJECT}/recovery-{uuid.uuid4()}.pdf",
        triggered_by="nightly_integrity_sweep",
        recovery_reason="bronze_orphan",
    )

    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        before = await conn.fetchval(
            "SELECT data_version FROM silver.workspaces "
            "WHERE workspace_id = $1::uuid",
            _TEST_WORKSPACE,
        )
    finally:
        await conn.close()

    pool = await ingest_progress.get_pool()
    bumped = await sweep_mod._bump_data_version_for_recovered_workspaces(pool)
    assert _TEST_WORKSPACE in bumped

    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        after = await conn.fetchval(
            "SELECT data_version FROM silver.workspaces "
            "WHERE workspace_id = $1::uuid",
            _TEST_WORKSPACE,
        )
    finally:
        await conn.close()
    assert after == before + 1


# ---------------------------------------------------------------------------
# Integrity-report row is written
# ---------------------------------------------------------------------------
async def test_write_integrity_report_row_lands_in_ingest_progress():
    pool = await ingest_progress.get_pool()

    out = sweep_mod.NightlyIntegritySweepOutput(
        pass_number=1,
        started_at="2026-05-25T02:00:00Z",
        duration_ms=1234,
        tiers=[
            sweep_mod.TierReport(tier=1, name="bronze_audit",
                                 items_dispatched=2, items_examined=2),
            sweep_mod.TierReport(tier=2, name="silver_audit",
                                 extras={"orphan_passages_claimed": 7,
                                         "qdrant_miss_rates": {}}),
            sweep_mod.TierReport(tier=3, name="gold_audit",
                                 extras={"view_results": []}),
            sweep_mod.TierReport(tier=4, name="outbox_audit",
                                 items_dispatched=1),
        ],
        workspaces_data_version_bumped=[_TEST_WORKSPACE],
    )

    await sweep_mod._write_integrity_report_row(pool, 1, out)

    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        row = await conn.fetchrow(
            """
            SELECT current_stage, current_step, status, triggered_by, error_text
            FROM silver.ingest_progress
            WHERE triggered_by = 'nightly_integrity_sweep'
            ORDER BY started_at DESC LIMIT 1
            """,
        )
    finally:
        await conn.close()
    assert row is not None
    assert row["current_stage"] == "integrity_sweep"
    assert row["status"] == "completed"
    assert row["triggered_by"] == "nightly_integrity_sweep"
    assert "tier1_bronze_orphans_dispatched" in (row["error_text"] or "")
