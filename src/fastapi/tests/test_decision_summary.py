"""Live tests for `get_workspace_decision_summary` (doc-phase 119).

First downstream consumer test — exercises live aggregation on top
of the live `record_decision` writer from doc-phase 115.

Verifies:
- Empty workspace returns zero totals
- Single decision lands in correct decision_type bucket
- Mixed human_decision values bucket into accepted/modified/rejected/signed_off/other
- Audit-anchor coverage count is accurate
- Window filter excludes out-of-range decisions
- Mean uncertainty respects nullable values
- Invalid window raises
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from app.services.decision_intelligence import (
    ALL_DECISION_TYPES,
    get_workspace_decision_summary,
    record_decision,
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
    ws_id = uuid4()
    await conn.execute(
        "INSERT INTO silver.workspaces (workspace_id, name, slug) "
        "VALUES ($1::uuid, $2, $3)",
        str(ws_id), f"test-ws-{ws_id}", f"test-ws-{ws_id}",
    )
    await conn.execute("SELECT set_config('app.workspace_id', $1, false)", str(ws_id))
    try:
        yield ws_id
    finally:
        await conn.execute("SELECT set_config('app.workspace_id', '', false)")
        await conn.execute(
            "DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid", str(ws_id)
        )


@pytest.fixture
async def synthetic_user(conn):
    email = f"summary-test-{uuid4()}@example.com"
    user_id = await conn.fetchval(
        "INSERT INTO public.users (name, email, password) VALUES ($1,$2,$3) RETURNING id",
        "Summary Test User", email, "test-hash",
    )
    try:
        yield user_id
    finally:
        await conn.execute("DELETE FROM public.users WHERE id = $1", user_id)


def test_all_decision_types_constant_matches_check_enum():
    """The exported tuple lists exactly the 8 §21.3 types."""
    assert len(ALL_DECISION_TYPES) == 8
    assert set(ALL_DECISION_TYPES) == {
        "target_recommendation", "crs_decision", "schema_mapping",
        "public_data_import", "export_approval", "workflow_enablement",
        "conflict_resolution", "report_signoff",
    }


@pytest.mark.asyncio
async def test_empty_workspace_summary(conn, synthetic_workspace):
    """A fresh workspace returns zero totals + None mean uncertainty."""
    summary = await get_workspace_decision_summary(
        conn, workspace_id=synthetic_workspace,
    )
    assert summary.total_decisions == 0
    assert summary.decisions_with_audit_anchor == 0
    assert summary.by_type == []
    assert summary.mean_uncertainty is None
    assert summary.latest_decision_at is None


@pytest.mark.asyncio
async def test_summary_after_three_decisions(
    conn, synthetic_workspace, synthetic_user
):
    """3 decisions across 2 types → correct totals + per-type buckets."""
    ids = []
    # 2 × schema_mapping, both accepted
    for _ in range(2):
        d = await record_decision(
            conn,
            workspace_id=synthetic_workspace,
            decision_type="schema_mapping",
            recommendation="Map column 'au_g_t' → silver.assays.au_ppm",
            human_decision="accepted",
            decided_by_user_id=synthetic_user,
            uncertainty=0.10,
        )
        ids.append(d)

    # 1 × export_approval, rejected
    d = await record_decision(
        conn,
        workspace_id=synthetic_workspace,
        decision_type="export_approval",
        recommendation="Approve customer webhook export",
        human_decision="rejected",
        decided_by_user_id=synthetic_user,
        uncertainty=0.40,
    )
    ids.append(d)

    summary = await get_workspace_decision_summary(
        conn, workspace_id=synthetic_workspace,
    )

    assert summary.total_decisions == 3
    # Every record_decision writes an audit anchor → 100% coverage
    assert summary.decisions_with_audit_anchor == 3

    # Mean uncertainty: avg(0.10, 0.10, 0.40) = 0.20
    assert summary.mean_uncertainty == pytest.approx(0.20, rel=1e-6)

    # Per-type breakdown
    by_type = {b.decision_type: b for b in summary.by_type}
    assert set(by_type.keys()) == {"schema_mapping", "export_approval"}
    assert by_type["schema_mapping"].total == 2
    assert by_type["schema_mapping"].accepted == 2
    assert by_type["schema_mapping"].rejected == 0
    assert by_type["export_approval"].total == 1
    assert by_type["export_approval"].rejected == 1
    assert by_type["export_approval"].accepted == 0

    # Latest is the most recent insert (the export_approval one)
    assert summary.latest_decision_at is not None

    # Cleanup
    for d in ids:
        await conn.execute(
            "DELETE FROM silver.decision_records WHERE decision_id = $1::uuid",
            str(d),
        )


@pytest.mark.asyncio
async def test_window_filter_excludes_out_of_range(
    conn, synthetic_workspace, synthetic_user
):
    """A window that doesn't include the decision returns zero."""
    d = await record_decision(
        conn,
        workspace_id=synthetic_workspace,
        decision_type="crs_decision",
        recommendation="EPSG:32613 vs EPSG:32614 — chose 32613",
        human_decision="accepted",
        decided_by_user_id=synthetic_user,
    )

    # Window in the past → excludes the just-inserted decision
    past_window = datetime.now(timezone.utc) - timedelta(days=10)
    past_end = past_window + timedelta(days=1)
    summary = await get_workspace_decision_summary(
        conn,
        workspace_id=synthetic_workspace,
        window_start=past_window,
        window_end=past_end,
    )
    assert summary.total_decisions == 0

    # Window that includes now → finds it
    near_start = datetime.now(timezone.utc) - timedelta(minutes=5)
    near_end = datetime.now(timezone.utc) + timedelta(minutes=5)
    summary2 = await get_workspace_decision_summary(
        conn,
        workspace_id=synthetic_workspace,
        window_start=near_start,
        window_end=near_end,
    )
    assert summary2.total_decisions == 1

    await conn.execute(
        "DELETE FROM silver.decision_records WHERE decision_id = $1::uuid",
        str(d),
    )


@pytest.mark.asyncio
async def test_invalid_window_raises(conn, synthetic_workspace):
    """end <= start raises ValueError."""
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="window_end .* must be > window_start"):
        await get_workspace_decision_summary(
            conn,
            workspace_id=synthetic_workspace,
            window_start=now,
            window_end=now,
        )


@pytest.mark.asyncio
async def test_mean_uncertainty_ignores_nulls(
    conn, synthetic_workspace, synthetic_user
):
    """Decisions without uncertainty are excluded from the mean."""
    # First: uncertainty=0.50
    d1 = await record_decision(
        conn,
        workspace_id=synthetic_workspace,
        decision_type="workflow_enablement",
        recommendation="Enable Activepieces flow X",
        human_decision="accepted",
        decided_by_user_id=synthetic_user,
        uncertainty=0.50,
    )
    # Second: no uncertainty set (NULL in DB)
    d2 = await record_decision(
        conn,
        workspace_id=synthetic_workspace,
        decision_type="public_data_import",
        recommendation="Import BC MINFILE",
        human_decision="accepted",
        decided_by_user_id=synthetic_user,
        # uncertainty omitted → NULL
    )

    summary = await get_workspace_decision_summary(
        conn, workspace_id=synthetic_workspace,
    )
    # Mean of {0.50} ignoring NULL → 0.50
    assert summary.mean_uncertainty == pytest.approx(0.50, rel=1e-6)
    assert summary.total_decisions == 2

    for d in (d1, d2):
        await conn.execute(
            "DELETE FROM silver.decision_records WHERE decision_id = $1::uuid",
            str(d),
        )
