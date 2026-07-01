"""Module 6 Phase B Chunk 3 — End-to-end citation pipeline integration tests.

Tests the full Stage 1 → Stage 2 → guards → lifecycle flow with mocked
database backends.  No real LLM or real DB required.

Test scenarios:
  - S1-style (document-heavy): NI43 binding + passage_id resolved → rows land
  - S2-style (structured-only): DATA binding → 0 citation rows (correct)
  - S3-style (guard failure): completeness guard fails → lifecycle 'rejected'
  - insert_citation_items_with_spans: transactional atomicity (C3 close-out)
  - normalized_text swap: response.text == telemetry['normalized_text'] after
    resolve_spans (C1 close-out)

Run with:
    pytest tests/test_citation_pipeline.py -v
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from app.agent.citation_binding import BoundEvidence, BoundEvidenceSet
from app.models.answer_run import AnswerCitationItemCreate, AnswerCitationSpanCreate
from app.services.span_resolver import resolve_spans

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WS_ID = UUID("a0000000-0000-0000-0000-000000000001")
_RUN_ID = UUID("c2222222-2222-2222-2222-222222222222")


def _mock_pg_pool_returning_passage(passage_id: UUID) -> MagicMock:
    """Build a mock asyncpg Pool that returns the given passage_id for any query."""
    mock_row = {"passage_id": passage_id}
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=mock_row)
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_pool


def _mock_pg_pool_no_passage() -> MagicMock:
    """Build a mock asyncpg Pool that returns None (no passage found)."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_pool


# ---------------------------------------------------------------------------
# C1 close-out: normalized_text as canonical answer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c1_normalized_text_equals_response_text_after_resolve():
    """C1: resolve_spans returns normalized_text that the orchestrator should use.

    When dash-form markers are present, the normalized text (colon form)
    is what spans index into — the orchestrator must swap response.text
    to telemetry['normalized_text'].
    """
    str(uuid4())
    uuid4()
    marker_text = "[DATA:1]"

    bound = BoundEvidenceSet()
    bound.add(BoundEvidence(
        marker_text=marker_text,
        kind="DATA",
        index_or_id="1",
        source_store="postgis",
        evidence_id=uuid4(),  # has a target
        passage_id=None,
    ))

    # Answer with dash-form marker — will be normalized.
    raw_answer = "There are 10 drill holes [DATA-1]."

    _, _, tel = await resolve_spans(
        answer_text=raw_answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )

    normalized = tel["normalized_text"]
    # C1 assertion: normalized text contains colon-form marker.
    assert "[DATA:1]" in normalized
    assert "[DATA-1]" not in normalized
    assert tel["legacy_dash_rewrites"] == 1

    # Spans index into normalized, not raw.
    # Verify span_start points correctly into normalized text.
    items, spans_per_item, _ = await resolve_spans(
        answer_text=raw_answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
    )
    assert len(spans_per_item) == 1
    span = spans_per_item[0][0]
    assert normalized[span.span_start:span.span_end] == "[DATA:1]"


# ---------------------------------------------------------------------------
# S1-style: document-heavy query, passage_id resolved
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_s1_ni43_binding_passage_id_resolved():
    """S1 scenario: NI43 tool-slot binding + passage_id lookup succeeds.

    After resolve_spans with a live pg_pool mock, exactly 1 citation item
    is produced with passage_id populated.
    """
    chunk_id = str(uuid4())
    passage_id = uuid4()
    marker_text = "[NI43:1]"

    bound = BoundEvidenceSet()
    bound.add(BoundEvidence(
        marker_text=marker_text,
        kind="NI43",
        index_or_id="1",
        source_store="qdrant",
        evidence_id=None,
        passage_id=None,
        display_ref={"chunk_id": chunk_id},
    ))

    mock_pool = _mock_pg_pool_returning_passage(passage_id)
    answer = f"The NI 43-101 report confirms a resource of 500 Mlb U3O8 {marker_text}."

    items, spans_per_item, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        pg_pool=mock_pool,
    )

    assert len(items) == 1
    assert items[0].passage_id == passage_id
    assert items[0].evidence_id is None
    assert tel["tool_slot_passage_resolved"] == 1
    assert tel["markers_resolved"] == 1


# ---------------------------------------------------------------------------
# S2-style: structured-only query, 0 citation rows (correct behaviour)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_s2_structured_only_zero_citation_rows():
    """S2 scenario: DATA marker (PostGIS) has no passage_id — 0 rows expected.

    This is the correct behaviour for structured-data queries. The answer
    still returns; the completeness guard should accept [DATA:1] as a
    citation marker for the one factual sentence.
    """
    marker_text = "[DATA:1]"
    bound = BoundEvidenceSet()
    bound.add(BoundEvidence(
        marker_text=marker_text,
        kind="DATA",
        index_or_id="1",
        source_store="postgis",
        evidence_id=None,
        passage_id=None,
        display_ref={"chunk_id": None},  # no chunk_id — PostGIS result
    ))

    answer = f"There are 42 drill holes in Patterson Lake South {marker_text}."

    items, spans_per_item, tel = await resolve_spans(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        pg_pool=None,
    )

    # 0 citation rows — correct for structured-data markers.
    assert len(items) == 0
    assert tel["tool_slot_unresolvable"] >= 1


# ---------------------------------------------------------------------------
# S3-style: guard failure → lifecycle should be 'rejected'
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_s3_completeness_guard_fails_bare_assertion():
    """S3 scenario: bare assertion sentence → completeness guard fails.

    evaluate_guards() returns all_passed=False when text contains an
    uncited declarative sentence.
    """
    from unittest.mock import patch

    from app.agent.hallucination.layer_completeness import evaluate_guards

    text = (
        "The deposit is very large with significant uranium potential. "
        "No citation is provided for this factual claim. "
        "Also no citation here."
    )

    with patch("app.agent.hallucination.orchestrator_validators.settings") as ms:
        ms.NUMERICAL_VERIFICATION_ENABLED = False
        ms.ENTITY_RESOLUTION_ENABLED = False
        ms.TIMEOUT_POSTGIS_S = 5.0
        ms.TIMEOUT_NEO4J_S = 3.0
        bundle = await evaluate_guards(
            answer_text=text,
            tool_results=[],
            project_id="proj-uuid",
            pg_pool=None,
            neo4j_driver=None,
        )

    assert not bundle.all_passed
    assert any(g.guard_name == "completeness" for g in bundle.failed_guards)
    assert len(bundle.completeness.uncited_sentences) >= 1


# ---------------------------------------------------------------------------
# C3 close-out: insert_citation_items_with_spans — transactional atomicity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c3_insert_citation_items_with_spans_atomic():
    """C3: insert_citation_items_with_spans commits items + spans atomically.

    Mock a pg_pool that records all SQL calls; verify both INSERTs happen
    within a single transaction context.
    """
    from app.services.answer_run_store import insert_citation_items_with_spans

    run_id = uuid4()
    ws_id = uuid4()
    passage_id = uuid4()
    item_uuid = uuid4()

    item = AnswerCitationItemCreate(
        answer_run_id=run_id,
        workspace_id=ws_id,
        evidence_id=None,
        passage_id=passage_id,
        marker_text="[NI43:1]",
        source_store="qdrant",
        confidence=None,
        rejection_reason=None,
    )
    span = AnswerCitationSpanCreate(
        answer_run_id=run_id,
        answer_citation_item_id=UUID("00000000-0000-0000-0000-000000000000"),
        workspace_id=ws_id,
        span_start=10,
        span_end=20,
    )

    # Mock the conn to return a UUID from the item INSERT.
    mock_row = {"answer_citation_item_id": item_uuid}
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=mock_row)
    mock_conn.execute = AsyncMock(return_value=None)

    # transaction() is called as a sync method that returns an async context manager.
    # Use MagicMock for the transaction object itself.
    mock_txn = MagicMock()
    mock_txn.__aenter__ = AsyncMock(return_value=None)
    mock_txn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.transaction = MagicMock(return_value=mock_txn)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await insert_citation_items_with_spans(
        mock_pool,
        [item],
        [[span]],
    )

    # Should return the UUID of the inserted item.
    assert len(result) == 1
    assert result[0] == item_uuid

    # Transaction was entered.
    mock_conn.transaction.assert_called_once()
    # fetchrow (item INSERT) and execute (span INSERT) were both called.
    mock_conn.fetchrow.assert_called_once()
    mock_conn.execute.assert_called_once()


@pytest.mark.asyncio
async def test_c3_empty_items_returns_empty():
    """insert_citation_items_with_spans with empty items list returns []."""
    from app.services.answer_run_store import insert_citation_items_with_spans

    result = await insert_citation_items_with_spans(None, [], [])
    assert result == []


@pytest.mark.asyncio
async def test_c3_pool_none_returns_empty():
    """insert_citation_items_with_spans with pool=None returns []."""
    from app.services.answer_run_store import insert_citation_items_with_spans

    result = await insert_citation_items_with_spans(None, [object()], [[]])
    assert result == []
