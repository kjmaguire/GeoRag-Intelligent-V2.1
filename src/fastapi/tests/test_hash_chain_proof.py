"""Live tests for `build_hash_chain_proof` (doc-phase 117).

Verifies the §7.7 / §15.3 chain proof builder against a real
audit_ledger window that includes rows written by the
record_decision facade (doc-phase 115).

Each test:
1. Creates a synthetic workspace + user
2. Records 1-3 decisions via `record_decision` — each emits an
   audit_ledger row with a real chain hash
3. Builds the proof over the window
4. Asserts row counts, hash match, and summary fields
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from app.audit.hash_chain_proof import RECIPE_VERSION, build_hash_chain_proof
from app.services.decision_intelligence import record_decision


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
    await conn.execute(
        "SELECT set_config('app.workspace_id', $1, false)", str(ws_id)
    )
    try:
        yield ws_id
    finally:
        await conn.execute("SELECT set_config('app.workspace_id', '', false)")
        await conn.execute(
            "DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid",
            str(ws_id),
        )


@pytest.fixture
async def synthetic_user(conn):
    email = f"proof-test-{uuid4()}@example.com"
    user_id = await conn.fetchval(
        "INSERT INTO public.users (name, email, password) VALUES ($1,$2,$3) RETURNING id",
        "Proof Test User", email, "test-hash",
    )
    try:
        yield user_id
    finally:
        await conn.execute("DELETE FROM public.users WHERE id = $1", user_id)


@pytest.mark.asyncio
async def test_empty_window_returns_empty_proof(conn, synthetic_workspace):
    """A window with no audit rows returns row_count=0, all_match=True."""
    start = datetime.now(timezone.utc) - timedelta(hours=2)
    end = datetime.now(timezone.utc) - timedelta(hours=1)

    proof = await build_hash_chain_proof(
        conn,
        report_id=None,
        workspace_id=synthetic_workspace,
        start=start,
        end=end,
    )

    assert proof["workspace_id"] == str(synthetic_workspace)
    assert proof["recipe_version"] == RECIPE_VERSION
    assert proof["summary"]["row_count"] == 0
    assert proof["summary"]["all_match"] is True
    assert proof["summary"]["broken_ids"] == []
    assert proof["rows"] == []


@pytest.mark.asyncio
async def test_invalid_window_raises(conn, synthetic_workspace):
    """end <= start raises ValueError."""
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="end .* must be > start"):
        await build_hash_chain_proof(
            conn,
            workspace_id=synthetic_workspace,
            start=now,
            end=now,
        )

    with pytest.raises(ValueError, match="end .* must be > start"):
        await build_hash_chain_proof(
            conn,
            workspace_id=synthetic_workspace,
            start=now,
            end=now - timedelta(seconds=1),
        )


@pytest.mark.asyncio
async def test_proof_captures_single_decision(
    conn, synthetic_workspace, synthetic_user
):
    """One decision → one audit row → proof has 1 row, all_match=True."""
    start = datetime.now(timezone.utc) - timedelta(minutes=1)

    decision_id = await record_decision(
        conn,
        workspace_id=synthetic_workspace,
        decision_type="schema_mapping",
        recommendation="Map column 'au_g_t' → silver.assays.au_ppm",
        human_decision="accepted",
        decided_by_user_id=synthetic_user,
        reason="Unit conversion validated",
    )

    end = datetime.now(timezone.utc) + timedelta(minutes=1)

    proof = await build_hash_chain_proof(
        conn,
        report_id=None,
        workspace_id=synthetic_workspace,
        start=start,
        end=end,
    )

    assert proof["summary"]["row_count"] == 1
    assert proof["summary"]["all_match"] is True
    assert proof["summary"]["broken_ids"] == []

    row = proof["rows"][0]
    assert row["action_type"] == "decision.schema_mapping"
    assert row["actor_kind"] == "user"
    assert row["actor_id"] == synthetic_user
    assert row["target_schema"] == "silver"
    assert row["target_table"] == "decision_records"
    assert row["target_id"] == str(decision_id)
    assert row["match"] is True
    assert len(row["stored_hash_hex"]) == 64  # SHA-256 hex
    assert row["stored_hash_hex"] == row["recomputed_hash_hex"]

    # Cleanup
    await conn.execute(
        "DELETE FROM silver.decision_records WHERE decision_id = $1::uuid",
        str(decision_id),
    )


@pytest.mark.asyncio
async def test_proof_captures_multiple_decisions_in_order(
    conn, synthetic_workspace, synthetic_user
):
    """3 decisions → 3 chained audit rows, ordered by created_at."""
    start = datetime.now(timezone.utc) - timedelta(minutes=1)

    decision_ids = []
    for kind in ("schema_mapping", "export_approval", "workflow_enablement"):
        d = await record_decision(
            conn,
            workspace_id=synthetic_workspace,
            decision_type=kind,
            recommendation=f"Test {kind}",
            human_decision="accepted",
            decided_by_user_id=synthetic_user,
        )
        decision_ids.append(d)

    end = datetime.now(timezone.utc) + timedelta(minutes=1)

    proof = await build_hash_chain_proof(
        conn,
        report_id=None,
        workspace_id=synthetic_workspace,
        start=start,
        end=end,
    )

    assert proof["summary"]["row_count"] == 3
    assert proof["summary"]["all_match"] is True

    # Verify chain linkage: each row's previous_hash_hex equals the
    # PRIOR row's stored_hash_hex (within this workspace's chain).
    rows = proof["rows"]
    for i in range(1, len(rows)):
        assert rows[i]["previous_hash_hex"] == rows[i - 1]["stored_hash_hex"], (
            f"Chain break between row {i-1} and row {i}"
        )

    # Cleanup
    for d in decision_ids:
        await conn.execute(
            "DELETE FROM silver.decision_records WHERE decision_id = $1::uuid",
            str(d),
        )


@pytest.mark.asyncio
async def test_proof_report_id_filter(
    conn, synthetic_workspace, synthetic_user
):
    """report_id filter narrows the proof to matching target_id."""
    start = datetime.now(timezone.utc) - timedelta(minutes=1)

    # First decision (this will be the "report" anchor — use its
    # decision_id as the fake report_id).
    anchor_decision_id = await record_decision(
        conn,
        workspace_id=synthetic_workspace,
        decision_type="report_signoff",
        recommendation="Anchor for filter test",
        human_decision="accepted",
        decided_by_user_id=synthetic_user,
    )

    # Second decision (NOT the anchor; should be filtered out).
    other_decision_id = await record_decision(
        conn,
        workspace_id=synthetic_workspace,
        decision_type="export_approval",
        recommendation="Other decision",
        human_decision="accepted",
        decided_by_user_id=synthetic_user,
    )

    end = datetime.now(timezone.utc) + timedelta(minutes=1)

    proof = await build_hash_chain_proof(
        conn,
        report_id=anchor_decision_id,
        workspace_id=synthetic_workspace,
        start=start,
        end=end,
    )

    # Only the anchor's audit row should match (since the anchor's
    # audit_ledger row has target_id = str(anchor_decision_id)).
    assert proof["summary"]["row_count"] == 1
    assert proof["rows"][0]["target_id"] == str(anchor_decision_id)
    assert proof["report_id"] == str(anchor_decision_id)

    # Cleanup
    for d in (anchor_decision_id, other_decision_id):
        await conn.execute(
            "DELETE FROM silver.decision_records WHERE decision_id = $1::uuid",
            str(d),
        )
