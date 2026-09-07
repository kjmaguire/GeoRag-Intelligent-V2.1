"""Unit tests for the answer-run Pydantic models in ``app/models/answer_run.py``.

These validators used to be covered as a side-effect of
``tests/test_citation_lifecycle.py`` and
``tests/test_answer_run_confidence_latency.py``, both deleted 2026-09-07
together with the unwired ``services/citation_lifecycle.py`` and
``services/answer_run_store.py`` modules they exercised. The live writer of
``silver.answer_runs`` is the inline SQL in
``agent/agentic_retrieval/nodes.py::persist_node``; the models are still
what that node builds from, so their validators keep their coverage here.

Pure model tests — no database.
"""

from __future__ import annotations

import typing
from uuid import UUID, uuid4

import pytest

from app.models.answer_run import (
    AnswerCitationItemCreate,
    AnswerCitationSpanCreate,
    AnswerRunCreate,
    CitationLifecycleState,
    CitationMode,
)
from app.models.evidence import EvidenceItemCreate

_WS_ID = UUID("a0000000-0000-0000-0000-000000000001")
_RUN_ID = uuid4()


# ---------------------------------------------------------------------------
# AnswerRunCreate — confidence / latency_ms / rejection_reason
# ---------------------------------------------------------------------------


class TestAnswerRunCreateOptionalFields:
    """confidence + latency_ms + rejection_reason validation."""

    def _base_kwargs(self) -> dict:
        return {
            "workspace_id": _WS_ID,
            "query_text": "tell me about hole 36-1085",
            "query_class": "factual",
            "workspace_data_version_at_query": 1,
        }

    def test_fields_default_to_none(self) -> None:
        run = AnswerRunCreate(**self._base_kwargs())
        assert run.confidence is None
        assert run.latency_ms is None
        assert run.rejection_reason is None

    def test_confidence_accepts_zero_and_one(self) -> None:
        AnswerRunCreate(**self._base_kwargs(), confidence=0.0)
        AnswerRunCreate(**self._base_kwargs(), confidence=1.0)
        AnswerRunCreate(**self._base_kwargs(), confidence=0.873)

    def test_confidence_rejects_above_one(self) -> None:
        with pytest.raises(Exception):
            AnswerRunCreate(**self._base_kwargs(), confidence=1.001)

    def test_confidence_rejects_negative(self) -> None:
        with pytest.raises(Exception):
            AnswerRunCreate(**self._base_kwargs(), confidence=-0.0001)

    def test_latency_ms_accepts_zero_and_positive(self) -> None:
        AnswerRunCreate(**self._base_kwargs(), latency_ms=0)
        AnswerRunCreate(**self._base_kwargs(), latency_ms=12345)

    def test_latency_ms_rejects_negative(self) -> None:
        with pytest.raises(Exception):
            AnswerRunCreate(**self._base_kwargs(), latency_ms=-1)

    def test_rejection_reason_accepts_free_text(self) -> None:
        run = AnswerRunCreate(
            **self._base_kwargs(),
            rejection_reason="llm_unavailable",
        )
        assert run.rejection_reason == "llm_unavailable"


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
    """CitationLifecycleState is a Literal type alias for the state values.

    All five values remain valid for the CHECK constraint on
    ``silver.answer_runs.citation_lifecycle_state``; as built,
    ``persist_node`` only ever writes ``committed`` or ``rejected``.
    """
    args = typing.get_args(CitationLifecycleState)
    assert set(args) == {"draft", "generated", "validated", "committed", "rejected"}


def test_citation_mode_alias_is_literal() -> None:
    """CitationMode is a Literal type alias for the mode values."""
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
        structured_ref={
            "schema": "silver",
            "table": "collars",
            "pk": {"collar_id": "abc"},
        },
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
