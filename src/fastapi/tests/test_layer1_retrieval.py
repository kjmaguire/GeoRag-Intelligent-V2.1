"""Tests for the Layer 1 retrieval quality gate.

Coverage
--------
app.agent.hallucination.layer1_retrieval.assess_retrieval_quality()

Restored 2026-09-24 per CLAUDE.md hard rule 5 / Section 04i — see that
module's docstring for the full design. The critical property under test
is the cosine/RRF-fallback carve-out: DocumentSearchResult.rerank_degraded
scores must NEVER be compared against the calibrated-scale
RETRIEVAL_GATE_CONFIDENT_SCORE threshold, or every degraded-reranker query
would be flagged/refused regardless of actual relevance (the exact
RETRIEVAL_QUALITY_THRESHOLD incident recorded in config.py, under a new
name).

Run with:
    pytest tests/test_layer1_retrieval.py -v
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.hallucination.layer1_retrieval import (
    assess_retrieval_quality,
    build_refusal_text,
)
from app.agent.tools import DocumentChunk, DocumentSearchResult
from app.config import settings


def _chunk(chunk_id: str, score: float) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        text="Hole PLS-22-08 returned 1.85 g/t Au over 12.5 m.",
        source_document_id="doc-1",
        document_title="Technical Report",
        section_number="14.1",
        section_title="Mineral Resource Estimate",
        section="14.1 — Mineral Resource Estimate",
        page=112,
        document_type="NI43",
        report_id="report-1",
        relevance_score=score,
    )


def _doc_result(scores: list[float], *, rerank_degraded: bool = False) -> DocumentSearchResult:
    chunks = [_chunk(f"c{i}", s) for i, s in enumerate(scores)]
    return DocumentSearchResult(
        chunks=chunks,
        count=len(chunks),
        data_source="qdrant:georag_chunks (reranked)",
        rerank_degraded=rerank_degraded,
    )


class _OtherToolResult:
    """Stand-in for any non-DocumentSearchResult tool result carrying rows."""

    def __init__(self, count: int = 1) -> None:
        self.count = count


class TestZeroEvidenceRefusal:
    """The hard half — nothing cleared the floor from ANY store."""

    def test_empty_tool_results_refuses(self) -> None:
        verdict = assess_retrieval_quality([])
        assert verdict.refuse is True
        assert verdict.weak is False
        assert verdict.reason is not None
        assert "Layer 1" in verdict.reason

    def test_document_search_returned_nothing_and_nothing_else_ran(self) -> None:
        tool_results: list[tuple[str, Any]] = [
            ("search_documents", _doc_result([]))
        ]
        verdict = assess_retrieval_quality(tool_results)
        assert verdict.refuse is True

    def test_refusal_text_reads_as_a_refusal(self) -> None:
        # response_assembler._is_refusal keys off "i don't have" (among
        # other phrases) anywhere in the text — this pins the refusal
        # text to that vocabulary without importing response_assembler
        # (which pulls in llm_calls / tools at module scope).
        text = build_refusal_text()
        assert text
        assert "i don't have" in text.lower()


class TestOtherEvidencePresentIsNotALayer1Concern:
    """Zero document chunks but a structured tool grounded the answer."""

    def test_structured_tool_data_prevents_refusal(self) -> None:
        tool_results: list[tuple[str, Any]] = [
            ("search_documents", _doc_result([])),
            ("query_spatial_collars", _OtherToolResult(count=5)),
        ]
        verdict = assess_retrieval_quality(tool_results)
        assert verdict.refuse is False
        assert verdict.weak is False
        assert verdict.other_evidence_present is True

    def test_no_document_search_call_at_all(self) -> None:
        tool_results: list[tuple[str, Any]] = [
            ("query_spatial_collars", _OtherToolResult(count=5)),
        ]
        verdict = assess_retrieval_quality(tool_results)
        assert verdict.refuse is False
        assert verdict.document_chunks_considered == 0


class TestRealRerankerScores:
    """rerank_degraded=False — relevance_score is on a calibrated [0,1] scale."""

    def test_confident_scores_pass_clean(self) -> None:
        tool_results: list[tuple[str, Any]] = [
            ("search_documents", _doc_result([0.6, 0.55, 0.5])),
        ]
        verdict = assess_retrieval_quality(tool_results)
        assert verdict.refuse is False
        assert verdict.weak is False
        assert verdict.used_cosine_fallback is False
        assert verdict.document_chunks_confident == 3

    def test_marginal_scores_are_weak_not_refused(self) -> None:
        # All three cleared the per-chunk floor (already applied upstream
        # in search_documents) but sit just above it -- below the
        # confident threshold.
        tool_results: list[tuple[str, Any]] = [
            ("search_documents", _doc_result([0.21, 0.22, 0.23])),
        ]
        verdict = assess_retrieval_quality(tool_results)
        assert verdict.refuse is False
        assert verdict.weak is True
        assert verdict.document_chunks_confident == 0
        assert verdict.reason is not None
        assert "Layer 1" in verdict.reason

    def test_one_confident_chunk_among_weak_ones_is_not_weak(self) -> None:
        tool_results: list[tuple[str, Any]] = [
            ("search_documents", _doc_result([0.21, 0.6])),
        ]
        verdict = assess_retrieval_quality(tool_results)
        assert verdict.weak is False
        assert verdict.document_chunks_confident == 1


class TestCosineRrfFallback:
    """rerank_degraded=True — scores are RAW RRF fusion order, NOT calibrated.

    This is the case CLAUDE.md's task brief calls out explicitly: the gate
    must not refuse/flag every query just because the reranker fell back
    to cosine/RRF order.
    """

    def test_thin_fallback_coverage_is_weak_but_not_refused(self) -> None:
        tool_results: list[tuple[str, Any]] = [
            ("search_documents", _doc_result([0.016], rerank_degraded=True)),
        ]
        settings_min = settings.RETRIEVAL_GATE_MIN_CHUNKS
        verdict = assess_retrieval_quality(tool_results)
        assert verdict.refuse is False
        assert verdict.used_cosine_fallback is True
        if settings_min > 1:
            assert verdict.weak is True

    def test_low_rrf_scores_are_never_compared_to_the_confident_threshold(
        self,
    ) -> None:
        """The critical regression guard.

        Real RRF fusion scores (~1/(k+rank), e.g. 0.016) sit an order of
        magnitude below RETRIEVAL_GATE_CONFIDENT_SCORE (0.35 default). If
        this function ever compared them directly, EVERY fallback query
        with more than one chunk would still report weak=False for the
        wrong reason (confident==0 would look like a scoring failure, not
        "we don't score this path") -- and any accidental use of the
        confident threshold on this path would flag or refuse nearly
        every degraded-reranker query. Pin: document_chunks_confident is
        always 0 on the fallback path (meaningless on this scale, per the
        module docstring) and weak is driven by COUNT alone.
        """
        many_low_score_chunks = [0.016, 0.015, 0.014, 0.013, 0.012]
        tool_results: list[tuple[str, Any]] = [
            (
                "search_documents",
                _doc_result(many_low_score_chunks, rerank_degraded=True),
            ),
        ]
        verdict = assess_retrieval_quality(tool_results)
        assert verdict.refuse is False
        assert verdict.used_cosine_fallback is True
        assert verdict.document_chunks_confident == 0
        # 5 >= the default RETRIEVAL_GATE_MIN_CHUNKS (1) -- not weak either.
        assert verdict.weak is False

    def test_fallback_reason_never_mentions_a_score_threshold(self) -> None:
        tool_results: list[tuple[str, Any]] = [
            ("search_documents", _doc_result([0.01], rerank_degraded=True)),
        ]
        verdict = assess_retrieval_quality(tool_results)
        if verdict.weak:
            assert verdict.reason is not None
            assert "confident threshold" not in verdict.reason


class TestFeatureFlag:
    def test_disabled_never_refuses_or_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "RETRIEVAL_QUALITY_GATE_ENABLED", False, raising=False)
        verdict = assess_retrieval_quality([])
        assert verdict.refuse is False
        assert verdict.weak is False
