"""§10.6 — promotion-gate enforcer tests.

Two layers:

  1. **Unit** — ``assess_promotion`` against a synthetic candidate vs
     baseline pair (no live FastAPI required, just PG). Seeds 2
     fresh runs with controlled pass/fail patterns, then asserts the
     gate's allow/block decision matches the >5pp regression rule.

  2. **Integration** — POST ``/api/v1/admin/eval/assess-promotion``
     end-to-end through the live FastAPI process.

Both layers clean up their seed rows on exit.
"""
from __future__ import annotations

import os
import uuid
from uuid import UUID

import asyncpg
import httpx
import pytest

FASTAPI_URL = os.environ.get("FASTAPI_URL", "http://localhost:8000")
SERVICE_KEY = os.environ.get("FASTAPI_SERVICE_KEY", "georag-service-key-dev")
PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
)
TEST_WORKSPACE_ID = UUID("a0000000-0000-0000-0000-000000000001")

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def pg_pool():
    pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def pg_conn():
    conn = await asyncpg.connect(PG_DSN)
    try:
        yield conn
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Seed helpers — build two synthetic eval runs over the same question set.
# ---------------------------------------------------------------------------
async def _pick_question_ids(
    conn: asyncpg.Connection,
    question_set: str,
    n: int,
) -> list[UUID]:
    rows = await conn.fetch(
        """
        SELECT question_id FROM eval.golden_questions
         WHERE question_set = $1
         ORDER BY question_id
         LIMIT $2
        """,
        question_set, n,
    )
    return [r["question_id"] for r in rows]


async def _seed_run(
    conn: asyncpg.Connection,
    question_ids: list[UUID],
    pass_flags: list[bool],
) -> UUID:
    """Insert a synthetic run_results set; returns a fresh run_id."""
    run_id = uuid.uuid4()
    for qid, passed in zip(question_ids, pass_flags, strict=True):
        await conn.execute(
            """
            INSERT INTO eval.run_results
                (run_id, question_id, passed, actual_payload)
            VALUES ($1, $2, $3, '{}'::jsonb)
            """,
            run_id, qid, passed,
        )
    return run_id


async def _cleanup_runs(conn: asyncpg.Connection, run_ids: list[UUID]) -> None:
    for rid in run_ids:
        await conn.execute(
            "DELETE FROM eval.run_results WHERE run_id = $1", rid
        )
    # Audit rows targeting these runs are best-effort
    for rid in run_ids:
        await conn.execute(
            """
            DELETE FROM audit.audit_ledger
             WHERE action_type LIKE 'eval.promotion.%'
               AND target_id = $1
            """,
            str(rid),
        )


# ---------------------------------------------------------------------------
# Unit-ish: direct service call
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_assess_promotion_allows_when_no_regression(
    pg_pool: asyncpg.Pool,
    pg_conn: asyncpg.Connection,
):
    """Baseline 7/10 pass, candidate 8/10 pass → no regression → allow."""
    from app.services.eval.promotion_gate import assess_promotion

    qids = await _pick_question_ids(pg_conn, "refusal_correctness", 10)
    if len(qids) < 10:
        pytest.skip("need ≥10 refusal_correctness questions seeded")

    baseline = await _seed_run(
        pg_conn, qids, [True] * 7 + [False] * 3,
    )
    candidate = await _seed_run(
        pg_conn, qids, [True] * 8 + [False] * 2,
    )

    try:
        a = await assess_promotion(
            pg_pool,
            workspace_id=TEST_WORKSPACE_ID,
            candidate_run_id=candidate,
            baseline_run_id=baseline,
            actor_user_id=971,
            emit_audit_row=False,  # don't pollute audit chain in unit path
        )
        assert a.allow is True, a.to_dict()
        assert a.blocking_sets == []
    finally:
        await _cleanup_runs(pg_conn, [baseline, candidate])


@pytest.mark.asyncio
async def test_assess_promotion_blocks_on_large_regression(
    pg_pool: asyncpg.Pool,
    pg_conn: asyncpg.Connection,
):
    """Baseline 9/10, candidate 4/10 → 50pp drop → blocks."""
    from app.services.eval.promotion_gate import (
        REGRESSION_THRESHOLD_PCT,
        assess_promotion,
    )

    qids = await _pick_question_ids(pg_conn, "refusal_correctness", 10)
    if len(qids) < 10:
        pytest.skip("need ≥10 refusal_correctness questions seeded")

    baseline = await _seed_run(pg_conn, qids, [True] * 9 + [False] * 1)
    candidate = await _seed_run(pg_conn, qids, [True] * 4 + [False] * 6)

    try:
        a = await assess_promotion(
            pg_pool,
            workspace_id=TEST_WORKSPACE_ID,
            candidate_run_id=candidate,
            baseline_run_id=baseline,
            emit_audit_row=False,
        )
        assert a.allow is False, a.to_dict()
        assert "refusal_correctness" in a.blocking_sets
        # Per-question regression list should have ≥5 rows (was-pass→now-fail)
        assert len(a.regressions) >= 5
        # Verify threshold is the locked default
        assert REGRESSION_THRESHOLD_PCT == 5.0
    finally:
        await _cleanup_runs(pg_conn, [baseline, candidate])


@pytest.mark.asyncio
async def test_assess_promotion_allows_small_drift(
    pg_pool: asyncpg.Pool,
    pg_conn: asyncpg.Connection,
):
    """Baseline 10/10 pass, candidate 10/10 pass → 0pp drift → allow."""
    from app.services.eval.promotion_gate import assess_promotion

    qids = await _pick_question_ids(pg_conn, "refusal_correctness", 10)
    if len(qids) < 10:
        pytest.skip("need ≥10 refusal_correctness questions seeded")

    baseline = await _seed_run(pg_conn, qids, [True] * 10)
    candidate = await _seed_run(pg_conn, qids, [True] * 10)

    try:
        a = await assess_promotion(
            pg_pool,
            workspace_id=TEST_WORKSPACE_ID,
            candidate_run_id=candidate,
            baseline_run_id=baseline,
            emit_audit_row=False,
        )
        assert a.allow is True
        assert a.regressions == []
    finally:
        await _cleanup_runs(pg_conn, [baseline, candidate])


@pytest.mark.asyncio
async def test_assess_promotion_emits_blocked_audit_row(
    pg_pool: asyncpg.Pool,
    pg_conn: asyncpg.Connection,
):
    """Audit row land in audit.audit_ledger with action_type=eval.promotion.blocked."""
    from app.services.eval.promotion_gate import assess_promotion

    qids = await _pick_question_ids(pg_conn, "refusal_correctness", 10)
    if len(qids) < 10:
        pytest.skip("need ≥10 refusal_correctness questions seeded")

    baseline = await _seed_run(pg_conn, qids, [True] * 9 + [False])
    candidate = await _seed_run(pg_conn, qids, [True] * 4 + [False] * 6)

    try:
        a = await assess_promotion(
            pg_pool,
            workspace_id=TEST_WORKSPACE_ID,
            candidate_run_id=candidate,
            baseline_run_id=baseline,
            actor_user_id=971,
            emit_audit_row=True,
        )
        assert a.allow is False

        audit_row = await pg_conn.fetchrow(
            """
            SELECT action_type, target_id
              FROM audit.audit_ledger
             WHERE action_type IN
                   ('eval.promotion.allowed','eval.promotion.blocked')
               AND target_id = $1
             ORDER BY created_at DESC
             LIMIT 1
            """,
            str(candidate),
        )
        assert audit_row is not None, "audit row missing"
        assert audit_row["action_type"] == "eval.promotion.blocked"
    finally:
        await _cleanup_runs(pg_conn, [baseline, candidate])


# ---------------------------------------------------------------------------
# Integration: POST /api/v1/admin/eval/assess-promotion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_assess_promotion_endpoint_blocks_on_regression(
    pg_conn: asyncpg.Connection,
):
    qids = await _pick_question_ids(pg_conn, "refusal_correctness", 10)
    if len(qids) < 10:
        pytest.skip("need ≥10 refusal_correctness questions seeded")

    baseline = await _seed_run(pg_conn, qids, [True] * 9 + [False])
    candidate = await _seed_run(pg_conn, qids, [True] * 3 + [False] * 7)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{FASTAPI_URL}/api/v1/admin/eval/assess-promotion",
                headers={
                    "X-Service-Key": SERVICE_KEY,
                    "Accept": "application/json",
                },
                json={
                    "workspace_id": str(TEST_WORKSPACE_ID),
                    "candidate_run_id": str(candidate),
                    "baseline_run_id": str(baseline),
                    "actor_user_id": 971,
                    "dry_run": True,
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["allow"] is False
        assert body["regression_threshold_pct"] == 5.0
        assert "refusal_correctness" in body["blocking_sets"]
    finally:
        await _cleanup_runs(pg_conn, [baseline, candidate])


@pytest.mark.asyncio
async def test_assess_promotion_endpoint_rejects_same_run_ids():
    same = uuid.uuid4()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{FASTAPI_URL}/api/v1/admin/eval/assess-promotion",
            headers={
                "X-Service-Key": SERVICE_KEY,
                "Accept": "application/json",
            },
            json={
                "workspace_id": str(TEST_WORKSPACE_ID),
                "candidate_run_id": str(same),
                "baseline_run_id": str(same),
                "dry_run": True,
            },
        )
    assert resp.status_code == 400
    assert "must differ" in resp.text
