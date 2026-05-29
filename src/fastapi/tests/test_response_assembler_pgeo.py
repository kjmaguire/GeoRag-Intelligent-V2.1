"""Unit tests for the PGEO citation path in response_assembler.py.

Covers:
  - assign_citation_ids with SpatialQueryResult, PublicGeoscienceSearchResult,
    and DocumentSearchResult (shared counter).
  - _source_chunk_id_for_pg_record canonical format.
  - _pg_record_title display title logic.
  - assemble_response end-to-end with PublicGeoscienceSearchResult.
  - Fallback path (empty tool_results) still emits [DATA-1].

Run with:
    pytest tests/test_response_assembler_pgeo.py -v
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from app.agent.public_geoscience_tool import (
    PublicGeoscienceRecord,
    PublicGeoscienceSearchResult,
)
from app.agent.response_assembler import (
    _pg_record_title,
    _source_chunk_id_for_pg_record,
    assign_citation_ids,
    assemble_response,
)
from app.agent.tools import (
    DocumentChunk,
    DocumentSearchResult,
    SpatialQueryResult,
)


# ---------------------------------------------------------------------------
# Helpers — build minimal test fixtures
# ---------------------------------------------------------------------------


def _make_spatial_result(count: int = 3) -> SpatialQueryResult:
    """Return a minimal SpatialQueryResult for testing."""
    return SpatialQueryResult(
        collars=[],
        count=count,
        data_source="PostGIS silver.collars",
    )


def _make_doc_result(document_type: str = "NI43") -> DocumentSearchResult:
    """Return a minimal DocumentSearchResult for testing."""
    chunk = DocumentChunk(
        chunk_id="chunk-uuid-001",
        report_id="rep-001",
        source_document_id="rep-001",
        document_title="NI 43-101 Technical Report",
        document_type=document_type,
        text="Indicated resources: 12.5 Mt at 0.45% Cu.",
        relevance_score=0.85,
        section_number="6.1",
        section_title="Resource Estimate",
        section="6.1 — Resource Estimate",
        page=42,
    )
    return DocumentSearchResult(
        chunks=[chunk],
        count=1,
        data_source="Qdrant georag_reports",
    )


def _make_pg_record(
    n: int = 1,
    *,
    jurisdiction_name: str | None = "Saskatchewan",
    license_summary: str | None = "Saskatchewan Open Government Licence",
    staleness_seconds: int | None = 86400,
) -> PublicGeoscienceRecord:
    return PublicGeoscienceRecord(
        pg_id=f"pg-rec-{n:03d}",
        canonical_type="mineral_occurrence",
        jurisdiction_code="CA-SK",
        jurisdiction_name=jurisdiction_name,
        source_id="sk-smdi",
        source_feature_id=f"SMDI-{1000 + n}",
        name=f"Test Occurrence {n}",
        summary_text=f"Test Occurrence {n} in Saskatchewan. Commodities: Au.",
        commodities=["Au"],
        relevance_score=0.9 - (n * 0.05),
        license_summary=license_summary,
        staleness_seconds=staleness_seconds,
    )


def _make_pg_result(n_records: int = 3) -> PublicGeoscienceSearchResult:
    return PublicGeoscienceSearchResult(
        records=[_make_pg_record(i + 1) for i in range(n_records)],
        count=n_records,
        jurisdictions_queried=["CA-SK"],
        canonical_types_queried=["mineral_occurrence"],
    )


# ---------------------------------------------------------------------------
# assign_citation_ids
# ---------------------------------------------------------------------------


class TestAssignCitationIds:
    """Tests for the citation-id pre-assignment function."""

    def test_spatial_result_yields_one_data_citation(self) -> None:
        tool_results = [("query_spatial_collars", _make_spatial_result())]
        bundles = assign_citation_ids(tool_results)
        assert bundles == [["[DATA-1]"]]

    def test_pg_result_with_3_records_yields_3_pgeo_ids(self) -> None:
        tool_results = [("search_public_geoscience", _make_pg_result(3))]
        bundles = assign_citation_ids(tool_results)
        assert len(bundles) == 1
        assert bundles[0] == ["[PGEO-1]", "[PGEO-2]", "[PGEO-3]"]
        # All IDs are distinct.
        assert len(set(bundles[0])) == 3

    def test_mixed_tool_results_share_counter(self) -> None:
        """DATA-1, then PGEO-2 + PGEO-3, then NI43-4."""
        tool_results = [
            ("query_spatial_collars", _make_spatial_result()),
            ("search_public_geoscience", _make_pg_result(2)),
            ("search_documents", _make_doc_result("NI43")),
        ]
        bundles = assign_citation_ids(tool_results)
        assert bundles[0] == ["[DATA-1]"]
        assert bundles[1] == ["[PGEO-2]", "[PGEO-3]"]
        assert bundles[2] == ["[NI43-4]"]

    def test_empty_tool_results_returns_empty_list(self) -> None:
        assert assign_citation_ids([]) == []

    def test_pg_result_with_zero_records_yields_empty_bundle(self) -> None:
        pg_result = PublicGeoscienceSearchResult(
            records=[],
            count=0,
            jurisdictions_queried=[],
            canonical_types_queried=[],
        )
        tool_results = [("search_public_geoscience", pg_result)]
        bundles = assign_citation_ids(tool_results)
        # One tool result → one bundle, but zero records → no ids.
        assert bundles == [[]]


# ---------------------------------------------------------------------------
# _source_chunk_id_for_pg_record
# ---------------------------------------------------------------------------


class TestSourceChunkIdForPgRecord:
    """Tests for the canonical source_chunk_id format."""

    def test_format_with_all_fields_populated(self) -> None:
        rec = _make_pg_record(1)
        chunk_id = _source_chunk_id_for_pg_record(rec)
        assert chunk_id.startswith("pg_mineral_occurrence:")
        assert ":feature=SMDI-1001:" in chunk_id
        assert ":pg_id=pg-rec-001" in chunk_id

    def test_unknown_feature_id_when_none(self) -> None:
        rec = _make_pg_record(1)
        rec.source_feature_id = None
        chunk_id = _source_chunk_id_for_pg_record(rec)
        assert ":feature=unknown:" in chunk_id

    def test_source_id_included_in_output(self) -> None:
        rec = _make_pg_record(1)
        chunk_id = _source_chunk_id_for_pg_record(rec)
        assert "sk-smdi" in chunk_id

    def test_canonical_type_prefix_in_output(self) -> None:
        rec = _make_pg_record(1)
        rec.canonical_type = "mine"
        chunk_id = _source_chunk_id_for_pg_record(rec)
        assert chunk_id.startswith("pg_mine:")


# ---------------------------------------------------------------------------
# _pg_record_title
# ---------------------------------------------------------------------------


class TestPgRecordTitle:
    """Tests for the jurisdiction-qualified display title helper."""

    def test_with_jurisdiction_name_uses_human_readable(self) -> None:
        rec = _make_pg_record(1, jurisdiction_name="Saskatchewan")
        title = _pg_record_title(rec)
        assert title.startswith("Saskatchewan — ")
        assert "Test Occurrence 1" in title

    def test_without_jurisdiction_name_falls_back_to_code(self) -> None:
        rec = _make_pg_record(1, jurisdiction_name=None)
        title = _pg_record_title(rec)
        assert title.startswith("CA-SK — ")

    def test_empty_name_falls_back_to_canonical_type_label(self) -> None:
        rec = _make_pg_record(1)
        rec.name = ""
        title = _pg_record_title(rec)
        # Should include something derived from canonical_type
        assert "Saskatchewan — " in title
        assert title  # non-empty

    def test_title_contains_record_name(self) -> None:
        rec = _make_pg_record(2)
        title = _pg_record_title(rec)
        assert "Test Occurrence 2" in title


# ---------------------------------------------------------------------------
# assemble_response end-to-end with PublicGeoscienceSearchResult
# ---------------------------------------------------------------------------


class TestAssembleResponseWithPgeo:
    """End-to-end tests for assemble_response when PGEO results are present."""

    def test_3_pg_records_yield_3_distinct_citations(self) -> None:
        pg_result = _make_pg_result(3)
        tool_results = [("search_public_geoscience", pg_result)]

        with patch(
            "app.agent.hallucination.qualitative_detector.detect_qualitative_claims",
            return_value=[],
        ), patch(
            "app.agent.hallucination.qualitative_detector.confidence_penalty",
            return_value=0.0,
        ):
            response = assemble_response(
                text="There are several mineral occurrences in Saskatchewan [PGEO-1] [PGEO-2] [PGEO-3].",
                tool_results=tool_results,
            )

        assert len(response.citations) == 3
        cit_ids = {c.citation_id for c in response.citations}
        assert cit_ids == {"[PGEO-1]", "[PGEO-2]", "[PGEO-3]"}

    def test_all_pg_citations_have_correct_citation_type(self) -> None:
        pg_result = _make_pg_result(2)
        tool_results = [("search_public_geoscience", pg_result)]

        with patch(
            "app.agent.hallucination.qualitative_detector.detect_qualitative_claims",
            return_value=[],
        ), patch(
            "app.agent.hallucination.qualitative_detector.confidence_penalty",
            return_value=0.0,
        ):
            response = assemble_response(
                text="[PGEO-1] [PGEO-2].",
                tool_results=tool_results,
            )

        for cit in response.citations:
            assert cit.citation_type == "PGEO"

    def test_all_pg_citations_have_unique_source_chunk_ids(self) -> None:
        pg_result = _make_pg_result(3)
        tool_results = [("search_public_geoscience", pg_result)]

        with patch(
            "app.agent.hallucination.qualitative_detector.detect_qualitative_claims",
            return_value=[],
        ), patch(
            "app.agent.hallucination.qualitative_detector.confidence_penalty",
            return_value=0.0,
        ):
            response = assemble_response(
                text="[PGEO-1] [PGEO-2] [PGEO-3].",
                tool_results=tool_results,
            )

        chunk_ids = [c.source_chunk_id for c in response.citations]
        assert len(chunk_ids) == len(set(chunk_ids)), "source_chunk_ids must be unique"

    def test_pg_citations_have_corpus_public_geoscience(self) -> None:
        pg_result = _make_pg_result(2)
        tool_results = [("search_public_geoscience", pg_result)]

        with patch(
            "app.agent.hallucination.qualitative_detector.detect_qualitative_claims",
            return_value=[],
        ), patch(
            "app.agent.hallucination.qualitative_detector.confidence_penalty",
            return_value=0.0,
        ):
            response = assemble_response(
                text="[PGEO-1] [PGEO-2].",
                tool_results=tool_results,
            )

        for cit in response.citations:
            assert cit.corpus == "public_geoscience"

    def test_pg_citations_carry_jurisdiction_and_license_fields(self) -> None:
        pg_result = _make_pg_result(1)
        tool_results = [("search_public_geoscience", pg_result)]

        with patch(
            "app.agent.hallucination.qualitative_detector.detect_qualitative_claims",
            return_value=[],
        ), patch(
            "app.agent.hallucination.qualitative_detector.confidence_penalty",
            return_value=0.0,
        ):
            response = assemble_response(
                text="[PGEO-1].",
                tool_results=tool_results,
            )

        cit = response.citations[0]
        assert cit.jurisdiction_code == "CA-SK"
        assert cit.jurisdiction_name == "Saskatchewan"
        assert cit.license_summary is not None
        assert cit.staleness_seconds is not None
        assert cit.staleness_seconds > 0

    def test_fallback_citation_when_no_tool_results(self) -> None:
        """Empty tool_results must still produce a single [DATA-1] fallback."""
        with patch(
            "app.agent.hallucination.qualitative_detector.detect_qualitative_claims",
            return_value=[],
        ), patch(
            "app.agent.hallucination.qualitative_detector.confidence_penalty",
            return_value=0.0,
        ):
            response = assemble_response(
                text="I don't have data on that in this project.",
                tool_results=[],
            )

        assert len(response.citations) == 1
        assert response.citations[0].citation_id == "[DATA-1]"
        assert response.citations[0].source_chunk_id == "no-tool-call"
