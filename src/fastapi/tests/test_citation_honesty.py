"""The answer text is returned as the model wrote it.

CLAUDE.md hard rule 4 says every claim must carry a source_chunk_id "or be
rejected by typed output validation. There is no best-effort citation mode."
The assembler implemented the opposite: when the model emitted no citation
markers it appended every citation id to the last sentence, and when no tool
ran at all it fabricated a `[DATA-1]` citation with source_chunk_id
"no-tool-call" and appended that marker too.

Both turned a detectable failure into an undetectable one. A five-sentence
geological interpretation with zero markers came back reading "... [NI43-1]
[NI43-2] [DATA-3]."; the frontend rendered three citation chips; every claim
looked sourced and no claim was mapped to any chunk. A fabricated sentence was
indistinguishable from a grounded one.

It also defeated the guard built to catch it. classify_guards raises
CITATION_INCOMPLETE when `response_citations` is empty -- but the assembler
guaranteed it never was, because of the placeholder. So the code path existed,
was tested in isolation, and could not fire in production.
"""

from __future__ import annotations

from app.agent.guards import GuardErrorCode, classify_guards
from app.agent.hallucination.citation_markers import CITATION_MARKER_RE
from app.agent.response_assembler import EMPTY_SOURCE_SENTINELS, assemble_response
from app.models.rag import Citation


def _citation(citation_id: str, chunk_id: str) -> Citation:
    return Citation(
        citation_id=citation_id,
        citation_type="DATA",
        source_chunk_id=chunk_id,
        document_title="Some report",
        section=None,
        page=None,
        relevance_score=0.9,
    )


class TestAssemblerLeavesTheAnswerAlone:
    def test_uncited_prose_is_not_given_markers(self) -> None:
        prose = (
            "The deposit is a structurally controlled orogenic gold system. "
            "Mineralisation is hosted in a shear zone. "
            "Grades improve at depth."
        )

        response = assemble_response(text=prose, tool_results=[])

        assert response.text == prose
        assert not CITATION_MARKER_RE.search(response.text)

    def test_the_no_tool_call_placeholder_is_not_written_into_the_answer(self) -> None:
        response = assemble_response(text="An answer with no evidence.", tool_results=[])

        # The placeholder still exists -- GeoRAGResponse requires at least one
        # Citation -- but presenting "no tool call executed" to a reader as a
        # source is exactly the lie this removes.
        assert len(response.citations) == 1
        assert response.citations[0].source_chunk_id in EMPTY_SOURCE_SENTINELS
        assert "[DATA-1]" not in response.text

    def test_an_ungrounded_answer_still_has_its_confidence_floored(self) -> None:
        """Removing the cosmetic fix must not remove the real one."""
        response = assemble_response(text="Grades average 8.7 g/t.", tool_results=[])

        assert response.confidence <= 0.05


class TestCitationCompletenessGuardCanActuallyFire:
    def test_sentinel_only_citations_count_as_no_citations(self) -> None:
        codes = classify_guards(
            response_citations=[_citation("[DATA-1]", "no-tool-call")],
            text_has_markers=False,
        )

        assert GuardErrorCode.CITATION_INCOMPLETE in codes

    def test_citations_present_but_answer_cites_nothing(self) -> None:
        """The exact state the assembler used to paper over."""
        codes = classify_guards(
            response_citations=[_citation("[NI43-1]", "chunk-abc")],
            text_has_markers=False,
        )

        assert GuardErrorCode.CITATION_INCOMPLETE in codes

    def test_a_properly_cited_answer_does_not_fire(self) -> None:
        codes = classify_guards(
            response_citations=[_citation("[NI43-1]", "chunk-abc")],
            text_has_markers=True,
        )

        assert GuardErrorCode.CITATION_INCOMPLETE not in codes

    def test_no_signal_means_no_inference(self) -> None:
        """`None` is "the caller did not say", not "the answer is uncited"."""
        codes = classify_guards(validation_warnings=[])

        assert GuardErrorCode.CITATION_INCOMPLETE not in codes
