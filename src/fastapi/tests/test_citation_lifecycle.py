"""Unit tests for the Module 6 Phase B Chunk 1 citation lifecycle state machine.

Tests cover:
  - transition_lifecycle() correct state writes
  - Expected transition sequence (draft → generated → validated → committed)
  - Rejected path (draft → generated → rejected)
  - Pool=None guard (no exception raised)
  - AnswerCitationItemCreate model validators
  - AnswerCitationSpanCreate model validators
  - EvidenceItemCreate exactly_one_ref validator (SCHEMA-03)
  - CitationLifecycleState / CitationMode re-export aliases

These are unit tests — all DB calls use a mock pool so no live database
is required.  Integration tests (live DB) run as part of the acceptance
test suite in test_golden_queries.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from app.models.answer_run import (
    AnswerCitationItemCreate,
    AnswerCitationSpanCreate,
    CitationLifecycleState,
    CitationMode,
)
from app.models.evidence import EvidenceItemCreate
from app.services.citation_lifecycle import (
    transition_lifecycle,
    transition_to_committed,
    transition_to_draft,
    transition_to_generated,
    transition_to_rejected,
    transition_to_validated,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WS_ID = UUID("a0000000-0000-0000-0000-000000000001")
_RUN_ID = uuid4()


def _mock_pool() -> MagicMock:
    """Return an asyncpg pool mock that accepts acquire() context managers."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


# ---------------------------------------------------------------------------
# transition_lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_lifecycle_happy_path() -> None:
    """transition_lifecycle writes expected state to the DB.

    Verifies the full draft → generated → validated → committed sequence.
    """
    pool = _mock_pool()
    conn = pool.acquire.return_value.__aenter__.return_value

    transitions = ["draft", "generated", "validated", "committed"]
    for state in transitions:
        await transition_lifecycle(pool, _RUN_ID, state)  # type: ignore[arg-type]

    assert conn.execute.call_count == 4, "Expected one execute() per transition"

    # Verify the last call wrote 'committed'
    last_call_args = conn.execute.call_args_list[-1]
    # args[0] is the SQL, args[1] is $1 (state), args[2] is $2 (answer_run_id)
    assert last_call_args.args[1] == "committed"
    assert last_call_args.args[2] == str(_RUN_ID)


@pytest.mark.asyncio
async def test_transition_lifecycle_rejected_path() -> None:
    """transition_lifecycle → 'rejected' writes correctly and logs reason."""
    pool = _mock_pool()
    conn = pool.acquire.return_value.__aenter__.return_value

    await transition_lifecycle(pool, _RUN_ID, "draft")
    await transition_lifecycle(pool, _RUN_ID, "generated")
    await transition_lifecycle(pool, _RUN_ID, "rejected", rejection_reason="L6 constraint violated")

    assert conn.execute.call_count == 3
    last_call_args = conn.execute.call_args_list[-1]
    assert last_call_args.args[1] == "rejected"


@pytest.mark.asyncio
async def test_transition_lifecycle_pool_none_does_not_raise() -> None:
    """transition_lifecycle with pool=None is a no-op (non-fatal)."""
    # Should not raise; returns silently.
    await transition_lifecycle(None, _RUN_ID, "draft")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_transition_lifecycle_db_error_does_not_raise() -> None:
    """transition_lifecycle swallows DB errors so observability never fails a query."""
    pool = MagicMock()
    conn = AsyncMock()
    conn.execute = AsyncMock(side_effect=Exception("connection reset"))
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    # Should not raise.
    await transition_lifecycle(pool, _RUN_ID, "generated")


@pytest.mark.asyncio
async def test_convenience_wrappers_call_correct_states() -> None:
    """Convenience wrappers pass the correct state string to transition_lifecycle."""
    pool = _mock_pool()
    conn = pool.acquire.return_value.__aenter__.return_value

    await transition_to_draft(pool, _RUN_ID)
    await transition_to_generated(pool, _RUN_ID)
    await transition_to_validated(pool, _RUN_ID)
    await transition_to_committed(pool, _RUN_ID)
    await transition_to_rejected(pool, _RUN_ID, reason="guard failure")

    states_written = [call.args[1] for call in conn.execute.call_args_list]
    assert states_written == [
        "draft",
        "generated",
        "validated",
        "committed",
        "rejected",
    ]


# ---------------------------------------------------------------------------
# AnswerCitationItemCreate model validator tests
# ---------------------------------------------------------------------------


def test_answer_citation_item_requires_target_with_evidence_id() -> None:
    """AnswerCitationItemCreate accepts when evidence_id is set."""
    item = AnswerCitationItemCreate(
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        evidence_id=uuid4(),
        marker_text="[ev:a1b2c3d4]",
    )
    assert item.evidence_id is not None
    assert item.passage_id is None


def test_answer_citation_item_requires_target_with_passage_id() -> None:
    """AnswerCitationItemCreate accepts when passage_id is set."""
    item = AnswerCitationItemCreate(
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        passage_id=uuid4(),
        marker_text="[DATA:1]",
    )
    assert item.passage_id is not None
    assert item.evidence_id is None


def test_answer_citation_item_rejects_both_null() -> None:
    """AnswerCitationItemCreate raises when both evidence_id and passage_id are None."""
    with pytest.raises(ValueError, match="requires at least one of"):
        AnswerCitationItemCreate(
            answer_run_id=_RUN_ID,
            workspace_id=_WS_ID,
            marker_text="[DATA:1]",
        )


def test_answer_citation_item_accepts_both_set() -> None:
    """AnswerCitationItemCreate accepts when both evidence_id and passage_id are set."""
    # Both non-None is allowed (evidence_id is preferred but passage_id retained for
    # the Chunk 2 dual-support window).
    item = AnswerCitationItemCreate(
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        evidence_id=uuid4(),
        passage_id=uuid4(),
        marker_text="[NI43:1]",
    )
    assert item.evidence_id is not None
    assert item.passage_id is not None


def test_answer_citation_item_confidence_range() -> None:
    """AnswerCitationItemCreate rejects confidence outside [0, 1]."""
    with pytest.raises(ValueError):
        AnswerCitationItemCreate(
            answer_run_id=_RUN_ID,
            workspace_id=_WS_ID,
            evidence_id=uuid4(),
            marker_text="[DATA:1]",
            confidence=1.5,
        )


# ---------------------------------------------------------------------------
# AnswerCitationSpanCreate model validator tests
# ---------------------------------------------------------------------------


def test_answer_citation_span_valid() -> None:
    """AnswerCitationSpanCreate accepts a valid span."""
    span = AnswerCitationSpanCreate(
        answer_run_id=_RUN_ID,
        answer_citation_item_id=uuid4(),
        workspace_id=_WS_ID,
        span_start=10,
        span_end=20,
    )
    assert span.span_start == 10
    assert span.span_end == 20


def test_answer_citation_span_rejects_equal_offsets() -> None:
    """AnswerCitationSpanCreate raises when span_end == span_start."""
    with pytest.raises(ValueError, match="span_end.*must be strictly greater"):
        AnswerCitationSpanCreate(
            answer_run_id=_RUN_ID,
            answer_citation_item_id=uuid4(),
            workspace_id=_WS_ID,
            span_start=10,
            span_end=10,
        )


def test_answer_citation_span_rejects_reversed_range() -> None:
    """AnswerCitationSpanCreate raises when span_end < span_start."""
    with pytest.raises(ValueError, match="span_end.*must be strictly greater"):
        AnswerCitationSpanCreate(
            answer_run_id=_RUN_ID,
            answer_citation_item_id=uuid4(),
            workspace_id=_WS_ID,
            span_start=20,
            span_end=5,
        )


def test_answer_citation_span_rejects_negative_start() -> None:
    """AnswerCitationSpanCreate raises when span_start < 0 (Pydantic ge=0)."""
    with pytest.raises(ValueError):
        AnswerCitationSpanCreate(
            answer_run_id=_RUN_ID,
            answer_citation_item_id=uuid4(),
            workspace_id=_WS_ID,
            span_start=-1,
            span_end=5,
        )


# ---------------------------------------------------------------------------
# CitationLifecycleState / CitationMode re-export alias tests
# ---------------------------------------------------------------------------


def test_citation_lifecycle_state_alias_is_literal() -> None:
    """CitationLifecycleState is a Literal type alias for the state values."""
    import typing
    args = typing.get_args(CitationLifecycleState)
    assert set(args) == {"draft", "generated", "validated", "committed", "rejected"}


def test_citation_mode_alias_is_literal() -> None:
    """CitationMode is a Literal type alias for the mode values."""
    import typing
    args = typing.get_args(CitationMode)
    assert set(args) == {"posthoc_span_resolution", "hybrid_delayed_attachment"}


# ---------------------------------------------------------------------------
# EvidenceItemCreate exactly_one_ref validator (SCHEMA-03)
# ---------------------------------------------------------------------------


def test_evidence_item_exactly_one_ref_passage() -> None:
    """EvidenceItemCreate accepts when only passage_id is set."""
    item = EvidenceItemCreate(
        workspace_id=_WS_ID,
        evidence_type="document_passage",
        passage_id=uuid4(),
        source_uri="s3://bronze/test.pdf",
    )
    assert item.passage_id is not None


def test_evidence_item_exactly_one_ref_structured() -> None:
    """EvidenceItemCreate accepts when only structured_ref is set."""
    item = EvidenceItemCreate(
        workspace_id=_WS_ID,
        evidence_type="structured_record",
        structured_ref={"schema": "silver", "table": "collars", "pk": {"collar_id": "abc"}},
        source_uri="s3://bronze/collars.csv",
    )
    assert item.structured_ref is not None


def test_evidence_item_rejects_zero_refs() -> None:
    """EvidenceItemCreate raises when no ref fields are set."""
    with pytest.raises(ValueError, match="exactly one of"):
        EvidenceItemCreate(
            workspace_id=_WS_ID,
            evidence_type="document_passage",
            source_uri="s3://bronze/test.pdf",
        )


def test_evidence_item_rejects_two_refs() -> None:
    """EvidenceItemCreate raises when two ref fields are set."""
    with pytest.raises(ValueError, match="exactly one of"):
        EvidenceItemCreate(
            workspace_id=_WS_ID,
            evidence_type="document_passage",
            passage_id=uuid4(),
            structured_ref={"schema": "silver", "table": "collars", "pk": {}},
            source_uri="s3://bronze/test.pdf",
        )


def test_evidence_item_rejects_type_field_mismatch() -> None:
    """EvidenceItemCreate raises when evidence_type mismatches the populated ref field."""
    with pytest.raises(ValueError, match="evidence_type="):
        EvidenceItemCreate(
            workspace_id=_WS_ID,
            evidence_type="graph_edge",  # says graph_edge but populates passage_id
            passage_id=uuid4(),
            source_uri="s3://bronze/test.pdf",
        )
