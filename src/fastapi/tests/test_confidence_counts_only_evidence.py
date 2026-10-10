"""Answer confidence is averaged over the results that are evidence.

`_extract_relevance` scores a structured result 1.0 whenever it has rows, and
the synthesis / decision / uncertainty profiles always run
query_spatial_collars and query_assay_data. So a document answer built on
marginal chunks (0.21-0.22) was shown at ~0.74 (audit 2026-10-10 RAG-6).
Layer 1 already decides which of those project-wide dumps count as evidence
for a document-centric question; confidence now follows the same rule.
"""

from __future__ import annotations

import pytest

from app.agent.response_assembler import _compute_confidence
from app.agent.tools import (
    AssayDataResult,
    CollarRecord,
    DocumentChunk,
    DocumentSearchResult,
    SpatialQueryResult,
)

ANSWER = "The report describes the deposit as unconformity-hosted [NI43-1]."


def _chunk(chunk_id: str, score: float) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        text="The deposit is unconformity-hosted.",
        source_document_id="report-1",
        document_title="Technical Report",
        section_number="7.1",
        section_title="Geology",
        section="7.1 — Geology",
        page=40,
        document_type="NI43",
        report_id="report-1",
        relevance_score=score,
    )


def _marginal_docs() -> DocumentSearchResult:
    return DocumentSearchResult(
        chunks=[_chunk("c1", 0.21), _chunk("c2", 0.22)],
        count=2,
        data_source="qdrant:georag_chunks (reranked)",
    )


def _collar_dump() -> SpatialQueryResult:
    collar = CollarRecord(
        collar_id="00000000-0000-0000-0000-0000000000c1",
        hole_id="PLS-22-08",
        easting=600000.0,
        northing=6400000.0,
        elevation=500.0,
        total_depth=320.0,
        azimuth=90.0,
        dip=-60.0,
        hole_type="DDH",
        status="completed",
        drill_date=None,
    )
    return SpatialQueryResult(collars=[collar], count=1, data_source="PostGIS silver.collars")


def _assay_dump() -> AssayDataResult:
    return AssayDataResult(
        samples=[],
        count=12,
        element="U3O8",
        available_elements=["U3O8"],
        min_value=0.01,
        max_value=3.2,
        mean_value=0.4,
        median_value=0.2,
        data_source="PostGIS silver.samples",
    )


def _all_results() -> list[tuple[str, object]]:
    return [
        ("search_documents", _marginal_docs()),
        ("query_spatial_collars", _collar_dump()),
        ("query_assay_data", _assay_dump()),
    ]


def test_project_wide_dumps_do_not_lift_a_document_answer() -> None:
    confidence = _compute_confidence(
        _all_results(),
        text=ANSWER,
        intent="synthesis",
        query="How is the deposit described in the technical report?",
    )

    assert confidence == pytest.approx(0.215)


def test_the_dumps_count_when_the_question_is_about_the_drill_data() -> None:
    confidence = _compute_confidence(
        _all_results(),
        text=ANSWER,
        intent="synthesis",
        query="Summarise the U3O8 assays from hole PLS-22-08.",
    )

    assert confidence > 0.5


def test_without_an_intent_every_result_counts_as_before() -> None:
    confidence = _compute_confidence(_all_results(), text=ANSWER)

    assert confidence == pytest.approx((0.215 + 1.0 + 1.0) / 3)
