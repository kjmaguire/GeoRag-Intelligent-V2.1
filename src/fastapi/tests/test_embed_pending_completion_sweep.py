"""Locks the contract for embed_pending_passages_wf's ingest_progress
completion sweep — landed 2026-05-25 alongside the stale_run_detector
fixes after the Ontario-project mass timeout.

Before the fix, the sweep only set ``current_step='completed'`` and left
``status='started'``, so stale_run_detector then clobbered the row to
``timed_out`` 15 minutes later. Confirm:

  1. Source uses mark_completed_by_run (the canonical terminal write).
  2. Source emits a per-run completion broadcast.
  3. Integration: a started row in 'embedding' stage transitions to
     status='completed' (not just current_step='completed').
"""
from __future__ import annotations

import inspect
import os
import sys
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest


# Parser stub for the lazy ingest_pdf import path.
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


# ---------------------------------------------------------------------------
# Source-level checks (no DB needed; run everywhere)
# ---------------------------------------------------------------------------
def test_sweep_source_uses_mark_completed_by_run():
    """The completion path must use the canonical terminal-write helper
    instead of a bare SQL UPDATE that forgets the status column."""
    from app.hatchet_workflows import embed_pending_passages as mod
    src = inspect.getsource(mod)
    assert "mark_completed_by_run" in src, (
        "embed_pending_passages must call mark_completed_by_run so "
        "status='completed' lands (the old UPDATE only set current_step)"
    )


def test_sweep_source_broadcasts_completion():
    """The sweep should fire post_ingestion_progress on each transitioned
    run so the UI flips without waiting for its poll tick."""
    from app.hatchet_workflows import embed_pending_passages as mod
    src = inspect.getsource(mod)
    assert "post_ingestion_progress" in src
    # And it must be called inside the sweep loop, not just the orphan
    # bookkeeping at the top of the task.
    sweep_section = src[src.index("rows_to_complete"):src.index("return EmbedPendingPassagesOutput")]
    assert "post_ingestion_progress" in sweep_section, (
        "broadcast must fire from inside the completion sweep loop"
    )


def test_sweep_source_does_not_filter_by_failed_at_null():
    """The old sweep filtered on `failed_at IS NULL`, which locked it out
    after stale_run_detector wrote failed_at. The new sweep uses the
    status enum so race-recovery is possible."""
    from app.hatchet_workflows import embed_pending_passages as mod
    src = inspect.getsource(mod)
    sweep_section = src[src.index("rows_to_complete"):src.index("return EmbedPendingPassagesOutput")]
    assert "failed_at IS NULL" not in sweep_section, (
        "completion sweep must not filter by failed_at — it locks out "
        "race-recovery after stale_run_detector writes it"
    )
    assert "status NOT IN" in sweep_section, (
        "completion sweep should filter by status enum, not legacy timestamp columns"
    )


# ---------------------------------------------------------------------------
# Integration test against the live DB
# ---------------------------------------------------------------------------
if not os.environ.get("POSTGRES_USER"):
    pytest.skip("postgres env not configured", allow_module_level=True)

import contextlib  # noqa: E402

from app.hatchet_workflows import _progress as ingest_progress  # noqa: E402

# Reuse the workspace + project already provisioned by the state-machine
# tests. Inserting fresh workspace/project rows fails under RLS when the
# test connection lands as georag_app (the default POSTGRES_USER inside
# the fastapi container).
_TEST_WORKSPACE = "a0000000-0000-0000-0000-00000000feed"
_TEST_PROJECT = "b1000000-0000-0000-0000-0000000000a0"


def _unique_key(suffix: str) -> str:
    return f"reports/_embed_sweep_test_/{uuid.uuid4()}_{suffix}.pdf"


async def _ensure_test_workspace() -> None:
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


async def _cleanup(key: str) -> None:
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            "DELETE FROM silver.ingest_progress WHERE minio_key = $1", key,
        )
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def _bootstrap():
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


async def test_started_row_in_embedding_stage_transitions_to_completed():
    """End-to-end: a row at current_step='embedding' for a project with
    zero unembedded passages must reach status='completed' after the
    sweep runs."""
    from app.hatchet_workflows import embed_pending_passages as mod

    key = _unique_key("sweep")
    run_id = await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE,
        project_id=_TEST_PROJECT,
        minio_key=key,
    )
    await ingest_progress.mark_stage_started(run_id=run_id, stage="embedding")
    try:
        run_task = getattr(mod.run, "_fn", mod.run)
        # Mock the actual embed work — we only care about the sweep
        # behavior, not the encoder. project_id='*' triggers the
        # all-projects code path; we feed it our synthetic project
        # explicitly instead.
        inp = mod.EmbedPendingPassagesInput(
            workspace_id=_TEST_WORKSPACE,
            project_id=_TEST_PROJECT,
            batch_size=8,
        )

        async def _noop_embed(**kwargs):
            return types.SimpleNamespace(
                passages_seen=0, passages_embedded=0,
                qdrant_points_upserted=0, passages_skipped=0,
                errors=[],
            )

        # Patches reflect the import sites: post_ingestion_progress +
        # claim_and_record_recovery are imported lazily INSIDE the task
        # body, so we patch the source modules. embed_pending_passages
        # is bound at the module top so we patch the module attribute.
        with patch.object(mod, "embed_pending_passages", _noop_embed), \
                patch("app.services.laravel_bridge.post_ingestion_progress", AsyncMock()), \
                patch("app.services.ingest.orphan_sweep.claim_and_record_recovery",
                      AsyncMock(return_value=([], []))):
            await run_task(inp, MagicMock())

        row = await ingest_progress.get_run(run_id=run_id)
        assert row["status"] == "completed", (
            f"sweep must flip status enum to 'completed' "
            f"(got status={row['status']}, current_step={row['current_step']})"
        )
        assert row["current_step"] == "completed"
        assert row["completed_at"] is not None
    finally:
        await _cleanup(key)
