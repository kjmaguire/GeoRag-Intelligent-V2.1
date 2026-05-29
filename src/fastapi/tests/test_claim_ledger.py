"""§7.4 Claim Ledger — round-trip tests."""
from __future__ import annotations

import os
import uuid
from uuid import UUID

import asyncpg
import pytest

from app.services.claim_ledger import (
    ClaimType, Support, VerificationStatus,
    record_claim, record_claims_bulk, update_verification,
    list_claims_for_run, summary_for_run,
)

PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
)
TEST_WORKSPACE_ID = UUID("a0000000-0000-0000-0000-000000000001")

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_pool():
    pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_claim_roundtrip(pg_pool):
    run_id = uuid.uuid4()
    cid = await record_claim(
        pg_pool, workspace_id=TEST_WORKSPACE_ID, answer_run_id=run_id,
        claim_text="The deposit averages 2.3 g/t Au.",
        claim_type=ClaimType.NUMERIC,
        required_support=Support.CITATION,
        sequence_in_answer=1,
    )
    assert UUID(cid)

    rows = await list_claims_for_run(
        pg_pool, workspace_id=TEST_WORKSPACE_ID, answer_run_id=run_id,
    )
    assert len(rows) == 1
    assert rows[0]["claim_type"] == "numeric"
    assert rows[0]["verification_status"] == "pending"


@pytest.mark.asyncio
async def test_verification_flow(pg_pool):
    run_id = uuid.uuid4()
    cid = await record_claim(
        pg_pool, workspace_id=TEST_WORKSPACE_ID, answer_run_id=run_id,
        claim_text="Hole DH-001 intersected mineralization.",
        claim_type=ClaimType.SPATIAL, required_support=Support.STRUCTURED_ROW,
    )
    ok = await update_verification(
        pg_pool, workspace_id=TEST_WORKSPACE_ID, claim_id=cid,
        status=VerificationStatus.VERIFIED, verifier="layer5_provenance",
        evidence={"citation_ids": ["abc-123"]}, confidence_score=0.95,
    )
    assert ok is True

    summary = await summary_for_run(
        pg_pool, workspace_id=TEST_WORKSPACE_ID, answer_run_id=run_id,
    )
    assert summary["total"] == 1
    assert summary["verified"] == 1
    assert summary["pending"] == 0


@pytest.mark.asyncio
async def test_bulk_insert(pg_pool):
    run_id = uuid.uuid4()
    n = await record_claims_bulk(
        pg_pool, workspace_id=TEST_WORKSPACE_ID, answer_run_id=run_id,
        claims=[
            {"claim_text": f"Claim {i}", "claim_type": ClaimType.QUALITATIVE,
             "required_support": Support.CITATION, "sequence_in_answer": i}
            for i in range(5)
        ],
    )
    assert n == 5
    rows = await list_claims_for_run(
        pg_pool, workspace_id=TEST_WORKSPACE_ID, answer_run_id=run_id,
    )
    assert len(rows) == 5
    # Ordered by sequence
    assert [r["sequence_in_answer"] for r in rows] == [0, 1, 2, 3, 4]
