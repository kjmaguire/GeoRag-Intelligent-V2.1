"""Grouped citation markers and evidence-id markers (2026-10-10 audit, 5 and 6).

5. **Grouped markers withheld fully cited answers.** The marker grammar knows
   one marker per bracket. Models write "[NI43-1, NI43-2]", "[NI43-1; NI43-2]"
   and "[NI43-1, 2]" regardless, none of which matched, so every sentence cited
   that way read as uncited to CLAUDE.md rule 4 and was dropped -- and an
   answer cited that way throughout became CITATION_REFUSAL_TEXT. They are now
   rewritten as adjacent single markers before Layers 2 and 5 look, so each id
   is checked on its own: an invented one still costs its citation.
6. **`[ev:...]` counted as a citation without being checked.** `_cites()`
   accepted any evidence-id marker, the orphan check excluded them, and the
   span resolver that would resolve them is off (CITATION_SPAN_RESOLVER_ENABLED
   is False), so "The resource is 48.2 Mt [ev:abc123]." shipped unchanged.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from app.agent.hallucination.citation_markers import (
    normalize_grouped_markers,
    ungroup_response_markers,
)
from app.agent.hallucination.layer2_typed_output import (
    CITATION_REFUSAL_TEXT,
    enforce_claim_citations,
    validate_and_repair_with_findings,
)
from app.agent.hallucination.layer5_provenance import gate_citation_provenance
from app.agent.response_assembler import assemble_response
from app.agent.tools import DocumentChunk, DocumentSearchResult
from app.config import settings
from app.models.rag import Citation, GeoRAGResponse

REPORT_ID = "3f1c2a9e-8b7d-4c61-9a0e-2d5b7f4e1c08"
CHUNKS = {
    1: "44a67709-2f1e-4b3c-9d8a-7e6f5c4b3a21",
    2: "6b1e0c52-4f7a-4d2b-9c3e-7a8f9d0e1b24",
    3: "9d0e7b3c-1a2f-4e58-8c6d-5b4a3f2e1d09",
}


def _citation(n: int) -> Citation:
    return Citation(
        citation_id=f"[NI43-{n}]",
        citation_type="NI43",
        source_chunk_id=f"georag_reports:{REPORT_ID}:section=14.2:chunk={CHUNKS[n]}",
        document_title="NI 43-101 Technical Report",
        section="14.2",
        page=100 + n,
        relevance_score=0.8,
    )


def _response(text: str, ids: tuple[int, ...] = (1, 2), **extra: Any) -> GeoRAGResponse:
    cits = [_citation(n) for n in ids]
    return GeoRAGResponse(
        text=text, citations=cits, confidence=0.8,
        sources_used=[c.source_chunk_id for c in cits], **extra,
    )


def _chunk(n: int) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=CHUNKS[n], text=f"Passage {n}.", source_document_id=REPORT_ID,
        document_title="NI 43-101 Technical Report", section_number="14.2",
        section_title="Mineral Resource Estimate", section="14.2", page=100 + n,
        document_type="NI43", report_id=REPORT_ID, relevance_score=0.8,
    )


def _docs(*ns: int) -> tuple[str, DocumentSearchResult]:
    return ("search_documents", DocumentSearchResult(
        chunks=[_chunk(n) for n in ns], count=len(ns),
        data_source="qdrant:georag_chunks (reranked)",
    ))


GROUPED_FORMS = [
    "[NI43-1, NI43-2]",
    "[NI43-1; NI43-2]",
    "[NI43-1, 2]",
    "[NI43-1,NI43-2]",
    "[NI43-1 ; 2]",
    # review 2026-10-10, item 9: a range, a space, "and"
    "[NI43-1-2]",
    "[NI43-1–2]",
    "[NI43-1 NI43-2]",
    "[NI43-1 and NI43-2]",
    "[NI43-1 & 2]",
]


# ---------------------------------------------------------------------------
# The rewrite
# ---------------------------------------------------------------------------


class TestNormalizeGroupedMarkers:
    @pytest.mark.parametrize("grouped", GROUPED_FORMS)
    def test_each_form_becomes_adjacent_single_markers(self, grouped: str) -> None:
        assert normalize_grouped_markers(f"Claim {grouped}.") == "Claim [NI43-1][NI43-2]."

    def test_a_bare_number_takes_the_prefix_of_the_item_before_it(self) -> None:
        assert normalize_grouped_markers("[DATA-1, NI43-2, 5]") == "[DATA-1][NI43-2][NI43-5]"
        assert normalize_grouped_markers("[NI43-1, 2, 3]") == "[NI43-1][NI43-2][NI43-3]"

    def test_the_separator_style_of_each_full_item_is_kept(self) -> None:
        assert normalize_grouped_markers("[DATA:1, 2, NI43:3]") == "[DATA:1][DATA:2][NI43:3]"
        assert normalize_grouped_markers("[DATA:1, NI43-2]") == "[DATA:1][NI43-2]"

    def test_every_group_in_a_text_is_rewritten(self) -> None:
        assert normalize_grouped_markers(
            "A [NI43-1, NI43-2]. B [DATA-1; 2]. C [NI43-3]."
        ) == "A [NI43-1][NI43-2]. B [DATA-1][DATA-2]. C [NI43-3]."

    @pytest.mark.parametrize(
        "text",
        [
            "Claim [NI43-1].",
            "Claim [NI43-1] [NI43-2].",
            "Claim [NI43-1][NI43-2].",
            "A list [1, 2, 3] of numbers.",
            "See [Smith, 2012] and [NI43-1, Smith].",
            "Claim (NI43-1, NI43-2).",
            "Claim [ev:abc, ev:def].",
            "Drilled in 2011, 2012 and 2013, [see NI43-1, 2].",
            "No brackets, only commas; and semicolons.",
            "",
            # a space joins FULL items only: "12" here is a stray number, not a citation
            "Claim [NI43-1 12].",
            "Claim [NI43-1 and].",
            "Claim (NI43-1 NI43-2).",
            # a range runs upward and stays small
            "Claim [NI43-3-1].",
            "Claim [NI43-1-9].",
        ],
    )
    def test_anything_else_is_left_alone(self, text: str) -> None:
        assert normalize_grouped_markers(text) == text

    def test_a_range_expands_to_every_id_in_it(self) -> None:
        assert normalize_grouped_markers("[NI43-1-3]") == "[NI43-1][NI43-2][NI43-3]"
        assert normalize_grouped_markers("[NI43:2–4]") == "[NI43:2][NI43:3][NI43:4]"
        assert normalize_grouped_markers("[NI43-1-2, NI43-5]") == "[NI43-1][NI43-2][NI43-5]"
        assert normalize_grouped_markers("[DATA-1, NI43-2 and 5]") == "[DATA-1][NI43-2][NI43-5]"

    def test_a_range_invents_no_citation_an_invented_id_still_costs_itself(self) -> None:
        """"[NI43-1-4]" is four markers; only the ones that were retrieved
        survive Layers 2 and 5 (here 3 and 4 are not citations at all)."""
        response = _response("The resource is 8.6 Mt [NI43-1-4].", ids=(1, 2))
        out, _findings = validate_and_repair_with_findings(response)
        assert "[NI43-1]" in out.text and "[NI43-2]" in out.text
        assert "[NI43-3]" not in out.text and "[NI43-4]" not in out.text

    def test_pathological_input_stays_linear(self) -> None:
        import time

        for text in (
            "[NI43-1" + " NI43-1" * 50_000,
            "[NI43-1" + " and 1" * 50_000,
            "[NI43-1" + " " * 200_000,
            "[NI43-1-2" * 20_000,
        ):
            start = time.perf_counter()
            normalize_grouped_markers(text)
            assert time.perf_counter() - start < 2.0

    def test_a_year_after_a_marker_is_not_a_second_citation_by_accident(self) -> None:
        """"[NI43-1, 2012]" reads as ids 1 and 2012 -- the second is not a
        retrieved source, and the later layers say so."""
        assert normalize_grouped_markers("[NI43-1, 2012]") == "[NI43-1][NI43-2012]"

    def test_the_response_helper_keeps_the_object_when_there_is_nothing_to_do(self) -> None:
        response = _response("Claim [NI43-1].")
        assert ungroup_response_markers(response) is response

    def test_the_response_helper_moves_the_insights_offset_with_the_head(self) -> None:
        head = "The resource is 8.6 Mt [NI43-1, NI43-2].\n\n"
        insights = "**Proactive insights**\n- Note [NI43-1, NI43-2] stays as written."
        response = _response(head + insights, proactive_insights_offset=len(head))
        out = ungroup_response_markers(response)
        assert out.text.startswith("The resource is 8.6 Mt [NI43-1][NI43-2].")
        assert out.text.endswith(insights)  # system output, not rewritten
        assert out.text[out.proactive_insights_offset:] == insights


# ---------------------------------------------------------------------------
# Finding 5 -- a fully cited answer is not withheld
# ---------------------------------------------------------------------------


class TestGroupedMarkersDoNotWithholdACitedAnswer:
    @pytest.mark.parametrize("grouped", GROUPED_FORMS)
    def test_rule_4_keeps_a_sentence_cited_with_a_group(self, grouped: str) -> None:
        response = _response(f"The Indicated resource is 8.6 Mt at 0.12% eU3O8 {grouped}.")
        out, findings = enforce_claim_citations(response)
        assert findings == []
        assert out.text == "The Indicated resource is 8.6 Mt at 0.12% eU3O8 [NI43-1][NI43-2]."
        assert out.text != CITATION_REFUSAL_TEXT

    @pytest.mark.parametrize("grouped", GROUPED_FORMS)
    def test_an_answer_cited_only_with_groups_is_not_turned_into_a_refusal(
        self, grouped: str
    ) -> None:
        response = _response(
            f"The Indicated resource is 8.6 Mt {grouped}. "
            f"It contains 22.7 Mlbs U3O8 {grouped}. "
            f"The cut-off grade is 0.02% eU3O8 {grouped}."
        )
        out, findings = enforce_claim_citations(response)
        assert findings == []
        assert "8.6 Mt" in out.text and "22.7 Mlbs" in out.text and "0.02%" in out.text
        assert out.refusal_payload is None

    def test_layer_2_finds_no_orphan_in_a_group_of_real_citations(self) -> None:
        response = _response("The resource is 8.6 Mt [NI43-1, NI43-2].")
        out, findings = validate_and_repair_with_findings(response)
        assert findings == []
        assert out.text == "The resource is 8.6 Mt [NI43-1][NI43-2]."

    def test_layer_5_keeps_a_group_of_retrieved_citations(self) -> None:
        response = _response("The resource is 8.6 Mt [NI43-1, NI43-2].")
        out, warnings = gate_citation_provenance(response, [_docs(1, 2)])
        assert warnings == []
        assert out.text == "The resource is 8.6 Mt [NI43-1][NI43-2]."

    def test_assemble_response_reads_a_group_as_markers(self) -> None:
        """A cited answer that also says what it could not establish is a
        qualified answer; with its markers unreadable it looked like a
        refusal and was scored 0.1."""
        text = (
            "Metallurgical data is not available in the retrieved reports, but "
            "the zone returned 0.12% eU3O8 [NI43-1, NI43-2]."
        )
        response = assemble_response(text, [_docs(1, 2)])
        assert "[NI43-1][NI43-2]" in response.text
        assert response.confidence > 0.1


class TestAnInventedIdInsideAGroupIsStillCaught:
    def test_layer_2_removes_the_invented_id_and_keeps_the_real_one(self) -> None:
        response = _response("Grades average 0.62% U3O8 [NI43-1, NI43-9].")
        out, findings = validate_and_repair_with_findings(response)
        assert out.text == "Grades average 0.62% U3O8 [NI43-1]."
        assert findings and "[NI43-9]" in findings[0]

    def test_a_bare_invented_number_is_caught_the_same_way(self) -> None:
        response = _response("Grades average 0.62% U3O8 [NI43-1, 99].")
        out, findings = validate_and_repair_with_findings(response)
        assert "[NI43-99]" not in out.text
        assert "[NI43-1]" in out.text
        assert findings and "[NI43-99]" in findings[0]

    def test_a_group_of_nothing_real_takes_its_sentence_with_it(self) -> None:
        response = _response(
            "Grades average 0.62% U3O8 [NI43-8, NI43-9]. "
            "Hole 36-1085 intersected 0.12% eU3O8 [NI43-1, NI43-2]."
        )
        out, findings = validate_and_repair_with_findings(response)
        assert "0.62%" not in out.text
        assert "36-1085" in out.text
        assert findings

    def test_layer_5_rejects_one_id_of_a_group_and_keeps_the_other(self) -> None:
        response = _response("The resource is 8.6 Mt [NI43-1, NI43-2].")
        out, warnings = gate_citation_provenance(response, [_docs(1)])  # chunk 2 not retrieved
        assert len(warnings) == 1 and "[NI43-2]" in warnings[0]
        assert out.text == "The resource is 8.6 Mt [NI43-1]."
        assert [c.citation_id for c in out.citations] == ["[NI43-1]"]

    def test_layer_5_drops_the_sentence_when_every_id_of_the_group_is_rejected(self) -> None:
        response = _response("The resource is 8.6 Mt [NI43-1, NI43-2].")
        out, warnings = gate_citation_provenance(response, [_docs(3)])
        assert len(warnings) == 2
        assert "8.6 Mt" not in out.text


@dataclasses.dataclass
class _NodeDeps:
    openai_http_client: Any = None
    anthropic_client: Any = None
    pg_pool: Any = None
    neo4j_driver: Any = None
    redis_client: Any = None
    project_id: str = "5e2b8c1d-7a4f-4e39-b6d0-91c3a8f2e7b4"


class TestValidateNode:
    @pytest.mark.asyncio
    async def test_a_group_cited_answer_validates_clean(self, monkeypatch) -> None:
        import app.agent.hallucination.orchestrator_validators as validators
        from app.agent.agentic_retrieval.nodes import validate_node
        from app.agent.agentic_retrieval.state import AgenticRetrievalState

        async def _no_findings(resp, _tool_results, _deps):
            return resp, [], False

        monkeypatch.setattr(validators, "run_post_assembly_validation", _no_findings)
        state = AgenticRetrievalState(query="What is the resource?", deps=_NodeDeps())
        state = state.model_copy(update=dict(
            response=_response("The resource is 8.6 Mt [NI43-1, NI43-2]. It holds 22.7 Mlbs [NI43-1; 2]."),
            tool_results=[_docs(1, 2)],
        ))
        out = await validate_node(state)
        assert out["response"].validation_state == "clean"
        assert out["validation_warnings"] == []
        assert out["response"].text == (
            "The resource is 8.6 Mt [NI43-1][NI43-2]. It holds 22.7 Mlbs [NI43-1][NI43-2]."
        )


# ---------------------------------------------------------------------------
# Finding 6 -- [ev:...] cites nothing while the span resolver is off
# ---------------------------------------------------------------------------


class TestEvidenceMarkers:
    AUDIT_SENTENCE = "The resource is 48.2 Mt [ev:abc123]."

    def test_the_resolver_is_off_by_default(self) -> None:
        assert settings.CITATION_SPAN_RESOLVER_ENABLED is False

    def test_an_uncheckable_marker_does_not_pass_unchanged(self) -> None:
        """The audit repro."""
        out, findings = validate_and_repair_with_findings(_response(self.AUDIT_SENTENCE))
        assert "48.2 Mt" not in out.text
        assert out.text == CITATION_REFUSAL_TEXT
        assert findings and "[ev:abc123]" in findings[0]

    def test_it_is_not_a_citation_for_rule_4_either(self) -> None:
        out, findings = enforce_claim_citations(_response(self.AUDIT_SENTENCE))
        assert out.text == CITATION_REFUSAL_TEXT
        assert findings

    def test_a_real_marker_beside_it_keeps_the_sentence(self) -> None:
        response = _response("The resource is 48.2 Mt [NI43-1][ev:abc123].")
        out, findings = validate_and_repair_with_findings(response)
        assert out.text == "The resource is 48.2 Mt [NI43-1]."
        assert findings and "[ev:abc123]" in findings[0]

    def test_the_dash_form_is_the_same_marker(self) -> None:
        out, findings = validate_and_repair_with_findings(
            _response("The resource is 48.2 Mt [NI43-1][ev-abc123].")
        )
        assert "ev" not in out.text
        assert findings

    def test_with_the_resolver_on_an_ev_marker_is_a_citation_as_before(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "CITATION_SPAN_RESOLVER_ENABLED", True)
        response = _response(self.AUDIT_SENTENCE)
        out, findings = validate_and_repair_with_findings(response)
        assert findings == [] and out.text == self.AUDIT_SENTENCE
        out, findings = enforce_claim_citations(response)
        assert findings == [] and out is response
