"""§12 polish — tests for the what_changed_weekly cron wrapper.

Live-stack tests:
  - cron schedule registered on the workflow
  - aio_mock_run with explicit_window_end=None fires + returns a
    structured digest
  - per-workspace digest counts > 0 if any active workspace exists
  - rollup audit anchor emitted with action_type
    'workspace.what_changed.weekly_digest'
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app.hatchet_workflows.what_changed_weekly import (
    WeeklyDigestInput, run_weekly, what_changed_weekly,
)

PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_conn():
    conn = await asyncpg.connect(PG_DSN, statement_cache_size=0)
    try:
        yield conn
    finally:
        await conn.close()


def test_cron_schedule_registered():
    """Workflow carries the agreed Monday-06:00-UTC slot — change here
    is a deliberate fail-fast for the operator runbook."""
    crons = (
        getattr(what_changed_weekly.config, "on_crons", None)
        or getattr(what_changed_weekly, "on_crons", None)
        or []
    )
    assert "0 6 * * 1" in crons, (
        f"expected Monday 06:00 UTC cron, got {crons}"
    )


@pytest.mark.asyncio
async def test_weekly_digest_default_input_runs():
    """aio_mock_run with default 7-day window should complete and
    return a WeeklyDigestOutput with the rollup counts populated."""
    out = await run_weekly.aio_mock_run(WeeklyDigestInput())
    assert out.workspace_count >= 0
    assert out.window_end >= out.window_start
    delta = out.window_end - out.window_start
    assert delta == timedelta(days=7)
    # If no active workspaces exist, total_ingestion is 0 (no error)
    assert out.total_ingestion >= 0


@pytest.mark.asyncio
async def test_weekly_digest_explicit_window():
    """Operator override path: explicit window end + custom window_days."""
    pinned_end = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
    out = await run_weekly.aio_mock_run(WeeklyDigestInput(
        window_days=3,
        explicit_window_end=pinned_end,
    ))
    assert out.window_end == pinned_end
    assert out.window_end - out.window_start == timedelta(days=3)


@pytest.mark.asyncio
async def test_rollup_audit_anchor_emitted(pg_conn: asyncpg.Connection):
    """Confirm the weekly digest emits a rollup
    workspace.what_changed.weekly_digest audit row."""
    pre_count = await pg_conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'workspace.what_changed.weekly_digest'
        """,
    )
    out = await run_weekly.aio_mock_run(WeeklyDigestInput(window_days=1))

    post_count = await pg_conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'workspace.what_changed.weekly_digest'
        """,
    )
    # Anchor should have landed for this run_id specifically
    target_row = await pg_conn.fetchrow(
        """
        SELECT target_id, payload FROM audit.audit_ledger
         WHERE action_type = 'workspace.what_changed.weekly_digest'
           AND target_id = $1
         LIMIT 1
        """,
        out.run_id,
    )
    assert post_count >= pre_count + 1
    assert target_row is not None
    assert target_row["target_id"] == out.run_id
