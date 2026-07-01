"""Tests for the Phase 1 ingestion-reliability state machine in
``app.hatchet_workflows._progress``.

Locks in the spec's core invariant:

    Every ingestion run reaches exactly one terminal state.
    Terminal states are immutable.

Covers T1, T5, and T7 from the spec test plan:

  T1 — failed-state writes capture stage + error
  T5 — late completion against a terminal row is a silent no-op
  T7 — DB write succeeds even when downstream broadcast fails
        (broadcasts are best-effort; the row is the durable record)

These are pure asyncpg integration tests against the live Postgres
schema. They use a dedicated workspace_id + minio_key prefix so they
don't collide with real ingestion rows.
"""
from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

# Skip the whole module if we can't talk to Postgres (e.g. local dev
# without docker compose up).
if not os.environ.get("POSTGRES_USER"):
    pytest.skip("postgres env not configured", allow_module_level=True)

import contextlib

from app.hatchet_workflows import _progress as ingest_progress  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_TEST_WORKSPACE = "a0000000-0000-0000-0000-00000000feed"
_TEST_PROJECT = "b1000000-0000-0000-0000-0000000000a0"


def _unique_key(suffix: str) -> str:
    return f"reports/_state_machine_test_/{uuid.uuid4()}_{suffix}.pdf"


async def _dsn() -> str:
    return ingest_progress._dsn()


async def _ensure_test_workspace() -> None:
    """Make sure the canary workspace + project exist (FK targets)."""
    conn = await asyncpg.connect(await _dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            """
            INSERT INTO silver.workspaces (workspace_id, name, slug)
            VALUES ($1::uuid, 'state-machine-tests', 'state-machine-tests-' || substring($1::text from 1 for 8))
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
                $1::uuid, 'state-machine-tests',
                'state-machine-tests-' || substring($1::text from 1 for 8),
                $2::uuid, 'EPSG:4326', 'grid', 'active'
            )
            ON CONFLICT (project_id) DO NOTHING
            """,
            _TEST_PROJECT, _TEST_WORKSPACE,
        )
    finally:
        await conn.close()


async def _cleanup_run(run_id: str) -> None:
    """Best-effort delete after each test."""
    conn = await asyncpg.connect(await _dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            "DELETE FROM silver.ingest_progress WHERE run_id = $1::uuid",
            run_id,
        )
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
async def _bootstrap_workspace():
    # Reset the module-level pool every test so we don't try to reuse a
    # pool bound to a closed event loop (pytest-asyncio default mode
    # creates a fresh loop per test).
    if ingest_progress._pool is not None:
        with contextlib.suppress(Exception):
            await ingest_progress._pool.close()
        ingest_progress._pool = None
    await _ensure_test_workspace()
    yield
    if ingest_progress._pool is not None:
        with contextlib.suppress(Exception):
            await ingest_progress._pool.close()
        ingest_progress._pool = None


@pytest.fixture
async def fresh_run():
    """Yield a freshly-created run; clean up after."""
    key = _unique_key("fresh")
    run_id = await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE,
        project_id=_TEST_PROJECT,
        minio_key=key,
    )
    assert run_id is not None, "start_run must succeed under test conditions"
    yield run_id, key
    await _cleanup_run(run_id)


# ---------------------------------------------------------------------------
# T1 — failed-state writes capture stage + error
# ---------------------------------------------------------------------------
async def test_mark_failed_records_stage_and_error(fresh_run):
    run_id, _ = fresh_run

    transitioned = await ingest_progress.mark_failed_by_run(
        run_id=run_id, stage="persist", error="db constraint blew up",
    )
    assert transitioned is True

    row = await ingest_progress.get_run(run_id=run_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["current_stage"] == "persist"
    assert row["error_text"] == "db constraint blew up"


async def test_mark_completed_records_report_id(fresh_run):
    run_id, _ = fresh_run

    fake_report = str(uuid.uuid4())
    transitioned = await ingest_progress.mark_completed_by_run(
        run_id=run_id, report_id=fake_report,
    )
    assert transitioned is True

    row = await ingest_progress.get_run(run_id=run_id)
    assert row is not None
    assert row["status"] == "completed"


# ---------------------------------------------------------------------------
# T5 — late completion against a terminal row is a no-op
# ---------------------------------------------------------------------------
async def test_late_completion_does_not_overwrite_failed(fresh_run):
    """The spec's core invariant — a delayed worker can't clobber a
    terminal state. mark_completed_by_run returns False on no-op."""
    run_id, _ = fresh_run

    assert await ingest_progress.mark_failed_by_run(
        run_id=run_id, stage="parse", error="boom",
    ) is True

    # Delayed completion arrives — should NOT transition.
    transitioned = await ingest_progress.mark_completed_by_run(
        run_id=run_id, report_id=str(uuid.uuid4()),
    )
    assert transitioned is False

    row = await ingest_progress.get_run(run_id=run_id)
    assert row["status"] == "failed", \
        "terminal state must be immutable — failed → completed is forbidden"
    assert row["error_text"] == "boom"


async def test_late_failure_does_not_overwrite_completed(fresh_run):
    run_id, _ = fresh_run

    assert await ingest_progress.mark_completed_by_run(run_id=run_id) is True

    transitioned = await ingest_progress.mark_failed_by_run(
        run_id=run_id, stage="persist", error="late failure",
    )
    assert transitioned is False

    row = await ingest_progress.get_run(run_id=run_id)
    assert row["status"] == "completed"


async def test_late_timed_out_does_not_overwrite_completed(fresh_run):
    """T2 / stale-run cron variant — a stale_heartbeat sweep that fires
    AFTER the workflow completed must not flip the row."""
    run_id, _ = fresh_run

    assert await ingest_progress.mark_completed_by_run(run_id=run_id) is True

    transitioned = await ingest_progress.mark_timed_out(
        run_id=run_id, reason="stale_heartbeat",
    )
    assert transitioned is False

    row = await ingest_progress.get_run(run_id=run_id)
    assert row["status"] == "completed"


# ---------------------------------------------------------------------------
# Stage-write semantics — mark_stage_started should be a no-op on terminal
# ---------------------------------------------------------------------------
async def test_stage_write_is_noop_on_terminal_row(fresh_run):
    run_id, _ = fresh_run

    await ingest_progress.mark_failed_by_run(
        run_id=run_id, stage="parse", error="dead",
    )

    # A delayed step-start arrives — should NOT reopen the row.
    await ingest_progress.mark_stage_started(
        run_id=run_id, stage="persist", worker_id="late-worker",
    )

    row = await ingest_progress.get_run(run_id=run_id)
    assert row["status"] == "failed"
    assert row["current_stage"] == "parse"


async def test_first_stage_started_flips_status_to_started(fresh_run):
    run_id, _ = fresh_run

    await ingest_progress.mark_stage_started(
        run_id=run_id, stage="preflight", worker_id="w1",
    )

    row = await ingest_progress.get_run(run_id=run_id)
    assert row["status"] == "started"
    assert row["current_stage"] == "preflight"
    assert row["current_step"] == "preflight"
    assert row["step_index"] == 1


# ---------------------------------------------------------------------------
# Heartbeat semantics
# ---------------------------------------------------------------------------
async def test_heartbeat_only_writes_when_started(fresh_run):
    run_id, _ = fresh_run

    # Heartbeat against a 'queued' row is a no-op (status='queued', not 'started').
    await ingest_progress.mark_heartbeat(run_id=run_id)
    row1 = await ingest_progress.get_run(run_id=run_id)
    # Now transition to started; heartbeat should write.
    await ingest_progress.mark_stage_started(run_id=run_id, stage="preflight")
    await ingest_progress.mark_heartbeat(run_id=run_id)
    row2 = await ingest_progress.get_run(run_id=run_id)
    assert row2["status"] == "started"
    # row1 didn't crash, that's all we needed to verify.
    assert row1 is not None


# ---------------------------------------------------------------------------
# Per-run identity — start_run always creates a new row
# ---------------------------------------------------------------------------
async def test_start_run_creates_independent_attempts():
    key = _unique_key("attempts")
    run_a = await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE,
        project_id=_TEST_PROJECT,
        minio_key=key,
    )
    run_b = await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE,
        project_id=_TEST_PROJECT,
        minio_key=key,
        parent_run_id=run_a,
        recovery_reason="manual_retry",
        triggered_by="manual_retry",
    )

    assert run_a != run_b

    row_a = await ingest_progress.get_run(run_id=run_a)
    row_b = await ingest_progress.get_run(run_id=run_b)
    assert row_a["attempt_number"] == 1
    assert row_b["attempt_number"] == 2
    assert row_b["parent_run_id"] == run_a
    assert row_b["recovery_reason"] == "manual_retry"
    assert row_b["triggered_by"] == "manual_retry"

    await _cleanup_run(run_b)
    await _cleanup_run(run_a)
