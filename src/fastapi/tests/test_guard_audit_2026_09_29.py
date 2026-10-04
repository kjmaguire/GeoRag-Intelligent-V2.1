"""Regression tests for the 2026-09-29 §04i guard audit (batch A).

Every fixture here uses the identifiers production actually carries: real
UUIDs for report / chunk / collar / project ids, NI 43-101 document types,
section numbers, pages, relevance and OCR scores, comma-formatted depths,
Cameco-style numeric hole IDs and imperial units. The older guard fixtures
used short fake ids ("c1", "report-1"), which is how RAG-1 hid: the digits
inside a UUID were feeding Layer 3's grounded set, and "c1" has none.

Findings covered: RAG-1, RAG-2, RAG-3, RAG-4, RAG-7, RAG-8, RAG-13, RAG-16,
RAG-20, RAG-21, RISK-4, AGT-4, AGT-11, PG-5.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
from typing import Any

import pytest

from app.agent.hallucination.orchestrator_validators import (
    run_post_assembly_validation,
    verify_completeness,
    verify_constraints,
    verify_entities,
    verify_numbers,
)
from app.agent.tools import DocumentChunk, DocumentSearchResult
from app.models.rag import Citation, GeoRAGResponse

PROJECT_ID = "5e2b8c1d-7a4f-4e39-b6d0-91c3a8f2e7b4"
REPORT_ID = "3f1c2a9e-8b7d-4c61-9a0e-2d5b7f4e1c08"
OTHER_REPORT_ID = "9d0e7b3c-1a2f-4e58-8c6d-5b4a3f2e1d09"
CHUNK_ID = "44a67709-2f1e-4b3c-9d8a-7e6f5c4b3a21"
SUMMARY_CHUNK_ID = "6b1e0c52-4f7a-4d2b-9c3e-7a8f9d0e1b24"

CHUNK_TEXT = (
    "14.2 Mineral Resource Estimate. The Shirley Basin project hosts an "
    "Indicated Mineral Resource of 8.6 Mt at an average grade of 0.12% eU3O8, "
    "containing 22.7 Mlbs U3O8, reported at a 0.02% eU3O8 cut-off. Hole "
    "36-1085 intersected 0.12% eU3O8 from 120-126 m. The 2011 drilling "
    "programme comprised 1,340 m in 14 holes to a maximum depth of 152 m."
)


def _chunk(
    chunk_id: str = CHUNK_ID,
    *,
    report_id: str | None = REPORT_ID,
    text: str = CHUNK_TEXT,
) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        text=text,
        source_document_id="c7d8e9f0-1a2b-4c3d-8e5f-6a7b8c9d0e1f",
        document_title=(
            "NI 43-101 Technical Report on the Shirley Basin Uranium Project, "
            "Wyoming, 2012"
        ),
        section_number="14.2",
        section_title="Mineral Resource Estimate",
        section="14.2 — Mineral Resource Estimate",
        page=112,
        document_type="NI43",
        report_id=report_id,  # type: ignore[arg-type]
        relevance_score=0.82,
        ocr_confidence=0.97,
    )


def _docs(*chunks: DocumentChunk) -> tuple[str, DocumentSearchResult]:
    return (
        "search_documents",
        DocumentSearchResult(
            chunks=list(chunks) or [_chunk()],
            count=len(chunks) or 1,
            data_source="qdrant:georag_chunks (reranked)",
        ),
    )


def _doc_citation(
    cid: str = "[NI43-1]", *, report_id: str = REPORT_ID, chunk_id: str = CHUNK_ID
) -> Citation:
    return Citation(
        citation_id=cid,
        citation_type="NI43",
        source_chunk_id=f"georag_reports:{report_id}:section=14.2:chunk={chunk_id}",
        document_title="NI 43-101 Technical Report on the Shirley Basin Uranium Project",
        section="14.2 — Mineral Resource Estimate",
        page=112,
        relevance_score=0.82,
    )


def _response(text: str, citations: list[Citation] | None = None) -> GeoRAGResponse:
    cits = citations or [_doc_citation()]
    return GeoRAGResponse(
        text=text,
        citations=cits,
        confidence=0.8,
        sources_used=[c.source_chunk_id for c in cits],
    )


# ---------------------------------------------------------------------------
# RAG-1 — Layer 3 no longer grounds invented numbers on ids and scores
# ---------------------------------------------------------------------------


class TestLayer3RejectsInventedNumbersInDocumentAnswers:
    @pytest.mark.parametrize(
        ("answer", "invented"),
        [
            ("Hole 36-1085 returned 7.44 g/t Au over 12.6 m [NI43-1].", ("7.44", "12.6")),
            ("The deposit hosts 48.2 Mt at 0.64% U3O8 [NI43-1].", ("48.2", "0.64")),
            ("The best intercept graded 0.087% eU3O8 [NI43-1].", ("0.087",)),
            ("The resource totals 12,345,000 tonnes [NI43-1].", ("12345000",)),
        ],
    )
    def test_an_invented_number_is_flagged(self, answer: str, invented: tuple[str, ...]) -> None:
        warnings = verify_numbers(answer, [_docs()])
        joined = " ".join(warnings)
        assert len(warnings) == len(invented), warnings
        for value in invented:
            assert value in joined

    @pytest.mark.parametrize(
        "answer",
        [
            "Per the 2012 NI 43-101 technical report, hole 36-1085 intersected "
            "0.12% eU3O8 from 120-126 m [NI43-1].",
            "The Indicated resource is 8.6 Mt at 0.12% eU3O8 for 22.7 Mlbs U3O8 "
            "[NI43-1]. See Section 14.2 on page 112.",
            "The 2011 programme comprised 1,340 m in 14 holes [NI43-1].",
            "It totalled approximately 1,300 m of drilling [NI43-1].",
            "The maximum hole depth was 152 m (499 ft) [NI43-1].",
            "The cut-off was 200 ppm eU3O8 [NI43-1].",
        ],
    )
    def test_a_grounded_answer_is_left_alone(self, answer: str) -> None:
        assert verify_numbers(answer, [_docs()]) == []

    def test_uuid_digits_no_longer_ground_anything(self) -> None:
        """The audit repro: a chunk whose text holds only '2011'. With real
        UUIDs in chunk_id / report_id / source_document_id, the invented
        grade used to return zero warnings."""
        docs = _docs(_chunk(text="Drilling resumed in 2011."))
        warnings = verify_numbers("The composite returned 7.44 g/t Au over 12.6 m [NI43-1].", [docs])
        assert len(warnings) == 2

    def test_a_derived_statistic_over_structured_rows_still_passes(self) -> None:
        """The derivation window survives for structured rows: a mean of
        collar depths sits near a collar depth."""
        collars = ("query_spatial_collars", dict(
            count=3,
            collars=[
                dict(hole_id="36-1085", collar_id="0f7d3c2e-9b1a-4d8e-a6c5-3e2f1d0c9b8a", total_depth=152.0),
                dict(hole_id="36-1042", collar_id="1a8e4d3f-0c2b-4e9f-b7d6-4f3e2d1c0b9a", total_depth=137.5),
                dict(hole_id="36-1090", collar_id="2b9f5e40-1d3c-4f0a-88e7-5a4f3e2d1c0b", total_depth=141.0),
            ],
        ))
        assert verify_numbers("The mean total depth is 143.5 m [DATA-1].", [collars]) == []
        assert verify_numbers("Hole 36-1085 reached 152 m [DATA-1].", [collars]) == []


# ---------------------------------------------------------------------------
# RAG-2 — thousands separators
# ---------------------------------------------------------------------------


class TestThousandsSeparators:
    def test_layer_6_reads_12400_m_as_one_depth(self) -> None:
        warnings = verify_constraints(
            "Hole 36-1085 reached a total depth of 12,400 m [NI43-1]."
        )
        assert len(warnings) == 1
        assert "12400" in warnings[0]

    def test_layer_3_matches_a_comma_formatted_value(self) -> None:
        docs = _docs(_chunk(text="The programme totalled 12,387 m of core drilling."))
        assert verify_numbers("The programme totalled 12,400 m [NI43-1].", [docs]) == []
        assert verify_numbers("The programme totalled 12,387 m [NI43-1].", [docs]) == []


# ---------------------------------------------------------------------------
# RAG-3 / RAG-16 — Layer 4 hole IDs
# ---------------------------------------------------------------------------


def _canon(hole_id: str) -> str:
    return re.sub(r"[\s\-_./]+", "", hole_id).upper()


class _CollarPool:
    """silver.collars as the new Layer 4 query sees it.

    Emulates ``UPPER(hole_id) = ANY($1) OR hole_id_canonical = ANY($3) OR
    regexp_replace(UPPER(hole_id), ...) = ANY($3)`` against holes stored
    exactly as ``stored`` spells them (mixed case, any separators).
    """

    def __init__(self, stored: list[str]) -> None:
        self.stored = stored
        self.calls: list[tuple[Any, ...]] = []

    def acquire(self):  # noqa: ANN201 — asyncpg-shaped context manager
        pool = self

        class _Conn:
            async def fetch(self, sql: str, upper_ids: list[str], project_id: str, canon_ids: list[str]):
                pool.calls.append((sql, upper_ids, project_id, canon_ids))
                return [
                    dict(hole_id=h, hole_id_canonical=_canon(h))
                    for h in pool.stored
                    if h.upper() in upper_ids or _canon(h) in canon_ids
                ]

        class _Acquire:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *_exc: object) -> bool:
                return False

        return _Acquire()


AUDIT_SENTENCE = (
    "Per the 2012 NI 43-101 technical report, hole 36-1085 intersected 0.12% "
    "eU3O8 from 120-126 m [NI43-1]."
)


class TestLayer4NumericHoleIds:
    @pytest.mark.asyncio
    async def test_the_audit_sentence_raises_nothing(self) -> None:
        """RAG-3: '43-101' and '120-126' were both reported as critical
        fabricated drill holes on this fully supported sentence."""
        warnings = await verify_entities(
            AUDIT_SENTENCE, PROJECT_ID, _CollarPool(["36-1085"]), None, [_docs()],
        )
        assert warnings == []

    @pytest.mark.asyncio
    async def test_a_fabricated_numeric_hole_is_still_critical(self) -> None:
        warnings = await verify_entities(
            "Hole 36-9999 intersected 0.31% eU3O8 from 88-94 m [NI43-1].",
            PROJECT_ID, _CollarPool(["36-1085"]), None, [_docs()],
        )
        assert any(w.startswith("Layer 4: Drill-hole ID '36-9999'") for w in warnings), warnings
        assert not any("88-94" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_the_numeric_hole_check_reaches_the_database(self) -> None:
        pool = _CollarPool(["36-1085"])
        await verify_entities(AUDIT_SENTENCE, PROJECT_ID, pool, None, [_docs()])
        assert pool.calls, "the hole-ID lookup must still run"
        _sql, upper_ids, project_id, canon_ids = pool.calls[0]
        assert upper_ids == ["36-1085"]
        assert canon_ids == ["361085"]
        assert project_id == PROJECT_ID


class TestLayer4CaseAndSeparators:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stored", "answered"),
        [("BH-12", "BH12"), ("BH12", "BH-12"), ("Gh08-212", "GH08-212"), ("pls_22_08", "PLS-22-08")],
    )
    async def test_the_same_hole_spelled_differently_resolves(self, stored: str, answered: str) -> None:
        collar = ("query_collar_details", dict(hole_id=stored, total_depth=212.0))
        warnings = await verify_entities(
            f"Hole {answered} reached 212 m [DATA-1].",
            PROJECT_ID, _CollarPool([stored]), None, [collar],
        )
        assert warnings == [], warnings


class TestLayer4SeparatorPositions:
    """Audit 2026-10-04 item 24: the SQL candidate fetch merges "PLS-2-28"
    with "PLS-22-8" (separator-free canonical form); the match is confirmed on
    the position-aware key, so the fabricated one is no longer a real hole."""

    @pytest.mark.asyncio
    async def test_a_hole_that_only_exists_after_deleting_separators_is_critical(self) -> None:
        collar = ("query_collar_details", dict(hole_id="PLS-22-8", total_depth=212.0))
        warnings = await verify_entities(
            "Hole PLS-2-28 reached 212 m [DATA-1].",
            PROJECT_ID, _CollarPool(["PLS-22-8"]), None, [collar],
        )
        assert any(w.startswith("Layer 4: Drill-hole ID 'PLS-2-28'") for w in warnings), warnings

    @pytest.mark.asyncio
    async def test_the_real_hole_still_resolves(self) -> None:
        collar = ("query_collar_details", dict(hole_id="PLS-22-8", total_depth=212.0))
        warnings = await verify_entities(
            "Hole PLS-22-8 reached 212 m [DATA-1].",
            PROJECT_ID, _CollarPool(["PLS-22-8"]), None, [collar],
        )
        assert warnings == [], warnings


class TestLayer4SwappedHole:
    """RAG-16: BH-21 exists, so the old existence check passed an answer
    that moved BH-12's intercept onto it."""

    EVIDENCE = (
        "query_collar_details",
        dict(
            hole_id="BH-12",
            collar_id="7c6b5a49-3e2d-4c1b-a09f-8e7d6c5b4a39",
            total_depth=184.0,
            assays=[dict(from_depth=61.2, to_depth=64.4, value=7.4, element="Au_ppm")],
        ),
    )

    @pytest.mark.asyncio
    async def test_a_measured_value_on_a_hole_absent_from_evidence_is_critical(self) -> None:
        warnings = await verify_entities(
            "BH-21 intersected 7.4 g/t Au over 3.2 m [DATA-1].",
            PROJECT_ID, _CollarPool(["BH-12", "BH-21"]), None, [self.EVIDENCE],
        )
        assert any(w.startswith("Layer 4: Drill-hole ID 'BH-21'") for w in warnings), warnings

    @pytest.mark.asyncio
    async def test_a_passing_mention_is_advisory(self) -> None:
        warnings = await verify_entities(
            "Unlike BH-21, BH-12 intersected 7.4 g/t Au [DATA-1].",
            PROJECT_ID, _CollarPool(["BH-12", "BH-21"]), None, [self.EVIDENCE],
        )
        assert warnings, "a hole absent from the evidence is still reported"
        assert not any(w.startswith("Layer 4:") for w in warnings), warnings

    @pytest.mark.asyncio
    async def test_the_right_hole_is_clean(self) -> None:
        warnings = await verify_entities(
            "BH-12 intersected 7.4 g/t Au over 3.2 m [DATA-1].",
            PROJECT_ID, _CollarPool(["BH-12", "BH-21"]), None, [self.EVIDENCE],
        )
        assert warnings == []

    @pytest.mark.asyncio
    async def test_the_swap_escalates_should_retry(self) -> None:
        @dataclasses.dataclass
        class _Deps:
            project_id: str = PROJECT_ID
            pg_pool: Any = None
            neo4j_driver: Any = None

        response = GeoRAGResponse(
            text="BH-21 intersected 7.4 g/t Au over 3.2 m [DATA-1].",
            citations=[
                Citation(
                    citation_id="[DATA-1]",
                    citation_type="DATA",
                    source_chunk_id="silver.collars:hole=BH-12:collar=7c6b5a49-3e2d-4c1b-a09f-8e7d6c5b4a39:assays=1:litho=0",
                    document_title="Collar BH-12",
                    relevance_score=0.95,
                )
            ],
            confidence=0.9,
            sources_used=["silver.collars:hole=BH-12"],
        )
        deps = _Deps(pg_pool=_CollarPool(["BH-12", "BH-21"]))
        _resp, _warnings, should_retry = await run_post_assembly_validation(
            response, [self.EVIDENCE], deps,  # type: ignore[arg-type]
        )
        assert should_retry is True


# ---------------------------------------------------------------------------
# RAG-4 — feet and kilometres on the depth ceiling; ranges read correctly
# ---------------------------------------------------------------------------


class TestLayer6DepthUnits:
    def test_a_correct_imperial_depth_passes(self) -> None:
        """5500 ft is 1676 m. It was compared as 5500 against 5000 m."""
        assert verify_constraints(
            "The hole was drilled to a total depth of 5500 ft [NI43-1]."
        ) == []

    @pytest.mark.parametrize(
        "text",
        [
            "The hole was drilled to a total depth of 17,000 ft [NI43-1].",
            "The hole was drilled to a total depth of 5.2 km [NI43-1].",
            "The hole reached a total depth of 14000 m [NI43-1].",
        ],
    )
    def test_an_impossible_depth_in_any_unit_is_caught(self, text: str) -> None:
        warnings = verify_constraints(text)
        assert len(warnings) == 1
        assert "depth_max_m" in warnings[0]

    def test_an_interval_is_not_a_grade(self) -> None:
        """RAG-3 follow-up: once '120-126' stopped being masked as a hole ID,
        its numbers attached to the eU3O8 keyword as a 120 % grade."""
        assert verify_constraints(AUDIT_SENTENCE) == []

    def test_an_impossible_interval_depth_is_now_checked(self) -> None:
        """The bare numeric-ID pattern used to mask '6120-6126' in any answer
        that also said 'hole', so these depths were never checked."""
        warnings = verify_constraints(
            "Hole 36-1085 intersected mineralisation at a depth of 6120-6126 m [NI43-1]."
        )
        assert len(warnings) == 2
        assert all("depth_max_m" in w for w in warnings)


# ---------------------------------------------------------------------------
# RAG-7 — CLAUDE.md rule 4 is enforced
# ---------------------------------------------------------------------------


from app.agent.hallucination.layer1_retrieval import build_refusal_text  # noqa: E402
from app.agent.hallucination.layer2_typed_output import (  # noqa: E402
    CITATION_REFUSAL_TEXT,
    enforce_claim_citations,
    validate_and_repair_with_findings,
)
from app.agent.hallucination.refusals import PROVENANCE_REFUSAL_TEXT  # noqa: E402


class TestRule4Enforcement:
    def test_a_fully_uncited_answer_is_withheld(self) -> None:
        """The audit repro: this validated 'clean'."""
        response = _response(
            "The deposit is unconformity-related and hosted in graphitic "
            "pelite. The alteration halo is chlorite-dominant."
        )
        out, findings = enforce_claim_citations(response)
        assert out.text == CITATION_REFUSAL_TEXT
        assert findings and all(f.startswith("Layer 2:") for f in findings)
        assert out.citations[0].source_chunk_id == "citation-rejected"

    def test_only_the_uncited_claim_is_removed(self) -> None:
        response = _response(
            "Hole 36-1085 intersected 0.12% eU3O8 from 120-126 m [NI43-1]. "
            "The zone is approx. 6 m thick and open along strike. "
            "See Table 14-3 for the full resource breakdown."
        )
        out, findings = enforce_claim_citations(response)
        assert "0.12% eU3O8" in out.text
        assert "open along strike" not in out.text
        assert "See Table 14-3" in out.text
        assert len(findings) == 1

    def test_a_fully_cited_answer_is_the_same_object(self) -> None:
        response = _response(
            "## Resource\n\n"
            "- The Indicated resource is 8.6 Mt at 0.12% eU3O8 [NI43-1].\n"
            "- It contains 22.7 Mlbs U3O8. [NI43-1]\n\n"
            "Would you like the Inferred figures as well?"
        )
        out, findings = enforce_claim_citations(response)
        assert findings == []
        assert out is response

    def test_hedges_and_search_statements_are_not_claims(self) -> None:
        response = _response(
            "Hole 36-1085 intersected 0.12% eU3O8 [NI43-1]. I found passages "
            "about QA/QC and drilling, but nothing specifically about "
            "metallurgical recovery. The provided evidence does not support "
            "answering the recovery question."
        )
        out, findings = enforce_claim_citations(response)
        assert findings == []
        assert out is response

    def test_markdown_survives_a_removal(self) -> None:
        response = _response(
            "## Resource\n\n"
            "- The Indicated resource is 8.6 Mt at 0.12% eU3O8 [NI43-1].\n"
            "- Grade continuity is excellent.\n"
            "- It contains 22.7 Mlbs U3O8 [NI43-1].\n\n"
            "### Conflicting evidence\n\n"
            "_No disagreement found among the passages provided._"
        )
        out, _findings = enforce_claim_citations(response)
        assert out.text == (
            "## Resource\n\n"
            "- The Indicated resource is 8.6 Mt at 0.12% eU3O8 [NI43-1].\n"
            "- It contains 22.7 Mlbs U3O8 [NI43-1].\n\n"
            "### Conflicting evidence\n\n"
            "_No disagreement found among the passages provided._"
        )

    def test_the_proactive_insights_block_is_untouched_and_its_offset_moves(self) -> None:
        head = "Hole 36-1085 reached 152 m [DATA-1]. The collar is well surveyed.\n\n"
        insights = "**Proactive insights**\n- 36-1085 is 1.8 sigma deeper than the project mean."
        response = GeoRAGResponse(
            text=head + insights,
            citations=[Citation(
                citation_id="[DATA-1]", citation_type="DATA",
                source_chunk_id="silver.collars:count=14:first=0f7d3c2e-9b1a-4d8e-a6c5-3e2f1d0c9b8a",
                document_title="Collars", relevance_score=0.95,
            )],
            confidence=0.9,
            sources_used=["silver.collars:count=14:first=0f7d3c2e-9b1a-4d8e-a6c5-3e2f1d0c9b8a"],
            proactive_insights_offset=len(head),
        )
        out, findings = enforce_claim_citations(response)
        assert len(findings) == 1
        assert out.text.endswith(insights)
        assert out.text[out.proactive_insights_offset:] == insights
        assert "well surveyed" not in out.text

    def test_a_system_refusal_is_never_touched(self) -> None:
        response = _response(build_refusal_text())
        out, findings = enforce_claim_citations(response)
        assert findings == [] and out is response

    def test_a_marker_naming_a_placeholder_citation_does_not_count(self) -> None:
        response = GeoRAGResponse(
            text="The deposit averages 0.62% U3O8 [DATA-1].",
            citations=[Citation(
                citation_id="[DATA-1]", citation_type="DATA",
                source_chunk_id="no-tool-call", document_title="No source retrieved",
                relevance_score=0.0,
            )],
            confidence=0.1,
            sources_used=["no-tool-call"],
        )
        out, findings = enforce_claim_citations(response)
        assert out.text == CITATION_REFUSAL_TEXT
        assert findings

    def test_an_invented_marker_takes_its_claim_with_it(self) -> None:
        response = _response(
            "Grades average 0.62% U3O8 [NI43-9]. Hole 36-1085 intersected "
            "0.12% eU3O8 [NI43-1]."
        )
        out, findings = validate_and_repair_with_findings(response)
        assert "0.62%" not in out.text
        assert "36-1085" in out.text
        assert findings and "[NI43-9]" in findings[0]


# ---------------------------------------------------------------------------
# validate_node wiring — RAG-7 forces should_retry; AGT-11 fails closed
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _NodeDeps:
    openai_http_client: Any = None
    anthropic_client: Any = None
    pg_pool: Any = None
    neo4j_driver: Any = None
    redis_client: Any = None
    project_id: str = PROJECT_ID


async def _validate(monkeypatch, response: GeoRAGResponse, tool_results: list[Any]):
    import app.agent.hallucination.orchestrator_validators as validators
    from app.agent.agentic_retrieval.nodes import validate_node
    from app.agent.agentic_retrieval.state import AgenticRetrievalState

    async def _no_findings(resp, _tool_results, _deps):
        return resp, [], False

    monkeypatch.setattr(validators, "run_post_assembly_validation", _no_findings)
    state = AgenticRetrievalState(query="What grade did hole 36-1085 return?", deps=_NodeDeps())
    state = state.model_copy(update=dict(response=response, tool_results=tool_results))
    return await validate_node(state)


class TestValidateNodeWiring:
    @pytest.mark.asyncio
    async def test_an_uncited_claim_is_removed_and_flagged(self, monkeypatch) -> None:
        out = await _validate(monkeypatch, _response(
            "Hole 36-1085 intersected 0.12% eU3O8 from 120-126 m [NI43-1]. "
            "Mineralisation is open to the north."
        ), [_docs()])
        response = out["response"]
        assert "open to the north" not in response.text
        assert response.validation_state == "flagged"
        assert response.confidence <= 0.2
        assert any(w.startswith("Layer 2:") for w in out["validation_warnings"])

    @pytest.mark.asyncio
    async def test_a_cited_answer_stays_clean(self, monkeypatch) -> None:
        response = _response("Hole 36-1085 intersected 0.12% eU3O8 from 120-126 m [NI43-1].")
        out = await _validate(monkeypatch, response, [_docs()])
        assert out["response"].validation_state == "clean"
        assert out["response"].text == response.text

    @pytest.mark.asyncio
    async def test_an_orphan_summary_citation_survives_validate(self, monkeypatch) -> None:
        """RAG-8 end to end: the ADR-0012 summary passage is cited, not refused."""
        summary = _chunk(
            SUMMARY_CHUNK_ID, report_id=None,
            text="Hole 36-1085: 0.12% eU3O8 over 120-126 m; QA/QC flags: none.",
        )
        citation = Citation(
            citation_id="[NI43-1]", citation_type="NI43",
            source_chunk_id=f"georag_reports:None:section=unknown:chunk={SUMMARY_CHUNK_ID}",
            document_title="Assay summary", relevance_score=0.77,
        )
        out = await _validate(monkeypatch, _response(
            "Hole 36-1085 returned 0.12% eU3O8 over 120-126 m with no QA/QC flags [NI43-1].",
            [citation],
        ), [_docs(summary)])
        assert out["response"].validation_state == "clean"
        assert "36-1085" in out["response"].text

    @pytest.mark.asyncio
    async def test_a_layer5_gate_exception_fails_closed(self, monkeypatch) -> None:
        """AGT-11: the gate raising used to leave the answer 'clean'."""
        import app.agent.hallucination.layer5_provenance as layer5

        def _boom(*_a, **_k):
            raise RuntimeError("malformed citation payload")

        monkeypatch.setattr(layer5, "gate_citation_provenance", _boom)
        out = await _validate(monkeypatch, _response(
            "Hole 36-1085 intersected 0.12% eU3O8 from 120-126 m [NI43-1]."
        ), [_docs()])
        assert out["response"].validation_state == "flagged"
        assert out["response"].confidence <= 0.2
        assert any("UNVERIFIED" in w for w in out["validation_warnings"])


# ---------------------------------------------------------------------------
# RAG-13 / AGT-4 — what satisfies the Layer 1 zero-evidence gate
# ---------------------------------------------------------------------------


from app.agent.hallucination.layer1_retrieval import assess_retrieval_quality  # noqa: E402

_EMPTY_DOCS = ("search_documents", DocumentSearchResult(chunks=[], count=0, data_source="qdrant (reranked)"))
_COLLARS = ("query_spatial_collars", dict(count=14, collars=[dict(hole_id="36-1085", total_depth=152.0)]))
_ASSAYS = ("query_assay_data", dict(count=40, element="U3O8_ppm", mean_value=1200.0))


class TestLayer1Evidence:
    def test_project_wide_rows_do_not_answer_a_document_question(self) -> None:
        verdict = assess_retrieval_quality(
            [_EMPTY_DOCS, _COLLARS, _ASSAYS],
            intent="synthesis",
            query="What metallurgical recovery did the PEA assume?",
        )
        assert verdict.refuse is True

    @pytest.mark.parametrize(
        "query",
        [
            "Summarise the 2011 drilling at Shirley Basin",
            "Which holes returned more than 0.1% eU3O8?",
            "What did hole 36-1085 intersect?",
        ],
    )
    def test_project_wide_rows_do_answer_a_drill_data_question(self, query: str) -> None:
        verdict = assess_retrieval_quality(
            [_EMPTY_DOCS, _COLLARS, _ASSAYS], intent="synthesis", query=query,
        )
        assert verdict.refuse is False

    def test_structured_intents_are_unchanged(self) -> None:
        verdict = assess_retrieval_quality(
            [_EMPTY_DOCS, _ASSAYS],
            intent="anomaly_detection",
            query="What metallurgical recovery did the PEA assume?",
        )
        assert verdict.refuse is False

    def test_a_hole_specific_lookup_always_counts(self) -> None:
        collar = ("query_collar_details", dict(hole_id="36-1085", total_depth=152.0))
        verdict = assess_retrieval_quality(
            [_EMPTY_DOCS, collar], intent="factual_lookup",
            query="What metallurgical recovery did the PEA assume?",
        )
        assert verdict.refuse is False

    @pytest.mark.parametrize("card", ["query_stereonet", "query_drill_traces_3d"])
    def test_a_visualization_card_is_not_evidence(self, card: str) -> None:
        """AGT-4: keyword-triggered cards arrive even when empty."""
        verdict = assess_retrieval_quality([_EMPTY_DOCS, (card, dict(count=0))])
        assert verdict.refuse is True


# ---------------------------------------------------------------------------
# RAG-20 (hook), RAG-21, RISK-4
# ---------------------------------------------------------------------------


from app.agent.hallucination.claim_sentences import join_units, split_units  # noqa: E402
from app.agent.hallucination.layer5_provenance import (  # noqa: E402
    enrich_provenance,
    gate_citation_provenance,
)


class TestRenderedCitationHook:
    def test_a_retrieved_but_unrendered_chunk_is_rejected_when_known(self) -> None:
        """RAG-20 gate side. NOT wired on the live path (validate_node does
        not pass rendered ids yet) — this pins the gate's behaviour for when
        the context renderer records what it rendered."""
        response = _response("Hole 36-1085 intersected 0.12% eU3O8 [NI43-1].")
        gated, warnings = gate_citation_provenance(
            response, [_docs()], rendered_citation_ids=frozenset(("[NI43-2]",)),
        )
        assert len(warnings) == 1
        assert "never rendered" in warnings[0]
        assert gated.text == PROVENANCE_REFUSAL_TEXT

    def test_without_the_hook_the_citation_passes(self) -> None:
        response = _response("Hole 36-1085 intersected 0.12% eU3O8 [NI43-1].")
        gated, warnings = gate_citation_provenance(response, [_docs()])
        assert warnings == [] and gated is response


class TestRefusalsAreNotIncomplete:
    def test_the_layer1_refusal_raises_no_completeness_findings(self) -> None:
        """RAG-21: three findings on every refusal, rendered as 'flagged'."""
        assert verify_completeness(build_refusal_text()) == []

    def test_the_citation_refusal_raises_none_either(self) -> None:
        assert verify_completeness(CITATION_REFUSAL_TEXT) == []


class TestSentenceSplitting:
    @pytest.mark.parametrize(
        "text",
        [
            "The zone is approx. 12 m wide [NI43-1].",
            "See Fig. 3 for the long section [NI43-1].",
            "Several holes, e.g. 36-1085 and 36-1042, cut the zone [NI43-1].",
        ],
    )
    def test_an_abbreviation_does_not_end_a_sentence(self, text: str) -> None:
        """RISK-4: each of these was two 'sentences'."""
        assert len(split_units(text)) == 1

    def test_a_decimal_followed_by_a_sentence_still_splits(self) -> None:
        units = split_units("The interval ended at 126.5 m. The next sample is barren.")
        assert [u.text for u in units] == [
            "The interval ended at 126.5 m.", "The next sample is barren.",
        ]

    def test_splitting_is_lossless(self) -> None:
        text = "## Resource\n\n- 8.6 Mt at 0.12% eU3O8. [NI43-1]\n- 22.7 Mlbs.\n\nDone?"
        assert join_units(split_units(text)) == text

    def test_a_trailing_marker_folds_onto_its_sentence(self) -> None:
        units = split_units("The grade is 0.12% eU3O8. [NI43-1]\nHole 36-1085 is next.")
        assert units[0].text == "The grade is 0.12% eU3O8. [NI43-1]"
        assert units[1].text == "Hole 36-1085 is next."


# ---------------------------------------------------------------------------
# PG-5 — provenance enrichment reads silver.reports
# ---------------------------------------------------------------------------


class _ReportsPool:
    """Stands in for the pool; ``row`` is returned for REPORT_ID (the one
    query is a batched ``report_id = ANY($1::uuid[])``, so it is ``fetch``)."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def acquire(self):  # noqa: ANN201
        pool = self

        class _Conn:
            async def fetch(self, sql: str, *args: Any):
                pool.calls.append((sql, args))
                if pool.row is None:
                    return []
                return [dict(report_id=REPORT_ID, **pool.row)]

        class _Acquire:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *_exc: object) -> bool:
                return False

        return _Acquire()


_SHA = "9f2c4e6a8b0d1f3e5a7c9b1d3f5e7a9c0b2d4f6e8a1c3e5b7d9f0a2c4e6b8d0f"
_OBJECT_KEY = f"workspaces/{PROJECT_ID}/raw/shirley-basin-ni43-101-2012.pdf"


class TestProvenanceEnrichment:
    @pytest.mark.asyncio
    async def test_a_report_citation_is_enriched_from_silver_reports(self) -> None:
        pool = _ReportsPool(dict(source_object_key=_OBJECT_KEY, source_file_sha256=_SHA))
        response = _response("Hole 36-1085 intersected 0.12% eU3O8 [NI43-1].")
        out = await enrich_provenance(response, pool)
        provenance = out.citations[0].provenance or ""
        assert _OBJECT_KEY in provenance
        assert "sha256:9f2c4e6a8b0d" in provenance
        # Not in the rendered section (audit 2026-10-04, item 22).
        assert _OBJECT_KEY not in (out.citations[0].section or "")
        sql, args = pool.calls[0]
        assert "silver.reports" in sql and "bronze" not in sql
        assert args == ([REPORT_ID],)
        assert "ANY($1::uuid[])" in sql

    @pytest.mark.asyncio
    async def test_many_chunks_of_one_report_cost_one_query(self) -> None:
        """Audit item 16: one SELECT per citation became one per distinct report."""
        pool = _ReportsPool(dict(source_object_key=_OBJECT_KEY, source_file_sha256=_SHA))
        citations = [
            Citation(
                citation_id=f"[NI43-{n}]", citation_type="NI43",
                source_chunk_id=f"georag_reports:{REPORT_ID}:section=14.2:chunk=chunk-{n}",
                document_title="Report", relevance_score=0.8,
            )
            for n in range(1, 13)
        ]
        out = await enrich_provenance(_response("x [NI43-1].", citations), pool)
        assert len(pool.calls) == 1
        assert pool.calls[0][1] == ([REPORT_ID],)
        assert all(_OBJECT_KEY in (c.provenance or "") for c in out.citations)

    @pytest.mark.asyncio
    async def test_distinct_reports_share_the_one_query(self) -> None:
        pool = _ReportsPool(dict(source_object_key=_OBJECT_KEY, source_file_sha256=_SHA))
        other = "11111111-2222-3333-4444-555555555555"
        citations = [
            Citation(
                citation_id="[NI43-1]", citation_type="NI43",
                source_chunk_id=f"georag_reports:{REPORT_ID}:section=1:chunk=a",
                document_title="Report", relevance_score=0.8,
            ),
            Citation(
                citation_id="[NI43-2]", citation_type="NI43",
                source_chunk_id=f"georag_reports:{other}:section=1:chunk=b",
                document_title="Other", relevance_score=0.8,
            ),
        ]
        out = await enrich_provenance(_response("x [NI43-1].", citations), pool)
        assert len(pool.calls) == 1
        assert pool.calls[0][1] == ([REPORT_ID, other],)
        # Only the report the pool knew about is enriched.
        assert _OBJECT_KEY in (out.citations[0].provenance or "")
        assert _OBJECT_KEY not in (out.citations[1].provenance or "")

    @pytest.mark.asyncio
    async def test_an_orphan_summary_is_not_looked_up(self) -> None:
        pool = _ReportsPool(None)
        citation = Citation(
            citation_id="[NI43-1]", citation_type="NI43",
            source_chunk_id=f"georag_reports:None:section=unknown:chunk={SUMMARY_CHUNK_ID}",
            document_title="Assay summary", relevance_score=0.77,
        )
        await enrich_provenance(_response("x [NI43-1].", [citation]), pool)
        assert pool.calls == []

    def test_the_columns_the_query_reads_exist_on_silver_reports(self) -> None:
        """The old query named columns that do not exist (bf.file_path,
        bf.sha256, bf.file_size) and failed on every call, silently."""
        from app.agent.hallucination.layer5_provenance import _REPORT_SOURCE_SQL

        migrations = pathlib.Path(__file__).resolve().parents[3] / "database" / "migrations"
        if not migrations.is_dir():
            pytest.skip("migrations directory not available in this checkout")
        corpus = "\n".join(
            p.read_text(encoding="utf-8", errors="replace")
            for p in migrations.glob("*.php")
            if "report" in p.name
        )
        for column in ("source_object_key", "source_file_sha256"):
            assert column in _REPORT_SOURCE_SQL
            assert column in corpus, f"no migration creates silver.reports.{column}"


# ---------------------------------------------------------------------------
# RAG-7 false-positive guard — good answers pass through untouched
# ---------------------------------------------------------------------------

_ALL_CITATIONS = [
    Citation(
        citation_id=cid,
        citation_type=cid[1:].split("-")[0],  # type: ignore[arg-type]
        source_chunk_id=f"georag_reports:{REPORT_ID}:section=14.2:chunk=6b1e0c52-4f7a-4d2b-9c3e-7a8f9d0e1b2{n}",
        document_title="NI 43-101 Technical Report on the Shirley Basin Uranium Project",
        relevance_score=0.8,
    )
    for n, cid in enumerate(["[NI43-1]", "[NI43-2]", "[PUB-1]", "[DATA-1]", "[PGEO-1]", "[PGEO-2]"])
]

#: Shapes taken from the system prompts' own examples and ordinary model
#: output. Removing anything here would be the refusal / banner spike the
#: enforcement must not cause.
GOOD_ANSWERS = [
    "The project hosts the Triple R deposit, a classic unconformity-related uranium "
    "deposit [NI43-1]. Mineralisation sits at the contact between Athabasca Group "
    "sandstones and the underlying basement pelitic gneisses [NI43-1], with grade "
    "control exerted by post-Athabasca reactivated faults [PUB-1].",
    "Saskatchewan Athabasca unconformity deposits typically range from 0.5 to over "
    "18 percent U3O8 [PGEO-1], with the highest grades concentrated at the "
    "sandstone-basement unconformity [PGEO-2].",
    "No drill holes go that deep — 50,000 m is well beyond physical drilling limits "
    "and the deepest hole in this project is 510 m [DATA-1].",
    "I can only answer geological questions about this project's exploration data.",
    "I don't have report sections discussing resource-potential conclusions for this project.",
    "Here's what the technical report says about the resource.\n\n"
    "- The Indicated resource is 8.6 Mt at 0.12% eU3O8 [NI43-1].\n"
    "- It contains 22.7 Mlbs U3O8 [NI43-1].\n\n"
    "I hope this helps. Let me know if you need the Inferred figures.",
    "I found passages about Rowan QA/QC, Madsen PFS resources, and Dixie historic "
    "drilling, but nothing specifically about metallurgical recovery. Could you "
    "clarify which study you mean?",
    "**Summary**\n\nThe deposit is a roll-front system hosted in the Wind River "
    "Formation [NI43-1]. Grades average 0.12% eU3O8 [NI43-2].\n\n"
    "### Conflicting evidence\n\n_No disagreement found among the passages provided._",
    "| Hole | From (m) | To (m) | eU3O8 (%) | Citation |\n|---|---|---|---|---|\n"
    "| 36-1085 | 120 | 126 | 0.12 | [NI43-1] |\n| 36-1042 | 98 | 101 | 0.08 | [NI43-2] |",
    "The deposit is hosted in the Wind River Formation. [NI43-1] It is a roll-front "
    "system. [NI43-2]",
]


@pytest.mark.parametrize("answer", GOOD_ANSWERS)
def test_rule4_leaves_a_good_answer_alone(answer: str) -> None:
    response = GeoRAGResponse(
        text=answer, citations=_ALL_CITATIONS, confidence=0.8, sources_used=["x"],
    )
    out, findings = enforce_claim_citations(response)
    assert findings == []
    assert out is response
