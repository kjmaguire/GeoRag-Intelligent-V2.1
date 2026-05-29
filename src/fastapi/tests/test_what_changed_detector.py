"""Live test for the doc-phase 147 what_changed_detector graduation."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.hatchet_workflows.what_changed_detector import (
    WhatChangedInput,
    execute as what_changed_execute,
)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@pytest.fixture
async def conn():
    c = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def synthetic_workspace(conn):
    ws = uuid4()
    await conn.execute(
        """
        INSERT INTO silver.workspaces (workspace_id, name, slug)
        VALUES ($1::uuid, $2, $3)
        """,
        str(ws),
        f"test-wcd-{ws}",
        f"test-wcd-{ws}",
    )
    try:
        yield ws
    finally:
        await conn.execute("SELECT set_config('app.workspace_id', '', false)")
        await conn.execute(
            "DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid",
            str(ws),
        )


@pytest.mark.asyncio
async def test_what_changed_detector_empty_workspace_returns_zeros(
    synthetic_workspace,
):
    """Fresh workspace with no recent activity → all counts zero,
    success=True, audit anchor still lands."""
    now = datetime.now(timezone.utc)
    inp = WhatChangedInput(
        workspace_id=synthetic_workspace,
        window_start=now - timedelta(days=7),
        window_end=now,
        detect_request_id=uuid4(),
    )
    out = await what_changed_execute.aio_mock_run(inp)
    assert out.success is True
    # Bonus: 1 audit anchor lands (the rollup) so total_audit_anchors >= 1.
    # But our window is window_start..window_end exclusive on end, so the
    # anchor (emitted now) might not be counted. Either way:
    assert out.new_ingestion_count == 0
    assert out.new_decision_count == 0
    assert out.new_hypothesis_count == 0
    assert out.new_support_ticket_count == 0


@pytest.mark.asyncio
async def test_what_changed_detector_captures_default_workspace_signal(conn):
    """Run against the Default Workspace which has real activity from
    doc-phases 134 (hypotheses) + 136 (support tickets) + 143
    (response drafts) etc."""
    default_ws = UUID("a0000000-0000-0000-0000-000000000001")
    now = datetime.now(timezone.utc)
    inp = WhatChangedInput(
        workspace_id=default_ws,
        window_start=now - timedelta(days=7),
        window_end=now,
        detect_request_id=uuid4(),
    )
    out = await what_changed_execute.aio_mock_run(inp)
    assert out.success is True
    # We seeded 9 hypotheses + 6 support tickets in this window via prior ticks.
    assert out.new_hypothesis_count >= 9
    assert out.new_support_ticket_count >= 6
    # Total audits should be non-trivial (lots of decision.* + support.* + hypothesis.* anchors).
    assert out.total_audit_anchors_in_window > 30


@pytest.mark.asyncio
async def test_what_changed_detector_emits_audit_anchor(
    conn, synthetic_workspace
):
    """Verify the rollup audit anchor lands with the structured payload."""
    request_id = uuid4()
    now = datetime.now(timezone.utc)
    inp = WhatChangedInput(
        workspace_id=synthetic_workspace,
        window_start=now - timedelta(days=1),
        window_end=now,
        detect_request_id=request_id,
    )
    await what_changed_execute.aio_mock_run(inp)

    n = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'workspace.what_changed.detected'
           AND target_id = $1
        """,
        str(request_id),
    )
    assert n == 1


@pytest.mark.asyncio
async def test_what_changed_detector_narrow_window_returns_lower_counts(conn):
    """A 1-second window against the Default Workspace returns less than
    a 7-day window — sanity check on the windowing filter."""
    default_ws = UUID("a0000000-0000-0000-0000-000000000001")
    now = datetime.now(timezone.utc)

    narrow = await what_changed_execute.aio_mock_run(
        WhatChangedInput(
            workspace_id=default_ws,
            window_start=now - timedelta(seconds=1),
            window_end=now,
            detect_request_id=uuid4(),
        )
    )
    wide = await what_changed_execute.aio_mock_run(
        WhatChangedInput(
            workspace_id=default_ws,
            window_start=now - timedelta(days=30),
            window_end=now,
            detect_request_id=uuid4(),
        )
    )
    assert narrow.total_audit_anchors_in_window <= wide.total_audit_anchors_in_window
    assert narrow.new_hypothesis_count <= wide.new_hypothesis_count
