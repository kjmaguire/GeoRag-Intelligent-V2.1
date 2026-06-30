"""Tests for the lineage payload model + builder — Phase 1 / Step 1.5."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from app.agent.lineage import build_lineage_payload
from app.agent.schemas import (
    GEO_ANSWER_SCHEMA_VERSION,
    ConfidenceBlock,
    GeoAnswer,
    Interpretation,
    Observation,
    SectionEmpty,
    UncertaintyBlock,
)
from app.models.lineage import (
    FiltersApplied,
    LineagePayload,
    QaQcFiltersApplied,
    RetrievedSource,
)
from app.models.rag import Citation, GeoRAGResponse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _Payload:
    chunk_id: str | None = None
    pdf_id: str | None = None


@dataclass
class _Candidate:
    store: str
    payload: _Payload | None = None
    canonical_id: str | None = None
    score: float | None = None


@dataclass
class _ScoredCandidate:
    candidate: _Candidate
    rrf_score: float | None = None


def _cit(citation_id: str, source: str) -> Citation:
    return Citation(
        citation_id=citation_id,
        citation_type="DATA",
        source_chunk_id=source,
        document_title="t",
        relevance_score=0.9,
    )


def _response_with(
    geo_answer: GeoAnswer | None = None,
    citations: list[Citation] | None = None,
) -> GeoRAGResponse:
    return GeoRAGResponse(
        text="some text",
        citations=citations
        or [_cit("[DATA-1]", "silver.collars:row=1")],
        confidence=0.8,
        sources_used=["silver.collars:row=1"],
        geo_answer=geo_answer,
    )


def _full_oiur_answer() -> GeoAnswer:
    return GeoAnswer(
        observations=[
            Observation(
                observation_id="O1",
                text="DDH-07 [DATA-1].",
                citation_ids=["[DATA-1]"],
            )
        ],
        interpretations=[
            Interpretation(
                interpretation_id="I1",
                text="Continuity is plausible.",
                supporting_observation_ids=["O1"],
            )
        ],
        uncertainty=UncertaintyBlock(
            confidence=ConfidenceBlock(
                level="Medium",
                reason="reason",
                data_to_reduce_uncertainty="One infill hole at section 5+50N.",
            ),
        ),
        recommended_actions=SectionEmpty(reason="No decision context."),
    )


# ---------------------------------------------------------------------------
# LineagePayload — basics
# ---------------------------------------------------------------------------


def test_lineage_payload_defaults_empty() -> None:
    p = LineagePayload()
    assert p.session_id is None
    assert p.retrieved_sources == []
    assert isinstance(p.filters_applied, FiltersApplied)
    assert isinstance(p.qaqc_filters_applied, QaQcFiltersApplied)
    assert p.answer_schema_version is None


def test_lineage_to_db_columns_serialises_jsonb_values() -> None:
    p = LineagePayload(
        session_id=UUID("11111111-2222-3333-4444-555555555555"),
        retrieved_sources=[
            RetrievedSource(
                source_type="qdrant",
                chunk_id="c-1",
                pdf_id=UUID("66666666-7777-8888-9999-aaaaaaaaaaaa"),
                score=0.91,
                cited=True,
            )
        ],
        filters_applied=FiltersApplied(jurisdiction_codes=["CA-SK"]),
        qaqc_filters_applied=QaQcFiltersApplied(failed_crm_batches=["B-2024-17"]),
        answer_schema_version=GEO_ANSWER_SCHEMA_VERSION,
    )
    cols = p.to_db_columns()
    assert cols["session_id"] == "11111111-2222-3333-4444-555555555555"
    assert cols["answer_schema_version"] == GEO_ANSWER_SCHEMA_VERSION
    # JSONB-bound fields must be JSON-serialisable as-is.
    json.dumps(cols["lineage_retrieved_sources"])
    json.dumps(cols["lineage_filters_applied"])
    json.dumps(cols["lineage_qaqc_filters_applied"])
    # UUIDs in nested models are stringified.
    assert cols["lineage_retrieved_sources"][0]["pdf_id"] == "66666666-7777-8888-9999-aaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# Builder — happy path
# ---------------------------------------------------------------------------


def test_build_lineage_marks_cited_chunks() -> None:
    # Citation source_chunk_id matches the first candidate's chunk_id.
    citations = [_cit("[DATA-1]", "c-1")]
    fused = [
        _ScoredCandidate(
            candidate=_Candidate(store="qdrant", payload=_Payload(chunk_id="c-1")),
            rrf_score=0.92,
        ),
        _ScoredCandidate(
            candidate=_Candidate(store="qdrant", payload=_Payload(chunk_id="c-2")),
            rrf_score=0.71,
        ),
    ]
    response = _response_with(
        geo_answer=_full_oiur_answer(), citations=citations
    )
    payload = build_lineage_payload(response=response, fused_candidates=fused)
    assert len(payload.retrieved_sources) == 2
    by_chunk = {s.chunk_id: s for s in payload.retrieved_sources}
    assert by_chunk["c-1"].cited is True
    assert by_chunk["c-2"].cited is False
    # Scores propagated from the wrapper's rrf_score.
    assert by_chunk["c-1"].score == pytest.approx(0.92)


def test_build_lineage_includes_schema_version_only_when_geo_answer_present() -> None:
    response_with = _response_with(geo_answer=_full_oiur_answer())
    response_without = _response_with(geo_answer=None)

    p_with = build_lineage_payload(response=response_with)
    p_without = build_lineage_payload(response=response_without)

    assert p_with.answer_schema_version == GEO_ANSWER_SCHEMA_VERSION
    assert p_without.answer_schema_version is None


def test_build_lineage_normalises_store_label() -> None:
    fused = [
        _ScoredCandidate(
            candidate=_Candidate(store="PostGIS", canonical_id="silver.collars:1")
        ),
        _ScoredCandidate(
            candidate=_Candidate(store="public_geo", canonical_id="pg:1")
        ),
        _ScoredCandidate(
            candidate=_Candidate(store="something_weird", canonical_id="x")
        ),
    ]
    payload = build_lineage_payload(
        response=_response_with(), fused_candidates=fused
    )
    types = [s.source_type for s in payload.retrieved_sources]
    assert "postgis" in types
    assert "public_geoscience" in types
    assert "other" in types


def test_build_lineage_skips_malformed_candidates() -> None:
    # A candidate that won't yield any chunk_id but is otherwise present
    # should still produce a RetrievedSource — the builder is best-effort.
    fused = [
        _ScoredCandidate(
            candidate=_Candidate(store="neo4j", canonical_id="entity-42"),
            rrf_score=0.55,
        ),
        # Mostly-empty candidate — no payload, no canonical_id.
        _ScoredCandidate(candidate=_Candidate(store="qdrant"), rrf_score=None),
    ]
    payload = build_lineage_payload(
        response=_response_with(), fused_candidates=fused
    )
    # Both entries materialise; nothing raises.
    assert len(payload.retrieved_sources) == 2
    assert payload.retrieved_sources[0].chunk_id == "entity-42"


def test_build_lineage_carries_session_id_and_filters() -> None:
    sid = uuid4()
    filters = FiltersApplied(
        project_id=uuid4(),
        jurisdiction_codes=["CA-SK"],
        data_types=["drill_logs", "assays"],
    )
    qaqc = QaQcFiltersApplied(
        silver_review_excluded_batches=["B-99"],
        failed_crm_batches=["B-2024-17"],
    )
    payload = build_lineage_payload(
        response=_response_with(geo_answer=_full_oiur_answer()),
        filters=filters,
        qaqc_filters=qaqc,
        session_id=sid,
    )
    assert payload.session_id == sid
    assert payload.filters_applied.jurisdiction_codes == ["CA-SK"]
    assert payload.qaqc_filters_applied.failed_crm_batches == ["B-2024-17"]


def test_build_lineage_empty_inputs_yields_well_formed_payload() -> None:
    payload = build_lineage_payload(response=_response_with())
    assert payload.retrieved_sources == []
    assert payload.filters_applied.model_dump() == FiltersApplied().model_dump()
    assert payload.qaqc_filters_applied.model_dump() == QaQcFiltersApplied().model_dump()
    assert payload.answer_schema_version is None
    # to_db_columns must always serialise cleanly.
    cols = payload.to_db_columns()
    assert cols["session_id"] is None
    json.dumps(cols["lineage_retrieved_sources"])
    json.dumps(cols["lineage_filters_applied"])
    json.dumps(cols["lineage_qaqc_filters_applied"])


def test_retrieved_source_pdf_id_normalises_to_uuid() -> None:
    fused = [
        _ScoredCandidate(
            candidate=_Candidate(
                store="qdrant",
                payload=_Payload(
                    chunk_id="c-1",
                    pdf_id="11111111-2222-3333-4444-555555555555",
                ),
            ),
            rrf_score=0.8,
        ),
        _ScoredCandidate(
            candidate=_Candidate(
                store="qdrant",
                payload=_Payload(chunk_id="c-2", pdf_id="not-a-uuid"),
            ),
        ),
    ]
    payload = build_lineage_payload(
        response=_response_with(), fused_candidates=fused
    )
    pdf_ids = {s.chunk_id: s.pdf_id for s in payload.retrieved_sources}
    assert isinstance(pdf_ids["c-1"], UUID)
    assert pdf_ids["c-2"] is None  # invalid string → None, no exception
