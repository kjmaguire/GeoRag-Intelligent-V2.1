"""Integration tests for the assembler ↔ OIUR wiring — Phase 1 / Step 1.2b.

Confirms:
  - flag OFF: behaviour is byte-identical to legacy (geo_answer is None)
  - flag ON + valid OIUR markdown: GeoRAGResponse.geo_answer is populated
  - flag ON + refusal text: geo_answer stays None (refusal path is not OIUR)
  - flag ON + malformed OIUR: geo_answer falls back to None, no exception
"""

from __future__ import annotations

import textwrap

import pytest

from app.agent.response_assembler import assemble_response
from app.agent.schemas import GeoAnswer
from app.agent.tools import DocumentChunk, DocumentSearchResult
from app.config import settings


def _doc_chunk() -> DocumentSearchResult:
    """Minimal document tool result so assemble_response emits a Citation."""
    chunk = DocumentChunk(
        chunk_id="chunk-001",
        text="DDH-07 intersected 12.4 m at 2.1 g/t Au.",
        source_document_id="doc-001",
        document_title="Triple R Technical Report 2023",
        section_number="3.2",
        section_title="Mineralisation",
        section="3.2 — Mineralisation",
        page=42,
        document_type="NI43",
        report_id="report-001",
        relevance_score=0.91,
    )
    return DocumentSearchResult(chunks=[chunk], count=1, data_source="Qdrant georag_reports")


FULL_OIUR = textwrap.dedent(
    """
    ## Observations
    (O1) DDH-07 intersected 12.4 m at 2.1 g/t Au [NI43-1].

    ## Interpretations
    (I1) supports: O1. The intersect supports continuity at the eastern contact [NI43-1].

    ## Uncertainty
    **Confidence: Medium**
    Reason: One hole constrains the eastern contact [NI43-1].
    Drivers:
    - Single-source constraint
    Data to reduce uncertainty: One infill hole 100 m east of DDH-07.

    ## Recommended actions
    1. Drill infill east of DDH-07. Rationale: confirms eastern continuity [NI43-1]. Expected gain: confirms eastward extension. Risk: cost ~$120k.
    """
).strip()


# ---------------------------------------------------------------------------
# Flag-off path — legacy behaviour
# ---------------------------------------------------------------------------


def test_flag_off_geo_answer_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GEO_ANSWER_OIUR_ENABLED", False)
    response = assemble_response(FULL_OIUR, [("search_documents", _doc_chunk())])
    assert response.geo_answer is None
    # The flat text path is unchanged.
    assert "## Observations" in response.text


def test_flag_off_with_legacy_text_no_change(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GEO_ANSWER_OIUR_ENABLED", False)
    legacy = "PLS-22-08 reached 510 m total depth [NI43-1]."
    response = assemble_response(legacy, [("search_documents", _doc_chunk())])
    assert response.geo_answer is None
    assert "PLS-22-08" in response.text


# ---------------------------------------------------------------------------
# Flag-on path
# ---------------------------------------------------------------------------


def test_flag_on_valid_oiur_populates_geo_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GEO_ANSWER_OIUR_ENABLED", True)
    response = assemble_response(FULL_OIUR, [("search_documents", _doc_chunk())])
    assert isinstance(response.geo_answer, GeoAnswer)
    # The flat text field is preserved as the rendered markdown answer.
    assert "## Observations" in response.text


def test_flag_on_rule_based_level_overrides_llm_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 1.3 — the LLM emits 'Medium' in FULL_OIUR but with a single
    citation source, Stage-1 computes 'Medium' too — no change.
    With two citations the same OIUR text should yield 'High' even
    though the LLM-emitted block says Medium.
    """
    from app.agent.schemas import UncertaintyBlock
    from app.agent.tools import DocumentChunk, DocumentSearchResult

    chunk1 = DocumentChunk(
        chunk_id="c-1",
        text="t1",
        source_document_id="d-1",
        document_title="Report A",
        section_number="3.2",
        section_title="X",
        section="3.2 — X",
        page=42,
        document_type="NI43",
        report_id="r-1",
        relevance_score=0.9,
    )
    chunk2 = DocumentChunk(
        chunk_id="c-2",
        text="t2",
        source_document_id="d-2",
        document_title="Report B",
        section_number="4.1",
        section_title="Y",
        section="4.1 — Y",
        page=10,
        document_type="NI43",
        report_id="r-2",
        relevance_score=0.85,
    )
    tool_results = [
        ("search_documents", DocumentSearchResult(chunks=[chunk1], count=1, data_source="Q")),
        ("search_documents", DocumentSearchResult(chunks=[chunk2], count=1, data_source="Q")),
    ]
    monkeypatch.setattr(settings, "GEO_ANSWER_OIUR_ENABLED", True)
    response = assemble_response(FULL_OIUR, tool_results)
    assert isinstance(response.geo_answer, GeoAnswer)
    assert isinstance(response.geo_answer.uncertainty, UncertaintyBlock)
    # Two distinct source_chunk_ids → Stage 1 computes High, overrides
    # the LLM-emitted Medium in FULL_OIUR.
    assert response.geo_answer.uncertainty.confidence.level == "High"
    # Prose is preserved verbatim from the LLM output.
    assert (
        "One hole constrains" in response.geo_answer.uncertainty.confidence.reason
    )


def test_flag_on_refusal_text_stays_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GEO_ANSWER_OIUR_ENABLED", True)
    refusal = "I don't have data on that in this project."
    response = assemble_response(refusal, [("search_documents", _doc_chunk())])
    assert response.geo_answer is None
    # Confidence is forced low by the existing _is_refusal path.
    assert response.confidence <= 0.2


def test_flag_on_malformed_oiur_falls_back_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GEO_ANSWER_OIUR_ENABLED", True)
    # Missing Recommended actions section — parser returns (None, [warnings]).
    malformed = textwrap.dedent(
        """
        ## Observations
        (O1) DDH-07 hit 12 m at 2 g/t [NI43-1].

        ## Interpretations
        (I1) supports: O1. Continuity inferred [NI43-1].

        ## Uncertainty
        **Confidence: Medium**
        Reason: One hole [NI43-1].
        Drivers:
        - sparse
        Data to reduce uncertainty: One infill hole east of DDH-07.
        """
    ).strip()
    response = assemble_response(malformed, [("search_documents", _doc_chunk())])
    assert response.geo_answer is None
    # Flat text and citations remain intact — no exception, no data loss.
    assert "## Observations" in response.text
    assert len(response.citations) == 1
