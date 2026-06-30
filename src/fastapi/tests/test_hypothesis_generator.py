"""Live tests for the §9.10 hypothesis generator (doc-phase 134).

Verifies the live orchestration:
  - 3 hypotheses (A/B/C) written per call with review_status='ai_suggested'
  - Evidence links distributed across roles
    (supporting/contradicting/missing/recommended_test)
  - Audit anchor emitted on completion
  - Stub-generated descriptions carry the synthetic_stub tag

The hypothesis generator is a synthetic stub; these tests exercise
the orchestration and the role distribution, not the real LLM
reasoning content.
"""
from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from app.services.geological_reasoning import (
    generate_hypotheses_for_question,
)
from app.services.geological_reasoning.hypothesis_generator import (
    _synthetic_hypothesis_set,
)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0
    )
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
async def conn():
    c = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def synthetic_workspace(conn):
    """Insert + tear down a synthetic workspace for this test.

    Sets ``app.workspace_id`` on the test's read-path connection so the
    post-Block-1 strict RLS policies let the test inspect the rows it
    just generated. (Generator writes happen on a separate pool conn
    that sets the GUC itself.)
    """
    ws_id = uuid4()
    await conn.execute(
        """
        INSERT INTO silver.workspaces (workspace_id, name, slug)
        VALUES ($1::uuid, $2, $3)
        """,
        str(ws_id),
        f"test-hg-{ws_id}",
        f"test-hg-{ws_id}",
    )
    await conn.execute(
        "SELECT set_config('app.workspace_id', $1, false)", str(ws_id),
    )
    try:
        yield ws_id
    finally:
        await conn.execute("SELECT set_config('app.workspace_id', '', false)")
        await conn.execute(
            "DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid",
            str(ws_id),
        )


# ----------------------------------------------------------------------
# Stub generator unit tests (no DB)
# ----------------------------------------------------------------------
def test_synthetic_stub_produces_three_hypotheses():
    drafts = _synthetic_hypothesis_set(
        parent_question="Does this AOI host a uranium-bearing system?",
        candidate_evidence_chunk_ids=["c1", "c2", "c3", "c4", "c5", "c6"],
    )
    assert len(drafts) == 3
    labels = [d.label for d in drafts]
    assert labels == ["A", "B", "C"]
    # Each draft has the synthetic_stub tag in its description.
    for d in drafts:
        assert "synthetic_stub" in d.description
        assert "doc-phase 134" in d.description
    # Confidences are deterministic ordered A > B > C.
    confidences = [d.confidence for d in drafts]
    assert confidences == [0.55, 0.30, 0.15]


def test_synthetic_stub_distributes_chunks_across_roles():
    drafts = _synthetic_hypothesis_set(
        parent_question="test",
        candidate_evidence_chunk_ids=["c1", "c2", "c3", "c4", "c5", "c6"],
    )
    role_counts: dict[str, int] = {}
    for d in drafts:
        for link in d.evidence_links:
            role_counts[link.role] = role_counts.get(link.role, 0) + 1
    # 6 chunks split into thirds + 3 missing + 3 recommended_test
    # (A has missing+rec_test, B has missing+rec_test, C has missing+rec_test)
    assert role_counts.get("supporting", 0) >= 1
    assert role_counts.get("contradicting", 0) >= 1
    assert role_counts.get("missing", 0) == 3
    assert role_counts.get("recommended_test", 0) == 3


def test_synthetic_stub_handles_zero_chunks():
    drafts = _synthetic_hypothesis_set(
        parent_question="test no chunks",
        candidate_evidence_chunk_ids=[],
    )
    # Still produces 3 hypotheses; only missing + recommended_test links.
    assert len(drafts) == 3
    for d in drafts:
        for link in d.evidence_links:
            assert link.role in ("missing", "recommended_test")
            assert link.source_chunk_id is None


def test_synthetic_stub_is_deterministic_for_same_question():
    a = _synthetic_hypothesis_set("same_q", ["c1", "c2", "c3"])
    b = _synthetic_hypothesis_set("same_q", ["c1", "c2", "c3"])
    assert [d.label for d in a] == [d.label for d in b]
    assert [d.description for d in a] == [d.description for d in b]


# ----------------------------------------------------------------------
# End-to-end DB tests
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generate_hypotheses_end_to_end(pool, conn, synthetic_workspace):
    """Smoke: generate hypotheses against a synthetic workspace."""
    result = await generate_hypotheses_for_question(
        workspace_id=synthetic_workspace,
        parent_question="Is there a viable uranium target in this AOI?",
        candidate_evidence_chunk_ids=["chunk_a", "chunk_b", "chunk_c"],
        pool=pool,
    )

    assert len(result.hypothesis_ids) == 3
    assert result.labels == ["A", "B", "C"]
    assert result.evidence_link_count >= 6  # at least 3 missing + 3 rec_test

    # Hypothesis rows persist with review_status='ai_suggested'.
    rows = await conn.fetch(
        """
        SELECT label, review_status, confidence, description
          FROM silver.hypotheses
         WHERE workspace_id = $1::uuid
         ORDER BY label
        """,
        str(synthetic_workspace),
    )
    assert len(rows) == 3
    assert [r["label"] for r in rows] == ["A", "B", "C"]
    for r in rows:
        assert r["review_status"] == "ai_suggested"
        assert "synthetic_stub" in r["description"]

    # Evidence-link rows are linked back and tagged.
    role_count = await conn.fetchval(
        """
        SELECT count(*)
          FROM silver.hypothesis_evidence_links l
          JOIN silver.hypotheses h ON h.hypothesis_id = l.hypothesis_id
         WHERE h.workspace_id = $1::uuid
        """,
        str(synthetic_workspace),
    )
    assert role_count == result.evidence_link_count


@pytest.mark.asyncio
async def test_generate_hypotheses_emits_audit_anchor(
    pool, conn, synthetic_workspace
):
    """Verify the hypothesis.generated audit ledger anchor is emitted."""
    await generate_hypotheses_for_question(
        workspace_id=synthetic_workspace,
        parent_question="Audit anchor test",
        candidate_evidence_chunk_ids=[],
        pool=pool,
    )

    n = await conn.fetchval(
        """
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'hypothesis.generated'
           AND workspace_id = $1::uuid
        """,
        str(synthetic_workspace),
    )
    assert n >= 1


@pytest.mark.asyncio
async def test_generate_hypotheses_role_distribution_in_db(
    pool, conn, synthetic_workspace
):
    """Verify all four role types appear in DB when chunks are supplied."""
    await generate_hypotheses_for_question(
        workspace_id=synthetic_workspace,
        parent_question="Role distribution test",
        candidate_evidence_chunk_ids=[f"chunk_{i}" for i in range(9)],
        pool=pool,
    )

    roles = await conn.fetch(
        """
        SELECT l.role, count(*) AS n
          FROM silver.hypothesis_evidence_links l
          JOIN silver.hypotheses h ON h.hypothesis_id = l.hypothesis_id
         WHERE h.workspace_id = $1::uuid
         GROUP BY l.role
        """,
        str(synthetic_workspace),
    )
    role_counts = {r["role"]: r["n"] for r in roles}
    # All four roles present.
    assert "supporting" in role_counts
    assert "contradicting" in role_counts
    assert "missing" in role_counts
    assert "recommended_test" in role_counts
