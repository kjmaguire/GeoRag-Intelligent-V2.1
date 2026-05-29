"""Tests for the stale_run_detector cron — verifies the three resolutions
landed 2026-05-25 in response to the Ontario-project mass timeout:

  1. Race recovery — embedding-stage rows with zero unembedded passages
     get marked completed instead of timed_out.
  2. Retry dispatch — preflight/parse/persist failures inside the attempt
     cap spawn a fresh ingest_pdf with parent_run_id linkage.
  3. Default — every other case becomes terminal timed_out.

Integration tests against the live Postgres schema, matching the
state-machine test pattern in test_ingest_progress_state_machine.py.
The ingest_pdf dispatch is mocked so we don't trigger a real Hatchet
workflow.
"""
from __future__ import annotations

import os
import sys
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

if not os.environ.get("POSTGRES_USER"):
    pytest.skip("postgres env not configured", allow_module_level=True)

# Parser stub so importing ingest_pdf (which stale_run_detector imports
# lazily) doesn't blow up when georag_dagster isn't installed.
def _ensure_parser_stub():
    if "georag_dagster.parsers.pdf_report" in sys.modules:
        return
    pkg_root = sys.modules.get("georag_dagster") or types.ModuleType("georag_dagster")
    pkg_parsers = types.ModuleType("georag_dagster.parsers")
    mod = types.ModuleType("georag_dagster.parsers.pdf_report")
    mod._FIGURE_TEMPDIR_ROOT = "/tmp/georag_figures"

    def _figure_tempdir(sha256: str) -> str:
        d = f"{mod._FIGURE_TEMPDIR_ROOT}/{sha256}"
        os.makedirs(d, exist_ok=True)
        return d

    mod._figure_tempdir = _figure_tempdir
    mod.parse_pdf_report = MagicMock()
    pkg_parsers.pdf_report = mod
    pkg_root.parsers = pkg_parsers
    sys.modules["georag_dagster"] = pkg_root
    sys.modules["georag_dagster.parsers"] = pkg_parsers
    sys.modules["georag_dagster.parsers.pdf_report"] = mod


_ensure_parser_stub()

from app.hatchet_workflows import _progress as ingest_progress  # noqa: E402
from app.hatchet_workflows import stale_run_detector as srd  # noqa: E402


# Reuse the workspace + project created by the state-machine tests.
# Inserting fresh rows fails under RLS when the test connection lands
# as georag_app (the default POSTGRES_USER inside the fastapi container).
_TEST_WORKSPACE = "a0000000-0000-0000-0000-00000000feed"
_TEST_PROJECT = "b1000000-0000-0000-0000-0000000000a0"


def _unique_key(suffix: str) -> str:
    return f"reports/_stale_detector_test_/{uuid.uuid4()}_{suffix}.pdf"


async def _ensure_test_workspace() -> None:
    """Verify the shared test workspace exists. Skip the module if not —
    bootstrap is the state-machine test's job and we don't want to fight
    RLS to do it twice."""
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM silver.projects WHERE project_id = $1::uuid",
            _TEST_PROJECT,
        )
    finally:
        await conn.close()
    if not exists:
        pytest.skip(
            "shared test project not present — run "
            "test_ingest_progress_state_machine.py first to provision it"
        )


async def _cleanup_runs_for_key(minio_key: str) -> None:
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            "DELETE FROM silver.ingest_progress WHERE minio_key = $1",
            minio_key,
        )
    finally:
        await conn.close()


async def _force_stale_heartbeat(run_id: str, minutes_old: int = 30) -> None:
    """Backdate last_heartbeat_at + step_started_at so the detector's
    `now() - interval` predicate sees this row as stale."""
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            f"""
            UPDATE silver.ingest_progress
            SET last_heartbeat_at = now() - interval '{minutes_old} minutes',
                last_stage_started_at = now() - interval '{minutes_old} minutes'
            WHERE run_id = $1::uuid
            """,
            run_id,
        )
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def _bootstrap():
    if ingest_progress._pool is not None:
        try:
            await ingest_progress._pool.close()
        except Exception:
            pass
        ingest_progress._pool = None
    await _ensure_test_workspace()
    yield
    if ingest_progress._pool is not None:
        try:
            await ingest_progress._pool.close()
        except Exception:
            pass
        ingest_progress._pool = None


def _unwrap_task(workflow_task):
    """Pull the underlying coroutine out of the Hatchet task decorator."""
    return getattr(workflow_task, "_fn", workflow_task)


# ---------------------------------------------------------------------------
# Resolution 1 — race recovery
# ---------------------------------------------------------------------------
async def test_stale_run_in_embedding_with_zero_unembedded_becomes_completed():
    """A row stuck in embedding/embed_verify whose project is actually
    fully embedded must be marked completed, not timed_out — this is the
    bug that caused the Ontario-project mass timeout 2026-05-25."""
    key = _unique_key("race-recovery")
    run_id = await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE,
        project_id=_TEST_PROJECT,
        minio_key=key,
    )
    await ingest_progress.mark_stage_started(run_id=run_id, stage="embed_verify")
    await ingest_progress.mark_stage_started(run_id=run_id, stage="embedding")
    await _force_stale_heartbeat(run_id)
    try:
        # _project_is_fully_embedded → True (no unembedded passages exist
        # for this synthetic project since we never inserted any).
        detect = _unwrap_task(srd.detect)
        with patch.object(srd, "post_ingestion_progress", AsyncMock()):
            out = await detect(srd.StaleRunDetectorInput(stale_minutes=15), MagicMock())
        assert out.runs_marked_completed >= 1
        row = await ingest_progress.get_run(run_id=run_id)
        assert row["status"] == "completed", \
            f"expected race-recovery to completed, got status={row['status']}"
    finally:
        await _cleanup_runs_for_key(key)


# ---------------------------------------------------------------------------
# Resolution 2 — retry dispatch
# ---------------------------------------------------------------------------
async def test_stale_run_in_parse_stage_dispatches_recovery_within_cap():
    """A row that stalled mid-parse (within attempt cap) should be marked
    timed_out AND spawn a recovery ingest_pdf run with parent_run_id +
    triggered_by='stale_run_sweep'."""
    key = _unique_key("retry-dispatch")
    run_id = await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE,
        project_id=_TEST_PROJECT,
        minio_key=key,
    )
    await ingest_progress.mark_stage_started(run_id=run_id, stage="parse")
    await _force_stale_heartbeat(run_id)
    try:
        dispatched: list[object] = []

        async def _fake_dispatch(payload):
            dispatched.append(payload)
            ref = MagicMock()
            ref.workflow_run_id = "fake-wf-" + uuid.uuid4().hex[:8]
            return ref

        detect = _unwrap_task(srd.detect)
        with patch.object(srd, "post_ingestion_progress", AsyncMock()), \
                patch("app.hatchet_workflows.ingest_pdf.ingest_pdf.aio_run_no_wait",
                      side_effect=_fake_dispatch):
            out = await detect(srd.StaleRunDetectorInput(stale_minutes=15), MagicMock())

        assert out.runs_marked_timed_out >= 1
        assert out.recovery_runs_dispatched >= 1
        assert len(dispatched) >= 1

        # Original row terminal.
        original = await ingest_progress.get_run(run_id=run_id)
        assert original["status"] == "timed_out"

        # Recovery row exists with parent_run_id pointing to original.
        conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
        try:
            recovery_row = await conn.fetchrow(
                """
                SELECT triggered_by, parent_run_id::text AS parent_run_id,
                       recovery_reason, attempt_number
                FROM silver.ingest_progress
                WHERE minio_key = $1 AND parent_run_id = $2::uuid
                """,
                key, run_id,
            )
        finally:
            await conn.close()
        assert recovery_row is not None, "recovery row must be created"
        assert recovery_row["triggered_by"] == "stale_run_sweep"
        assert recovery_row["recovery_reason"] == "stale_heartbeat"
        assert recovery_row["attempt_number"] == 2
    finally:
        await _cleanup_runs_for_key(key)


async def test_stale_run_over_attempt_cap_does_not_dispatch_recovery():
    """When attempt_number is already at or above the cap, no new
    recovery run should be dispatched — the row just stays timed_out."""
    key = _unique_key("cap-reached")
    # Build a parent chain that reaches the cap (3 attempts).
    run_a = await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE, project_id=_TEST_PROJECT, minio_key=key,
    )
    run_b = await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE, project_id=_TEST_PROJECT, minio_key=key,
        parent_run_id=run_a, recovery_reason="stale_heartbeat",
        triggered_by="stale_run_sweep",
    )
    run_c = await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE, project_id=_TEST_PROJECT, minio_key=key,
        parent_run_id=run_b, recovery_reason="stale_heartbeat",
        triggered_by="stale_run_sweep",
    )
    # run_c is attempt_number=3, at the cap.
    await ingest_progress.mark_stage_started(run_id=run_c, stage="parse")
    await _force_stale_heartbeat(run_c)
    try:
        dispatched: list[object] = []

        async def _fake_dispatch(payload):
            dispatched.append(payload)
            ref = MagicMock()
            ref.workflow_run_id = "should-not-fire"
            return ref

        detect = _unwrap_task(srd.detect)
        with patch.object(srd, "post_ingestion_progress", AsyncMock()), \
                patch("app.hatchet_workflows.ingest_pdf.ingest_pdf.aio_run_no_wait",
                      side_effect=_fake_dispatch):
            out = await detect(srd.StaleRunDetectorInput(stale_minutes=15), MagicMock())

        # Either no recovery was dispatched, OR the dispatched one was
        # for a *different* (unrelated test) row. Verify our row didn't
        # spawn a new attempt.
        assert out.recovery_runs_dispatched == 0 or \
            all(p.minio_key != key for p in dispatched), \
            "row at cap must not spawn another recovery"

        terminal = await ingest_progress.get_run(run_id=run_c)
        assert terminal["status"] == "timed_out"
    finally:
        await _cleanup_runs_for_key(key)


# ---------------------------------------------------------------------------
# Resolution 3 — non-retry stages still terminal
# ---------------------------------------------------------------------------
async def test_stale_run_in_unknown_step_marks_timed_out_only():
    """A row that died at a non-retry-eligible step (e.g. 'queued') is
    still marked timed_out with no recovery dispatch."""
    key = _unique_key("queued-stale")
    run_id = await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE, project_id=_TEST_PROJECT, minio_key=key,
    )
    # Force into started without advancing to any retry-eligible stage:
    # mark a stage then NULL out current_step so the detector's
    # filter-by-stage doesn't qualify it for retry.
    await ingest_progress.mark_stage_started(run_id=run_id, stage="preflight")
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            "UPDATE silver.ingest_progress SET current_step = 'queued' "
            "WHERE run_id = $1::uuid", run_id,
        )
    finally:
        await conn.close()
    await _force_stale_heartbeat(run_id)
    try:
        dispatched: list[object] = []

        async def _fake_dispatch(payload):
            dispatched.append(payload)
            return MagicMock(workflow_run_id="x")

        detect = _unwrap_task(srd.detect)
        with patch.object(srd, "post_ingestion_progress", AsyncMock()), \
                patch("app.hatchet_workflows.ingest_pdf.ingest_pdf.aio_run_no_wait",
                      side_effect=_fake_dispatch):
            await detect(srd.StaleRunDetectorInput(stale_minutes=15), MagicMock())

        # Our row should not have triggered a dispatch.
        assert all(p.minio_key != key for p in dispatched)
        terminal = await ingest_progress.get_run(run_id=run_id)
        assert terminal["status"] == "timed_out"
    finally:
        await _cleanup_runs_for_key(key)
