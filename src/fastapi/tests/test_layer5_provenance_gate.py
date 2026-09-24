"""Tests for the Layer 5 chunk-provenance GATE (as opposed to enrichment).

Coverage
--------
app.agent.hallucination.layer5_provenance.gate_citation_provenance()

Restored 2026-09-24 per CLAUDE.md hard rule 5 / Section 04i: "provenance is
enrichment, not a gate" is the documented gap this closes. The
`enrich_provenance` function (existing, unchanged by this restoration) is
covered by tests/test_agentic_retrieval_graph.py's "Hard rule #5" section.

The critical property under test is the cross-tenant / stale-citation
case: a Citation whose source_chunk_id names a chunk that was NOT part of
THIS query's retrieved set must be rejected, even though retrieval itself
is already workspace-scoped server-side (this is defense-in-depth against
a bug or a stale citation surviving a retry, not a first line of defense).

Run with:
    pytest tests/test_layer5_provenance_gate.py -v
"""

from __future__ import annotations

import pytest

from app.agent.hallucination.layer5_provenance import gate_citation_provenance
from app.agent.tools import DocumentChunk, DocumentSearchResult
from app.config import settings
from app.models.rag import Citation, GeoRAGResponse


def _chunk(chunk_id: str, *, report_id: str = "report-1") -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        text="Hole PLS-22-08 returned 1.85 g/t Au over 12.5 m.",
        source_document_id=report_id,
        document_title="Technical Report",
        section_number="14.1",
        section_title="Mineral Resource Estimate",
        section="14.1 — Mineral Resource Estimate",
        page=112,
        document_type="NI43",
        report_id=report_id,
        relevance_score=0.7,
    )


def _doc_source_id(*, report_id: str = "report-1", chunk_id: str = "c1") -> str:
    return f"georag_reports:{report_id}:section=14.1:chunk={chunk_id}"


def _citation(source_chunk_id: str, citation_id: str = "[NI43-1]") -> Citation:
    return Citation(
        citation_id=citation_id,
        citation_type="NI43",
        source_chunk_id=source_chunk_id,
        document_title="Technical Report",
        section="14.1",
        page=112,
        relevance_score=0.7,
    )


def _response(citations: list[Citation], text: str | None = None) -> GeoRAGResponse:
    return GeoRAGResponse(
        text=text or " ".join(c.citation_id for c in citations) + " summary.",
        citations=citations,
        confidence=0.8,
        sources_used=[c.source_chunk_id for c in citations],
    )


class TestNonDocumentChunkCitationsAreUntouched:
    """DATA / PGEO / sentinel citations are out of scope for this gate —
    same scope enrich_provenance already uses."""

    def test_data_citation_passes_through(self) -> None:
        citation = Citation(
            citation_id="[DATA-1]",
            citation_type="DATA",
            source_chunk_id="silver.collars:count=1:first=abc",
            document_title="Collar data",
            relevance_score=0.9,
        )
        response = _response([citation])
        gated, warnings = gate_citation_provenance(
            response, tool_results=[]
        )
        assert warnings == []
        assert gated is response  # unchanged, same object

    def test_no_tool_call_sentinel_passes_through(self) -> None:
        citation = Citation(
            citation_id="[DATA-1]",
            citation_type="DATA",
            source_chunk_id="no-tool-call",
            document_title="No tool call executed",
            relevance_score=0.0,
        )
        response = _response([citation])
        gated, warnings = gate_citation_provenance(response, tool_results=[])
        assert warnings == []
        assert gated is response


class TestChunkMembership:
    def test_citation_matching_a_retrieved_chunk_is_kept(self) -> None:
        doc_result = DocumentSearchResult(
            chunks=[_chunk("c1"), _chunk("c2")],
            count=2,
            data_source="qdrant:georag_chunks (reranked)",
        )
        response = _response([_citation(_doc_source_id(chunk_id="c1"))])
        gated, warnings = gate_citation_provenance(
            response, tool_results=[("search_documents", doc_result)]
        )
        assert warnings == []
        assert len(gated.citations) == 1
        assert gated.citations[0].source_chunk_id == _doc_source_id(chunk_id="c1")

    def test_citation_for_an_unretrieved_chunk_is_rejected(self) -> None:
        """The core regression guard: a chunk id that was never retrieved
        for THIS query (stale citation, cross-tenant leak, or an
        adversarial marker colliding with a real id elsewhere)."""
        doc_result = DocumentSearchResult(
            chunks=[_chunk("c1")],
            count=1,
            data_source="qdrant:georag_chunks (reranked)",
        )
        bad_citation = _citation(_doc_source_id(chunk_id="chunk-from-another-query"))
        response = _response([bad_citation])
        gated, warnings = gate_citation_provenance(
            response, tool_results=[("search_documents", doc_result)]
        )
        assert len(warnings) == 1
        assert "Layer 5" in warnings[0]
        assert "chunk-from-another-query" in warnings[0]
        # Rejected citation dropped; placeholder inserted so
        # GeoRAGResponse.citations stays non-empty (Pydantic min_length=1).
        assert len(gated.citations) == 1
        assert gated.citations[0].source_chunk_id == "provenance-rejected"

    def test_mixed_good_and_bad_citations(self) -> None:
        doc_result = DocumentSearchResult(
            chunks=[_chunk("c1"), _chunk("c2")],
            count=2,
            data_source="qdrant:georag_chunks (reranked)",
        )
        good = _citation(_doc_source_id(chunk_id="c1"), citation_id="[NI43-1]")
        bad = _citation(_doc_source_id(chunk_id="not-retrieved"), citation_id="[NI43-2]")
        response = _response([good, bad])
        gated, warnings = gate_citation_provenance(
            response, tool_results=[("search_documents", doc_result)]
        )
        assert len(warnings) == 1
        assert len(gated.citations) == 1
        assert gated.citations[0].citation_id == "[NI43-1]"

    def test_second_document_search_call_contributes_chunk_ids_too(self) -> None:
        """An adversarial second pass (hypothesis_generation profile) also
        retrieves chunks — its results must count toward the retrieved set."""
        primary = DocumentSearchResult(
            chunks=[_chunk("c1")], count=1, data_source="qdrant (reranked)"
        )
        adversarial = DocumentSearchResult(
            chunks=[_chunk("c2")], count=1, data_source="qdrant (reranked)"
        )
        response = _response([_citation(_doc_source_id(chunk_id="c2"))])
        gated, warnings = gate_citation_provenance(
            response,
            tool_results=[
                ("search_documents", primary),
                ("search_documents_adversarial", adversarial),
            ],
        )
        assert warnings == []
        assert len(gated.citations) == 1


class TestMissingDocumentId:
    @pytest.mark.parametrize("report_id", ["", "empty", "unknown", "none"])
    def test_placeholder_report_id_is_rejected(self, report_id: str) -> None:
        doc_result = DocumentSearchResult(
            chunks=[_chunk("c1", report_id=report_id)],
            count=1,
            data_source="qdrant (reranked)",
        )
        response = _response(
            [_citation(_doc_source_id(report_id=report_id, chunk_id="c1"))]
        )
        gated, warnings = gate_citation_provenance(
            response, tool_results=[("search_documents", doc_result)]
        )
        assert len(warnings) == 1
        assert "no document id" in warnings[0]


class TestAllCitationsRejected:
    def test_placeholder_citation_keeps_response_valid(self) -> None:
        response = _response([_citation(_doc_source_id(chunk_id="ghost"))])
        gated, warnings = gate_citation_provenance(response, tool_results=[])
        assert len(warnings) == 1
        assert len(gated.citations) == 1
        assert gated.citations[0].source_chunk_id == "provenance-rejected"
        # Still a structurally valid GeoRAGResponse.
        assert gated.citations[0].relevance_score == 0.0


class TestFeatureFlag:
    def test_disabled_never_rejects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "CHUNK_PROVENANCE_GATE_ENABLED", False, raising=False)
        response = _response([_citation(_doc_source_id(chunk_id="ghost"))])
        gated, warnings = gate_citation_provenance(response, tool_results=[])
        assert warnings == []
        assert gated is response


class TestNoCitations:
    def test_empty_citations_list_is_a_noop(self) -> None:
        # Not constructible via GeoRAGResponse (min_length=1 on citations),
        # so this exercises the function's own defensive guard directly —
        # a caller passing an already-invalid response must not crash.
        from unittest.mock import MagicMock

        fake_response = MagicMock(citations=[])
        gated, warnings = gate_citation_provenance(fake_response, tool_results=[])
        assert warnings == []
        assert gated is fake_response
