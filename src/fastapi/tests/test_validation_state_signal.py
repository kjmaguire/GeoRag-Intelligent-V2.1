"""`confidence` answers "did a tool return rows". Something has to answer the rest.

``_extract_relevance`` returns a flat ``1.0`` for every non-empty structured
result — AssayData, DownholeLogs, CollarDetails, SpatialQuery, GraphTraversal,
ProjectOverview, ProjectSummary, CoverageGap, DrillTrace3D and Stereonet all
score ``1.0 if count > 0 else 0.0``. Only DocumentSearch and
PublicGeoscienceSearch carry a real score. ``_compute_confidence`` takes the
arithmetic mean and caps at 0.95, so **any** question asked of a project that
has drilling reports exactly 0.95, however tangential the synthesis.

The schema said that number was a "composite confidence score across all 6
hallucination prevention layers". No layer contributes to it; the layers only
subtract, via the 0.2 floor `validate_node` applies on a guard failure. And the
UI rendered ``conf 0.05`` and ``conf 0.95`` in the same neutral blue pill.

Fixed by splitting the signal rather than by inventing a synthesis-quality
number at assembly time, before the guards have run:
``GeoRAGResponse.validation_state`` carries what the §04i guards concluded.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.response_assembler import _compute_confidence, _extract_relevance
from app.models.rag import Citation, GeoRAGResponse


@dataclasses.dataclass
class _Deps:
    openai_http_client: object | None = None
    anthropic_client: object | None = None
    pg_pool: object | None = None
    neo4j_driver: object | None = None
    redis_client: object | None = None
    project_id: str = "test-project"


def _response(**over) -> GeoRAGResponse:
    base = {
        "text": "Hole PLS-22-08 reached 510 m. [DATA-1]",
        "citations": [
            Citation(
                citation_id="[DATA-1]",
                citation_type="DATA",
                source_chunk_id="silver.collars:count=1:first=abc",
                document_title="Collar data",
                relevance_score=0.9,
            ),
        ],
        "sources_used": ["[DATA-1]"],
        "confidence": 0.8,
    }
    base.update(over)
    return GeoRAGResponse(**base)


async def _run_validate(monkeypatch, *, warnings, should_retry, raises=False):
    import app.agent.hallucination.orchestrator_validators as validators
    from app.agent.agentic_retrieval.nodes import validate_node

    async def fake(resp, tool_results, deps):
        if raises:
            raise RuntimeError("a guard blew up")
        return resp, list(warnings), should_retry

    monkeypatch.setattr(validators, "run_post_assembly_validation", fake)
    state = AgenticRetrievalState(query="q", deps=_Deps())
    state = state.model_copy(update={"response": _response()})
    return await validate_node(state)


class TestTheFieldIsHonestNow:
    def test_the_description_no_longer_claims_to_be_a_composite_of_the_layers(
        self,
    ) -> None:
        description = GeoRAGResponse.model_fields["confidence"].description or ""

        assert "across all 6 hallucination prevention layers" not in description
        assert "RETRIEVAL strength" in description

    def test_the_description_points_the_reader_at_the_other_signal(self) -> None:
        description = GeoRAGResponse.model_fields["confidence"].description or ""

        assert "validation_state" in description


class TestValidationStateDefault:
    def test_an_unvalidated_response_is_unverified_not_clean(self) -> None:
        """The default has to be the one that does not claim a check ran.

        `assemble_node` builds the response; `validate_node` runs after it.
        Anything that reads a response between those two points — a test, a
        replay, an eval harness, an exception path — must not see 'clean'.
        """
        assert _response().validation_state == "unverified"

    def test_it_survives_a_model_dump(self) -> None:
        """The completed SSE frame is `final.model_dump()`, so the field only
        reaches the browser if it serialises."""
        assert _response().model_dump()["validation_state"] == "unverified"


class TestValidateNodeSetsIt:
    @pytest.mark.asyncio
    async def test_no_warnings_is_clean(self, monkeypatch) -> None:
        out = await _run_validate(monkeypatch, warnings=[], should_retry=False)

        assert out["response"].validation_state == "clean"

    @pytest.mark.asyncio
    async def test_one_warning_is_flagged_even_below_the_retry_threshold(
        self, monkeypatch,
    ) -> None:
        """The case the old UX lost completely.

        A single "Layer 3: Ungrounded number" sits below NUMERIC_RETRY_THRESHOLD
        (3), so `should_retry` stays False: no banner, no confidence floor. The
        designed backstop was Stage-2 demotion, and `demote_node` returns
        immediately while GEO_ANSWER_OIUR_ENABLED is False. So the answer
        shipped at full retrieval-derived confidence with no user-visible
        signal of any kind.
        """
        out = await _run_validate(
            monkeypatch,
            warnings=["Layer 3: Ungrounded number 12.4 g/t"],
            should_retry=False,
        )

        assert out["response"].validation_state == "flagged"
        # Unchanged: flagging is the new signal, not a new penalty.
        assert out["response"].confidence == pytest.approx(0.8)

    @pytest.mark.asyncio
    async def test_should_retry_is_flagged_and_still_floored(
        self, monkeypatch,
    ) -> None:
        out = await _run_validate(
            monkeypatch,
            warnings=["Layer 4: Drill-hole ID DDH-9999 not found"],
            should_retry=True,
        )
        response = out["response"]

        assert response.validation_state == "flagged"
        assert response.confidence <= 0.2
        assert response.text.startswith("**Note:")

    @pytest.mark.asyncio
    async def test_a_guard_exception_is_unverified_not_flagged(
        self, monkeypatch,
    ) -> None:
        """Fail-closed, and distinguishable. 'flagged' would say a check ran
        and found something; nothing ran."""
        out = await _run_validate(
            monkeypatch, warnings=[], should_retry=False, raises=True,
        )

        assert out["response"].validation_state == "unverified"
        assert out["response"].confidence <= 0.2


class TestWhatConfidenceActuallyMeasures:
    """Pinning the behaviour the field description now states, so the two
    cannot drift apart again."""

    @pytest.mark.parametrize("count", [1, 40, 4000])
    def test_a_structured_result_scores_the_same_at_any_size(self, count) -> None:
        from app.agent.tools import SpatialQueryResult

        result = SpatialQueryResult.__new__(SpatialQueryResult)
        object.__setattr__(result, "count", count)

        assert _extract_relevance(result) == 1.0

    def test_a_structured_only_answer_reports_the_cap(self) -> None:
        """0.95 — the number a geologist reads as near-certainty — is what a
        single PostGIS query returning one row produces."""
        from app.agent.tools import SpatialQueryResult

        result = SpatialQueryResult.__new__(SpatialQueryResult)
        object.__setattr__(result, "count", 1)

        assert _compute_confidence(
            [("query_spatial_collars", result)], text="Some answer.",
        ) == pytest.approx(0.95)

    def test_no_tool_call_still_floors(self) -> None:
        assert _compute_confidence([], text="Some answer.") == pytest.approx(0.1)
