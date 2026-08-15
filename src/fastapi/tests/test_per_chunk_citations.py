"""RAG-quality audit 2026-08-14 — findings 1 + 3.

Finding 1 — per-chunk citation ids:
    ``assign_citation_ids`` emits one id PER document chunk (mirroring the
    PGEO per-record branch) so each ``[NI43-n]``/``[PUB-n]`` maps to the
    real chunk_id / section / page that grounded the sentence. The context
    renderer (``nodes._render_tool_results_context``) and the assembler
    (``assemble_response``) must stay in lockstep — Layer 2 strips any
    marker without a matching Citation.

Finding 3 — empty-retrieval sentinel drift:
    The IND-6 ungrounded-answer guard and
    ``confidence_computer._count_independent_sources`` share one sentinel
    set (``EMPTY_SOURCE_SENTINELS``) so a zero-evidence run deterministically
    takes the low-confidence refusal path.

Run with:
    pytest tests/test_per_chunk_citations.py -v
"""

from __future__ import annotations

from app.agent.agentic_retrieval.nodes import _render_tool_results_context
from app.agent.confidence_computer import _count_independent_sources
from app.agent.response_assembler import (
    EMPTY_SOURCE_SENTINELS,
    assemble_response,
    assign_citation_ids,
)
from app.agent.tools import DocumentChunk, DocumentSearchResult
from app.models.rag import Citation

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_chunk(
    n: int,
    *,
    document_type: str = "NI43",
    page: int | None = None,
) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=f"chunk-uuid-{n:03d}",
        report_id=f"rep-{n:03d}",
        source_document_id=f"rep-{n:03d}",
        document_title=f"Technical Report {n}",
        document_type=document_type,
        text=f"Chunk {n}: intercept of {10 + n} m at 0.{n}% U3O8.",
        relevance_score=0.9 - n * 0.05,
        section_number=f"{n}.1",
        section_title=f"Section {n}",
        section=f"{n}.1 — Section {n}",
        page=page if page is not None else 10 + n,
    )


def _make_doc_result(n_chunks: int, **chunk_kwargs) -> DocumentSearchResult:
    return DocumentSearchResult(
        chunks=[_make_chunk(i + 1, **chunk_kwargs) for i in range(n_chunks)],
        count=n_chunks,
        data_source="Qdrant georag_reports",
    )


def _empty_doc_result() -> DocumentSearchResult:
    return DocumentSearchResult(chunks=[], count=0, data_source="Qdrant georag_reports")


# ---------------------------------------------------------------------------
# Finding 1 — assign_citation_ids emits one id per chunk
# ---------------------------------------------------------------------------


class TestPerChunkIdAssignment:
    def test_three_chunks_yield_three_distinct_ids(self) -> None:
        bundles = assign_citation_ids([("search_documents", _make_doc_result(3))])
        assert bundles == [["[NI43-1]", "[NI43-2]", "[NI43-3]"]]

    def test_pub_chunks_get_pub_prefix_per_chunk(self) -> None:
        bundles = assign_citation_ids(
            [("search_documents", _make_doc_result(2, document_type="PUB"))]
        )
        assert bundles == [["[PUB-1]", "[PUB-2]"]]

    def test_mixed_document_types_labelled_per_chunk(self) -> None:
        result = DocumentSearchResult(
            chunks=[
                _make_chunk(1, document_type="NI43"),
                _make_chunk(2, document_type="PUB"),
            ],
            count=2,
            data_source="Qdrant georag_reports",
        )
        bundles = assign_citation_ids([("search_documents", result)])
        assert bundles == [["[NI43-1]", "[PUB-2]"]]

    def test_shared_counter_across_tools_still_interleaves(self) -> None:
        from app.agent.tools import SpatialQueryResult

        spatial = SpatialQueryResult(collars=[], count=3, data_source="PostGIS")
        bundles = assign_citation_ids(
            [
                ("query_spatial_collars", spatial),
                ("search_documents", _make_doc_result(2)),
            ]
        )
        assert bundles == [["[DATA-1]"], ["[NI43-2]", "[NI43-3]"]]

    def test_empty_doc_result_keeps_one_sentinel_id(self) -> None:
        """Empty retrieval still yields exactly one id so the
        georag_reports:empty sentinel citation survives for IND-6."""
        bundles = assign_citation_ids([("search_documents", _empty_doc_result())])
        assert bundles == [["[NI43-1]"]]


# ---------------------------------------------------------------------------
# Finding 1 — assemble_response emits one Citation per chunk
# ---------------------------------------------------------------------------


class TestPerChunkCitations:
    def test_each_chunk_gets_its_own_citation(self) -> None:
        response = assemble_response(
            text="Grades increase to the east [NI43-1] [NI43-2] [NI43-3].",
            tool_results=[("search_documents", _make_doc_result(3))],
        )
        assert len(response.citations) == 3
        assert [c.citation_id for c in response.citations] == [
            "[NI43-1]",
            "[NI43-2]",
            "[NI43-3]",
        ]

    def test_source_chunk_ids_bind_to_each_chunk(self) -> None:
        response = assemble_response(
            text="[NI43-1] [NI43-2] [NI43-3].",
            tool_results=[("search_documents", _make_doc_result(3))],
        )
        chunk_ids = [c.source_chunk_id for c in response.citations]
        assert chunk_ids == [
            "georag_reports:rep-001:section=1.1:chunk=chunk-uuid-001",
            "georag_reports:rep-002:section=2.1:chunk=chunk-uuid-002",
            "georag_reports:rep-003:section=3.1:chunk=chunk-uuid-003",
        ]
        assert len(set(chunk_ids)) == 3

    def test_section_and_page_are_per_chunk(self) -> None:
        """The pre-fix behaviour bound every citation to chunk 1's
        section/page — this pins the per-chunk binding."""
        response = assemble_response(
            text="[NI43-1] [NI43-2].",
            tool_results=[("search_documents", _make_doc_result(2))],
        )
        assert response.citations[0].page == 11
        assert response.citations[1].page == 12
        assert response.citations[0].section == "1.1 — Section 1"
        assert response.citations[1].section == "2.1 — Section 2"

    def test_relevance_and_title_are_per_chunk(self) -> None:
        response = assemble_response(
            text="[NI43-1] [NI43-2].",
            tool_results=[("search_documents", _make_doc_result(2))],
        )
        assert response.citations[0].document_title == "Technical Report 1"
        assert response.citations[1].document_title == "Technical Report 2"
        assert response.citations[0].relevance_score != response.citations[1].relevance_score

    def test_sources_used_lists_every_chunk(self) -> None:
        response = assemble_response(
            text="[NI43-1] [NI43-2] [NI43-3].",
            tool_results=[("search_documents", _make_doc_result(3))],
        )
        assert len(response.sources_used) == 3


# ---------------------------------------------------------------------------
# Finding 1 — renderer ↔ assembler lockstep
# ---------------------------------------------------------------------------


class TestRendererAssemblerLockstep:
    def test_context_markers_match_assembled_citation_ids(self) -> None:
        """Every id the renderer writes into the LLM context must exist as
        an assembled Citation — otherwise Layer 2 strips the marker as an
        orphan and the citation chain breaks."""
        tool_results = [("search_documents", _make_doc_result(3))]
        context = _render_tool_results_context(tool_results)
        response = assemble_response(
            text="answer [NI43-1] [NI43-2] [NI43-3].",
            tool_results=tool_results,
        )
        assembled_ids = {c.citation_id for c in response.citations}
        for cid in ("[NI43-1]", "[NI43-2]", "[NI43-3]"):
            assert cid in context
            assert cid in assembled_ids

    def test_each_chunk_block_carries_its_own_id(self) -> None:
        context = _render_tool_results_context(
            [("search_documents", _make_doc_result(2))]
        )
        blocks = context.split("\n\n")
        assert blocks[0].startswith("[NI43-1] Technical Report 1")
        assert blocks[1].startswith("[NI43-2] Technical Report 2")

    def test_chunk_count_in_context_unchanged_by_per_chunk_ids(self) -> None:
        """Context-budget interaction guard: per-chunk ids must not change
        how many chunk blocks are rendered (one per chunk, as before)."""
        n = 5
        context = _render_tool_results_context(
            [("search_documents", _make_doc_result(n))]
        )
        assert context.count("Technical Report") == n

    def test_per_chunk_pages_render_in_context(self) -> None:
        context = _render_tool_results_context(
            [("search_documents", _make_doc_result(2))]
        )
        assert "page 11" in context
        assert "page 12" in context


# ---------------------------------------------------------------------------
# Finding 3 — empty-retrieval sentinels unified with IND-6
# ---------------------------------------------------------------------------


class TestEmptyRetrievalSentinels:
    def test_zero_hit_retrieval_floors_confidence(self) -> None:
        """A run whose ONLY evidence is an empty search result must not
        ship at normal confidence — the IND-6 guard now filters the
        georag_reports:empty sentinel."""
        response = assemble_response(
            text="The project hosts extensive high-grade mineralization.",
            tool_results=[("search_documents", _empty_doc_result())],
        )
        assert response.confidence <= 0.05
        assert response.citations[0].source_chunk_id == "georag_reports:empty"

    def test_sentinel_set_contains_all_known_placeholders(self) -> None:
        assert {
            "no-tool-call",
            "georag_reports:empty",
            "pg_public_geoscience:empty",
        } == EMPTY_SOURCE_SENTINELS

    def test_confidence_computer_uses_the_same_sentinels(self) -> None:
        """Lockstep regression — every sentinel excluded by the IND-6 guard
        must also be excluded from the independent-source count."""
        citations = [
            Citation(
                citation_id=f"[DATA-{i + 1}]",
                citation_type="DATA",
                source_chunk_id=sentinel,
                document_title="placeholder",
                section=None,
                page=None,
                relevance_score=0.0,
            )
            for i, sentinel in enumerate(sorted(EMPTY_SOURCE_SENTINELS))
        ]
        assert _count_independent_sources(citations) == 0

    def test_real_chunk_still_counts_as_independent_source(self) -> None:
        response = assemble_response(
            text="[NI43-1].",
            tool_results=[("search_documents", _make_doc_result(1))],
        )
        assert _count_independent_sources(response.citations) == 1
        assert response.confidence > 0.05
