"""Live tests for `record_decision` (doc-phase 115).

Requires the postgres test connection. Creates + tears down a
synthetic workspace + user + decision per test to avoid polluting
real data.

Verifies the §21 facade lands:
- decision_records row
- evidence link rows
- options rows
- (when requested) outcome row
- audit_ledger row + back-fill of audit_ledger_id + hash
"""
from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

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
    """asyncpg connection."""
    c = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def synthetic_workspace(conn):
    """Insert + tear down a workspace row for the test.

    Also sets `app.workspace_id` for the connection so RLS-protected
    INSERTs into `silver.decision_records` pass the WITH CHECK
    policy. Resets the GUC on teardown.
    """
    ws_id = uuid4()
    # silver.workspaces itself has no RLS policy gating INSERTs, but
    # the decision schema does — set the GUC before any work runs.
    await conn.execute(
        """
        INSERT INTO silver.workspaces (workspace_id, name, slug)
        VALUES ($1::uuid, $2, $3)
        """,
        str(ws_id),
        f"test-ws-{ws_id}",
        f"test-ws-{ws_id}",
    )
    await conn.execute(
        "SELECT set_config('app.workspace_id', $1, false)",
        str(ws_id),
    )
    try:
        yield ws_id
    finally:
        # Reset GUC so the workspace DELETE doesn't trip RLS on
        # cascading silver.* table cleanup.
        await conn.execute("SELECT set_config('app.workspace_id', '', false)")
        await conn.execute(
            "DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid",
            str(ws_id),
        )


@pytest.fixture
async def synthetic_user(conn):
    """Insert + tear down a user row for the test."""
    email = f"test-{uuid4()}@example.com"
    user_id = await conn.fetchval(
        """
        INSERT INTO public.users (name, email, password)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        f"Decision Test User",
        email,
        "test-password-hash",
    )
    try:
        yield user_id
    finally:
        await conn.execute("DELETE FROM public.users WHERE id = $1", user_id)


@pytest.mark.asyncio
async def test_record_decision_basic(conn, synthetic_workspace, synthetic_user):
    """Minimal happy path: insert decision + verify row + audit anchor."""
    decision_id = await record_decision(
        conn,
        workspace_id=synthetic_workspace,
        decision_type="schema_mapping",
        recommendation="Map column 'au_g_t' to silver.assays.au_ppm",
        human_decision="accepted",
        decided_by_user_id=synthetic_user,
        reason="Units match after g/t→ppm conversion",
        uncertainty=0.15,
    )

    row = await conn.fetchrow(
        """
        SELECT decision_type, human_decision, reason, uncertainty,
               decided_by_user_id, hash, audit_ledger_id
        FROM silver.decision_records
        WHERE decision_id = $1::uuid
        """,
        str(decision_id),
    )

    assert row is not None
    assert row["decision_type"] == "schema_mapping"
    assert row["human_decision"] == "accepted"
    assert row["reason"] == "Units match after g/t→ppm conversion"
    assert float(row["uncertainty"]) == pytest.approx(0.15)
    assert row["decided_by_user_id"] == synthetic_user
    # Audit anchor populated by record_decision after emit_audit.
    assert row["audit_ledger_id"] is not None
    assert row["hash"] is not None
    assert len(bytes(row["hash"])) == 32  # SHA-256

    # Clean up the decision row (cascade clears child rows).
    await conn.execute(
        "DELETE FROM silver.decision_records WHERE decision_id = $1::uuid",
        str(decision_id),
    )


@pytest.mark.asyncio
async def test_record_decision_with_evidence_and_options(
    conn, synthetic_workspace, synthetic_user
):
    """Verify evidence_links + options rows are written + chained."""
    decision_id = await record_decision(
        conn,
        workspace_id=synthetic_workspace,
        decision_type="target_recommendation",
        recommendation="Rank zone Z-42 first",
        human_decision="modified",
        decided_by_user_id=synthetic_user,
        reason="Anomaly cluster + alteration overlap matches model",
        evidence_chunk_ids=["chunk_aaa", "chunk_bbb", "chunk_ccc"],
        options_considered=[
            {"label": "Z-17", "description": "Eastern anomaly cluster",
             "was_chosen": False},
            {"label": "Z-42", "description": "Central structural intersect",
             "was_chosen": True},
        ],
        uncertainty=0.32,
    )

    # Verify decision row + audit hooks
    row = await conn.fetchrow(
        "SELECT audit_ledger_id, hash FROM silver.decision_records "
        "WHERE decision_id = $1::uuid",
        str(decision_id),
    )
    assert row["audit_ledger_id"] is not None
    assert row["hash"] is not None

    # Evidence count
    ev_count = await conn.fetchval(
        "SELECT count(*) FROM silver.decision_evidence_links "
        "WHERE decision_id = $1::uuid",
        str(decision_id),
    )
    assert ev_count == 3

    # Options count + chosen flag
    options = await conn.fetch(
        "SELECT label, was_chosen FROM silver.decision_options "
        "WHERE decision_id = $1::uuid ORDER BY label",
        str(decision_id),
    )
    assert len(options) == 2
    assert options[0]["label"] == "Z-17"
    assert options[0]["was_chosen"] is False
    assert options[1]["label"] == "Z-42"
    assert options[1]["was_chosen"] is True

    # Audit ledger row exists + matches action_type.
    audit_row = await conn.fetchrow(
        """
        SELECT action_type, actor_kind, actor_id, target_id
        FROM audit.audit_ledger
        WHERE id = $1::uuid
        """,
        str(row["audit_ledger_id"]),
    )
    assert audit_row is not None
    assert audit_row["action_type"] == "decision.target_recommendation"
    assert audit_row["actor_kind"] == "user"
    assert audit_row["actor_id"] == synthetic_user
    assert audit_row["target_id"] == str(decision_id)

    # Cleanup
    await conn.execute(
        "DELETE FROM silver.decision_records WHERE decision_id = $1::uuid",
        str(decision_id),
    )


@pytest.mark.asyncio
async def test_record_decision_uncertainty_validation(
    conn, synthetic_workspace, synthetic_user
):
    """uncertainty outside [0,1] must raise ValueError before any DB write."""
    with pytest.raises(ValueError, match="uncertainty must be in"):
        await record_decision(
            conn,
            workspace_id=synthetic_workspace,
            decision_type="export_approval",
            recommendation="x",
            human_decision="x",
            decided_by_user_id=synthetic_user,
            uncertainty=1.5,  # out of range
        )

    with pytest.raises(ValueError, match="uncertainty must be in"):
        await record_decision(
            conn,
            workspace_id=synthetic_workspace,
            decision_type="export_approval",
            recommendation="x",
            human_decision="x",
            decided_by_user_id=synthetic_user,
            uncertainty=-0.01,  # out of range
        )


@pytest.mark.asyncio
async def test_record_decision_with_outcome(
    conn, synthetic_workspace, synthetic_user
):
    """Optional outcome_kind triggers a decision_outcomes row."""
    decision_id = await record_decision(
        conn,
        workspace_id=synthetic_workspace,
        decision_type="report_signoff",
        recommendation="Approve TDD Report v2",
        human_decision="accepted",
        decided_by_user_id=synthetic_user,
        outcome_kind="published",
        outcome_payload={"delivery_target": "sharepoint", "package_size_mb": 12.4},
    )

    outcome_row = await conn.fetchrow(
        "SELECT outcome_kind, outcome_payload FROM silver.decision_outcomes "
        "WHERE decision_id = $1::uuid",
        str(decision_id),
    )
    assert outcome_row is not None
    assert outcome_row["outcome_kind"] == "published"
    # Payload column is JSONB; asyncpg may return string or dict.
    payload = outcome_row["outcome_payload"]
    if isinstance(payload, str):
        import json
        payload = json.loads(payload)
    assert payload["delivery_target"] == "sharepoint"

    await conn.execute(
        "DELETE FROM silver.decision_records WHERE decision_id = $1::uuid",
        str(decision_id),
    )


@pytest.mark.asyncio
async def test_record_decision_option_missing_label_raises(
    conn, synthetic_workspace, synthetic_user
):
    """Options without 'label' raise ValueError + transaction rolls back."""
    with pytest.raises(ValueError, match="each option must have a 'label'"):
        await record_decision(
            conn,
            workspace_id=synthetic_workspace,
            decision_type="workflow_enablement",
            recommendation="Enable Activepieces flow",
            human_decision="accepted",
            decided_by_user_id=synthetic_user,
            options_considered=[{"description": "missing label here"}],
        )

    # No decision_records row should exist for this workspace after rollback.
    count = await conn.fetchval(
        "SELECT count(*) FROM silver.decision_records WHERE workspace_id = $1::uuid",
        str(synthetic_workspace),
    )
    assert count == 0
