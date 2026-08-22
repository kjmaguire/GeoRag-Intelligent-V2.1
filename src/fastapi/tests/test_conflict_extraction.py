"""A field read in four places and written in none of them.

``GeoRAGResponse.conflicting_evidence`` is declared in the schema, consumed by
``apply_guard_demotion`` (``conflicts_present``), by persist_node's telemetry
and by the repair loop. Its only writers were two test files, so
``conflicts_present`` was permanently False and the "conflicting sources force
Low confidence" rule could not fire — while the ``synthesis_with_conflicts``
prompt fragment was ordering the model to emit a "### Conflicting evidence"
sub-section that nothing read.

And rule 21 had the model write "_None detected in the retrieved corpus._" —
an affirmative claim about the whole corpus, from a reader that saw a
similarity-ranked slice of it cut to fit a context budget.
"""

from __future__ import annotations

import pytest

from app.agent.conflict_extraction import extract_conflicting_evidence

SECTION = """## Interpretations

The Rowan zone is described consistently across sources.

### Conflicting evidence

- PLS-22-08 — total depth: 510 m [NI43-1] vs 498 m [NI43-3]
- Rowan zone — Au grade: 2.31 g/t [NI43-2] vs 1.87 g/t [PGEO-4]

## Uncertainty
"""

NO_CONFLICTS = """### Conflicting evidence

_No disagreement found among the passages provided._

## Uncertainty
"""

LEGACY_NONE = """### Conflicting evidence
_None detected in the retrieved corpus._
"""


class TestNothingToReport:
    def test_no_section_returns_none(self) -> None:
        assert extract_conflicting_evidence("A plain cited answer. [NI43-1]") is None

    def test_empty_text_returns_none(self) -> None:
        assert extract_conflicting_evidence("") is None

    def test_the_none_line_is_not_a_conflict(self) -> None:
        assert extract_conflicting_evidence(NO_CONFLICTS) is None

    def test_the_legacy_none_line_is_also_recognised(self) -> None:
        """Answers generated before the rule-21 rewording are still in the
        conversation history and still get replayed."""
        assert extract_conflicting_evidence(LEGACY_NONE) is None

    def test_a_header_with_nothing_under_it_returns_none(self) -> None:
        assert extract_conflicting_evidence("### Conflicting evidence\n\n## Next") is None

    def test_none_rather_than_empty_list(self) -> None:
        """Two spellings of "no conflicts" would be one state too many: the
        schema default is None and every consumer tests truthiness.

        Fixed 2026-08-21. This asserted ``is not []``, which compares
        identity against a freshly-built list and is therefore True for
        every possible return value including ``[]`` -- the exact thing the
        test exists to forbid. It had never been capable of failing. Found
        by ruff F632, which CI runs and which is currently red on this
        branch for unrelated reasons in src/fastapi/scripts/.
        """
        result = extract_conflicting_evidence(NO_CONFLICTS)

        assert result is None, (
            f"expected None, got {result!r} — an empty list is a second "
            "spelling of 'no conflicts', and consumers that test "
            "truthiness cannot tell the two apart while the schema can"
        )


class TestParsing:
    def test_one_row_per_bullet(self) -> None:
        rows = extract_conflicting_evidence(SECTION)

        assert rows is not None
        assert len(rows) == 2

    def test_the_entity_and_property_are_split_when_the_bullet_uses_the_form(
        self,
    ) -> None:
        rows = extract_conflicting_evidence(SECTION)

        assert rows[0]["entity_key"] == "PLS-22-08"
        assert rows[0]["property_name"] == "total depth"

    def test_citations_on_both_sides_are_captured(self) -> None:
        rows = extract_conflicting_evidence(SECTION)

        assert rows[0]["evidence_ids"] == ["[NI43-1]", "[NI43-3]"]
        assert rows[1]["evidence_ids"] == ["[NI43-2]", "[PGEO-4]"]

    def test_the_two_values_are_separated(self) -> None:
        """The row header is not one of the values.

        The first side of "vs" carries the "<entity> - <property>:" label the
        bullet opens with; stripping it is what makes `values` the two things
        that actually disagree.
        """
        rows = extract_conflicting_evidence(SECTION)

        assert rows[0]["values"] == ["510 m", "498 m"]
        assert rows[1]["values"] == ["2.31 g/t", "1.87 g/t"]

    def test_the_model_s_own_sentence_is_kept_verbatim(self) -> None:
        """A reader must be able to see what was said, not only the parse."""
        rows = extract_conflicting_evidence(SECTION)

        assert rows[1]["claim"] == (
            "Rowan zone — Au grade: 2.31 g/t [NI43-2] vs 1.87 g/t [PGEO-4]"
        )

    def test_an_unparseable_bullet_still_produces_a_row_with_nulls(self) -> None:
        """Guessing an entity out of free prose is how a parser starts
        inventing data. None is the honest answer, and every consumer reads
        this field for presence only."""
        rows = extract_conflicting_evidence(
            "### Conflicting evidence\n- The two reports disagree [NI43-1] [NI43-2]\n"
        )

        assert rows is not None and len(rows) == 1
        assert rows[0]["entity_key"] is None
        assert rows[0]["property_name"] is None
        assert rows[0]["evidence_ids"] == ["[NI43-1]", "[NI43-2]"]

    def test_a_conflict_with_no_citation_is_kept_but_marked(self) -> None:
        """Dropping it would hide the weakest claims and keep the strongest.
        The empty evidence_ids list is the signal."""
        rows = extract_conflicting_evidence(
            "### Conflicting evidence\n- The depths disagree.\n"
        )

        assert rows is not None and rows[0]["evidence_ids"] == []

    def test_prose_that_is_not_a_bullet_is_ignored(self) -> None:
        rows = extract_conflicting_evidence(
            "### Conflicting evidence\nSome preamble sentence.\n- A — b: 1 [NI43-1] vs 2 [NI43-2]\n"
        )

        assert rows is not None and len(rows) == 1

    @pytest.mark.parametrize("marker", ["-", "*", "+", "1.", "2)"])
    def test_bullet_styles(self, marker: str) -> None:
        rows = extract_conflicting_evidence(
            f"### Conflicting evidence\n{marker} A — b: 1 [NI43-1] vs 2 [NI43-2]\n"
        )

        assert rows is not None and len(rows) == 1

    @pytest.mark.parametrize("depth", ["#", "##", "###", "####"])
    def test_header_depth_does_not_matter(self, depth: str) -> None:
        """The fragment orders '###' verbatim; models shift heading levels
        whenever the surrounding document nests differently."""
        rows = extract_conflicting_evidence(
            f"{depth} Conflicting evidence\n- A — b: 1 [NI43-1] vs 2 [NI43-2]\n"
        )

        assert rows is not None and len(rows) == 1

    def test_the_section_stops_at_the_next_header(self) -> None:
        rows = extract_conflicting_evidence(
            "### Conflicting evidence\n"
            "- A — b: 1 [NI43-1] vs 2 [NI43-2]\n"
            "## Recommended actions\n"
            "- Re-log the hole [NI43-5]\n"
        )

        assert rows is not None and len(rows) == 1
        assert "Re-log" not in rows[0]["claim"]


class TestTheRuleItDrives:
    def test_a_parsed_conflict_makes_conflicts_present_true(self) -> None:
        """The whole point. `apply_guard_demotion` reads
        `bool(response.conflicting_evidence)`; before there was a writer it
        read False for every answer ever produced."""
        rows = extract_conflicting_evidence(SECTION)

        assert bool(rows) is True

    def test_no_conflicts_leaves_it_false(self) -> None:
        assert bool(extract_conflicting_evidence(NO_CONFLICTS)) is False


class TestThePromptNoLongerOverclaims:
    def test_rule_21_does_not_assert_anything_about_the_corpus(self) -> None:
        from app.agent.prompts.answer_emphasis_section import (
            SYNTHESIS_WITH_CONFLICTS_EMPHASIS as FRAGMENT,
        )

        assert "_None detected in the retrieved corpus._" not in FRAGMENT
        assert "No disagreement found among the passages provided" in FRAGMENT

    def test_it_says_why(self) -> None:
        from app.agent.prompts.answer_emphasis_section import (
            SYNTHESIS_WITH_CONFLICTS_EMPHASIS as FRAGMENT,
        )

        assert "Say what you checked, not what exists." in FRAGMENT

    def test_the_model_is_told_it_is_the_only_detector(self) -> None:
        from app.agent.prompts.answer_emphasis_section import (
            SYNTHESIS_WITH_CONFLICTS_EMPHASIS as FRAGMENT,
        )

        assert "there is no upstream conflict detector" in FRAGMENT


class TestValidateNodeWiresIt:
    """The parser existing is not the fix — something has to call it."""

    @pytest.mark.asyncio
    async def test_validate_node_populates_the_field(self, monkeypatch) -> None:
        import dataclasses

        import app.agent.hallucination.orchestrator_validators as validators
        from app.agent.agentic_retrieval.nodes import validate_node
        from app.agent.agentic_retrieval.state import AgenticRetrievalState
        from app.models.rag import Citation, GeoRAGResponse

        @dataclasses.dataclass
        class _Deps:
            openai_http_client: object | None = None
            anthropic_client: object | None = None
            pg_pool: object | None = None
            neo4j_driver: object | None = None
            redis_client: object | None = None
            project_id: str = "test-project"

        response = GeoRAGResponse(
            text=SECTION,
            citations=[
                Citation(
                    citation_id="[NI43-1]",
                    citation_type="NI43",
                    source_chunk_id="chunk-1",
                    document_title="Technical report",
                    relevance_score=0.9,
                ),
            ],
            sources_used=["chunk-1"],
            confidence=0.8,
        )

        async def fake(resp, tool_results, deps):
            return resp, [], False

        monkeypatch.setattr(validators, "run_post_assembly_validation", fake)
        state = AgenticRetrievalState(query="q", deps=_Deps())
        state = state.model_copy(update={"response": response})

        out = await validate_node(state)

        assert out["response"].conflicting_evidence is not None
        assert len(out["response"].conflicting_evidence) == 2

    @pytest.mark.asyncio
    async def test_an_ordinary_answer_leaves_it_none(self, monkeypatch) -> None:
        import dataclasses

        import app.agent.hallucination.orchestrator_validators as validators
        from app.agent.agentic_retrieval.nodes import validate_node
        from app.agent.agentic_retrieval.state import AgenticRetrievalState
        from app.models.rag import Citation, GeoRAGResponse

        @dataclasses.dataclass
        class _Deps:
            openai_http_client: object | None = None
            anthropic_client: object | None = None
            pg_pool: object | None = None
            neo4j_driver: object | None = None
            redis_client: object | None = None
            project_id: str = "test-project"

        response = GeoRAGResponse(
            text="Hole PLS-22-08 reached 510 m. [NI43-1]",
            citations=[
                Citation(
                    citation_id="[NI43-1]",
                    citation_type="NI43",
                    source_chunk_id="chunk-1",
                    document_title="Technical report",
                    relevance_score=0.9,
                ),
            ],
            sources_used=["chunk-1"],
            confidence=0.8,
        )

        async def fake(resp, tool_results, deps):
            return resp, [], False

        monkeypatch.setattr(validators, "run_post_assembly_validation", fake)
        state = AgenticRetrievalState(query="q", deps=_Deps())
        state = state.model_copy(update={"response": response})

        out = await validate_node(state)

        assert out["response"].conflicting_evidence is None
