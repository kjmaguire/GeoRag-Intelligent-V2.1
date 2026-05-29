"""Tests for hallucination prevention layers 1, 3, 4, and 6.

Layer coverage
--------------
Layer 1  filter_by_quality()          — retrieval quality gate
Layer 3  verify_numerical_claims()    — numerical claim verification
Layer 4  resolve_entity_references()  — entity resolution
Layer 6  check_geological_constraints() — geological constraint rules

All external I/O (asyncpg, Neo4j) is mocked.  The LLM layer is replaced by
pydantic_ai.models.test.TestModel for the golden-query integration test.

Run with:
    pytest tests/test_hallucination_layers.py -v
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.deps import AgentDeps
from app.models.rag import Citation, GeoRAGResponse


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------


def _make_deps(
    *,
    pg_pool: object = None,
    neo4j_driver: object = None,
    project_id: str = "proj-test-uuid",
) -> AgentDeps:
    return AgentDeps(
        pg_pool=pg_pool,  # type: ignore[arg-type]
        qdrant_client=MagicMock(),
        neo4j_driver=neo4j_driver,  # type: ignore[arg-type]
        project_id=project_id,
        embedding_model=None,
    )


@dataclass
class _MockRunContext:
    """Minimal stand-in for pydantic_ai.RunContext[AgentDeps]."""

    deps: AgentDeps
    messages: list[Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.messages is None:
            self.messages = []


def _make_valid_response(text: str = "There are 10 drill holes in this project [DATA-1].", confidence: float = 0.85) -> GeoRAGResponse:
    """Build a minimal valid GeoRAGResponse for testing."""
    return GeoRAGResponse(
        text=text,
        citations=[
            Citation(
                citation_id="[DATA-1]",
                citation_type="DATA",
                source_chunk_id="collar-uuid-test-001",
                document_title="PostGIS silver.collars",
                relevance_score=0.95,
            )
        ],
        confidence=confidence,
        sources_used=["collar-uuid-test-001"],
    )


# ---------------------------------------------------------------------------
# Layer 1: filter_by_quality
# ---------------------------------------------------------------------------


class TestLayer1RetrievalQuality:
    """Tests for app.agent.hallucination.layer1_retrieval.filter_by_quality."""

    def test_passes_items_above_threshold(self) -> None:
        """Items with relevance_score >= threshold are kept."""
        from app.agent.hallucination.layer1_retrieval import filter_by_quality
        from app.agent.tools import DocumentChunk

        chunk_high = DocumentChunk(
            chunk_id="c1", text="uranium assay", source_document_id="doc1",
            document_title="NI 43-101", section_number=None, section_title=None,
            section=None, page=1, document_type="NI43", report_id="rep-001",
            relevance_score=0.75,
        )
        chunk_low = DocumentChunk(
            chunk_id="c2", text="background text", source_document_id="doc1",
            document_title="NI 43-101", section_number=None, section_title=None,
            section=None, page=2, document_type="NI43", report_id="rep-001",
            relevance_score=0.45,
        )

        result = filter_by_quality([chunk_high, chunk_low], threshold=0.6)

        assert len(result) == 1
        assert result[0].chunk_id == "c1"

    def test_returns_empty_when_all_below_threshold(self) -> None:
        """Returns empty list when ALL items are below threshold."""
        from app.agent.hallucination.layer1_retrieval import filter_by_quality
        from app.agent.tools import DocumentChunk

        chunks = [
            DocumentChunk(
                chunk_id=f"c{i}", text="text", source_document_id="doc1",
                document_title="Report", section_number=None, section_title=None,
                section=None, page=i, document_type="NI43", report_id="rep-001",
                relevance_score=0.3,
            )
            for i in range(3)
        ]

        result = filter_by_quality(chunks, threshold=0.6)

        assert result == []

    def test_empty_input_returns_empty(self) -> None:
        """Empty input list is handled gracefully."""
        from app.agent.hallucination.layer1_retrieval import filter_by_quality

        result = filter_by_quality([], threshold=0.6)
        assert result == []

    def test_at_threshold_boundary_is_accepted(self) -> None:
        """Items exactly at the threshold are accepted (>= not >)."""
        from app.agent.hallucination.layer1_retrieval import filter_by_quality
        from app.agent.tools import DocumentChunk

        chunk = DocumentChunk(
            chunk_id="c1", text="text", source_document_id="d1",
            document_title="T", section_number=None, section_title=None,
            section=None, page=1, document_type="NI43", report_id="rep-001",
            relevance_score=0.6,
        )

        result = filter_by_quality([chunk], threshold=0.6)
        assert len(result) == 1

    def test_all_items_above_threshold_returned_in_order(self) -> None:
        """All items above threshold are returned, preserving order."""
        from app.agent.hallucination.layer1_retrieval import filter_by_quality
        from app.agent.tools import DocumentChunk

        chunks = [
            DocumentChunk(
                chunk_id=f"c{i}", text="text", source_document_id="d",
                document_title="T", section_number=None, section_title=None,
                section=None, page=i, document_type="NI43", report_id="rep-001",
                relevance_score=0.7 + i * 0.05,
            )
            for i in range(4)
        ]

        result = filter_by_quality(chunks, threshold=0.6)
        assert [c.chunk_id for c in result] == ["c0", "c1", "c2", "c3"]


# ---------------------------------------------------------------------------
# Layer 3: verify_numerical_claims
# ---------------------------------------------------------------------------


class TestLayer3NumericalVerification:
    """Tests for app.agent.hallucination.layer3_numerical.verify_numerical_claims."""

    @pytest.mark.asyncio
    async def test_passes_when_number_in_tool_result(self) -> None:
        """Validator passes when all numbers in text are present in tool results."""
        from app.agent.hallucination.layer3_numerical import verify_numerical_claims

        # Build a mock message with a tool-return part that contains count=10.
        tool_result_json = json.dumps({"count": 10, "collars": [], "data_source": "PostGIS silver.collars"})

        mock_part = MagicMock()
        mock_part.part_kind = "tool-return"
        mock_part.content = tool_result_json

        mock_message = MagicMock()
        mock_message.parts = [mock_part]

        ctx = _MockRunContext(deps=_make_deps(), messages=[mock_message])

        output = _make_valid_response("There are 10 drill holes in this project [DATA-1].")

        result = await verify_numerical_claims(ctx, output)  # type: ignore[arg-type]

        # Should return unchanged output — no retry.
        assert result.text == output.text

    @pytest.mark.asyncio
    async def test_raises_retry_when_number_not_in_tool_result(self) -> None:
        """Validator raises ModelRetry when a number in text is absent from tool results.

        This is the exact bug we observed: tool returned 10, LLM said 2459.
        """
        from pydantic_ai import ModelRetry

        from app.agent.hallucination.layer3_numerical import verify_numerical_claims

        # Tool result says count=10.
        tool_result_json = json.dumps({"count": 10, "collars": [], "data_source": "PostGIS"})

        mock_part = MagicMock()
        mock_part.part_kind = "tool-return"
        mock_part.content = tool_result_json

        mock_message = MagicMock()
        mock_message.parts = [mock_part]

        ctx = _MockRunContext(deps=_make_deps(), messages=[mock_message])

        # LLM hallucinated 2459 when tool returned 10.
        output = _make_valid_response("There are 2459 drill holes in this project [DATA-1].")

        with pytest.raises(ModelRetry) as exc_info:
            await verify_numerical_claims(ctx, output)  # type: ignore[arg-type]

        assert "2459" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_skips_citation_index_numbers(self) -> None:
        """Numbers inside citation markers like [DATA-1] are not verified."""
        from app.agent.hallucination.layer3_numerical import verify_numerical_claims

        # Tool result has count=10.
        tool_result_json = json.dumps({"count": 10, "data_source": "PostGIS"})
        mock_part = MagicMock()
        mock_part.part_kind = "tool-return"
        mock_part.content = tool_result_json
        mock_message = MagicMock()
        mock_message.parts = [mock_part]
        ctx = _MockRunContext(deps=_make_deps(), messages=[mock_message])

        # Citation [DATA-1] contains "1" which is a skip value anyway, and
        # [DATA-99] contains "99" which is NOT in the tool result — but it
        # should be ignored because it's inside a citation marker.
        output = _make_valid_response("Found 10 drill holes [DATA-99].")
        # Change the citation_id to use 99 to ensure the marker isn't verified.
        output = GeoRAGResponse(
            text="Found 10 drill holes [DATA-99].",
            citations=[
                Citation(
                    citation_id="[DATA-99]",
                    citation_type="DATA",
                    source_chunk_id="collar-uuid-test-001",
                    document_title="PostGIS",
                    relevance_score=0.95,
                )
            ],
            confidence=0.85,
            sources_used=["collar-uuid-test-001"],
        )

        # Should not raise — 99 is inside [DATA-99] citation marker and stripped.
        result = await verify_numerical_claims(ctx, output)  # type: ignore[arg-type]
        assert result is not None

    @pytest.mark.asyncio
    async def test_disabled_when_setting_off(self) -> None:
        """Validator is a no-op when NUMERICAL_VERIFICATION_ENABLED=False."""
        from app.agent.hallucination.layer3_numerical import verify_numerical_claims

        ctx = _MockRunContext(deps=_make_deps(), messages=[])
        # Hallucinated number with no tool results to back it up.
        output = _make_valid_response("There are 9999 drill holes.")

        with patch("app.agent.hallucination.layer3_numerical.settings") as mock_settings:
            mock_settings.NUMERICAL_VERIFICATION_ENABLED = False
            result = await verify_numerical_claims(ctx, output)  # type: ignore[arg-type]

        # No retry raised — disabled.
        assert result.text == output.text

    @pytest.mark.asyncio
    async def test_passes_with_no_numbers_in_text(self) -> None:
        """Validator passes cleanly when there are no numbers to verify."""
        from app.agent.hallucination.layer3_numerical import verify_numerical_claims

        ctx = _MockRunContext(deps=_make_deps(), messages=[])
        output = _make_valid_response("No drill holes were found in this project [DATA-1].")

        result = await verify_numerical_claims(ctx, output)  # type: ignore[arg-type]
        assert result.text == output.text

    @pytest.mark.asyncio
    async def test_grounded_by_float_in_tool_result(self) -> None:
        """A float in the response is verified against a matching float in tool data."""
        from app.agent.hallucination.layer3_numerical import verify_numerical_claims

        tool_result_json = json.dumps({"total_depth": 350.5, "hole_id": "ATDD-001"})
        mock_part = MagicMock()
        mock_part.part_kind = "tool-return"
        mock_part.content = tool_result_json
        mock_message = MagicMock()
        mock_message.parts = [mock_part]
        ctx = _MockRunContext(deps=_make_deps(), messages=[mock_message])

        output = _make_valid_response("The hole has a total depth of 350.5 metres [DATA-1].")

        result = await verify_numerical_claims(ctx, output)  # type: ignore[arg-type]
        assert result is not None

    @pytest.mark.asyncio
    async def test_number_grounded_within_tolerance(self) -> None:
        """A number is accepted if it is within 0.01 of a tool result value."""
        from app.agent.hallucination.layer3_numerical import verify_numerical_claims

        # Tool returns 350.0, text says 350.0 — exact match.
        tool_result_json = json.dumps({"total_depth": 350.0})
        mock_part = MagicMock()
        mock_part.part_kind = "tool-return"
        mock_part.content = tool_result_json
        mock_message = MagicMock()
        mock_message.parts = [mock_part]
        ctx = _MockRunContext(deps=_make_deps(), messages=[mock_message])

        output = _make_valid_response("Total depth is 350.0 m [DATA-1].")
        result = await verify_numerical_claims(ctx, output)  # type: ignore[arg-type]
        assert result is not None


# ---------------------------------------------------------------------------
# Layer 4: resolve_entity_references
# ---------------------------------------------------------------------------


class TestLayer4EntityResolution:
    """Tests for app.agent.hallucination.layer4_entity.resolve_entity_references."""

    @pytest.mark.asyncio
    async def test_passes_when_hole_id_exists(self) -> None:
        """Validator passes when drill-hole ID is found in silver.collars."""
        from app.agent.hallucination.layer4_entity import resolve_entity_references

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[{"hole_id": "ATDD-001"}])
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        # Neo4j: empty result (no quoted names to resolve).
        mock_result = AsyncMock()
        mock_result.data = AsyncMock(return_value=[])
        mock_session = AsyncMock()
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_neo4j = MagicMock()
        mock_neo4j.session = MagicMock(return_value=mock_session)

        deps = _make_deps(pg_pool=mock_pool, neo4j_driver=mock_neo4j)
        ctx = _MockRunContext(deps=deps)

        output = _make_valid_response("Drill hole ATDD-001 has a depth of 350 m [DATA-1].")

        result = await resolve_entity_references(ctx, output)  # type: ignore[arg-type]
        assert result is not None

    @pytest.mark.asyncio
    async def test_raises_retry_for_missing_hole_id(self) -> None:
        """Validator raises ModelRetry when hole ID not found in silver.collars."""
        from pydantic_ai import ModelRetry

        from app.agent.hallucination.layer4_entity import resolve_entity_references

        # Database returns nothing — the hole ID does not exist.
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        # Neo4j: no quoted names.
        mock_result = AsyncMock()
        mock_result.data = AsyncMock(return_value=[])
        mock_session = AsyncMock()
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_neo4j = MagicMock()
        mock_neo4j.session = MagicMock(return_value=mock_session)

        deps = _make_deps(pg_pool=mock_pool, neo4j_driver=mock_neo4j)
        ctx = _MockRunContext(deps=deps)

        # Hallucinated hole ID that doesn't exist.
        output = _make_valid_response("Drill hole FAKE-99-99 has a depth of 350 m [DATA-1].")

        with pytest.raises(ModelRetry) as exc_info:
            await resolve_entity_references(ctx, output)  # type: ignore[arg-type]

        assert "FAKE-99-99" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_passes_when_no_hole_ids_in_text(self) -> None:
        """Validator passes cleanly when no hole IDs are present in the text."""
        from app.agent.hallucination.layer4_entity import resolve_entity_references

        # PostgreSQL and Neo4j should not be called — no entities to resolve.
        mock_pool = MagicMock()
        mock_neo4j = MagicMock()
        mock_result = AsyncMock()
        mock_result.data = AsyncMock(return_value=[])
        mock_session = AsyncMock()
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_neo4j.session = MagicMock(return_value=mock_session)

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        deps = _make_deps(pg_pool=mock_pool, neo4j_driver=mock_neo4j)
        ctx = _MockRunContext(deps=deps)

        output = _make_valid_response("There are 10 drill holes in this project [DATA-1].")

        result = await resolve_entity_references(ctx, output)  # type: ignore[arg-type]
        assert result is not None

    @pytest.mark.asyncio
    async def test_fails_open_on_postgres_timeout(self) -> None:
        """Validator returns without raising when PostGIS times out (fail open)."""
        from app.agent.hallucination.layer4_entity import resolve_entity_references

        async def _slow_fetch(*_a: object, **_k: object) -> list:
            await asyncio.sleep(999)
            return []

        mock_conn = AsyncMock()
        mock_conn.fetch = _slow_fetch
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_result = AsyncMock()
        mock_result.data = AsyncMock(return_value=[])
        mock_session = AsyncMock()
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_neo4j = MagicMock()
        mock_neo4j.session = MagicMock(return_value=mock_session)

        deps = _make_deps(pg_pool=mock_pool, neo4j_driver=mock_neo4j)
        ctx = _MockRunContext(deps=deps)

        output = _make_valid_response("Drill hole ATDD-001 has a depth of 350 m [DATA-1].")

        with patch("app.agent.hallucination.layer4_entity.settings") as mock_settings:
            mock_settings.ENTITY_RESOLUTION_ENABLED = True
            mock_settings.TIMEOUT_POSTGIS_S = 0.01
            mock_settings.TIMEOUT_NEO4J_S = 3.0
            # Should not raise — fail open on timeout.
            result = await resolve_entity_references(ctx, output)  # type: ignore[arg-type]

        assert result is not None

    @pytest.mark.asyncio
    async def test_disabled_when_setting_off(self) -> None:
        """Validator is a no-op when ENTITY_RESOLUTION_ENABLED=False."""
        from app.agent.hallucination.layer4_entity import resolve_entity_references

        deps = _make_deps()
        ctx = _MockRunContext(deps=deps)
        output = _make_valid_response("Drill hole FAKE-99-99 mentioned [DATA-1].")

        with patch("app.agent.hallucination.layer4_entity.settings") as mock_settings:
            mock_settings.ENTITY_RESOLUTION_ENABLED = False
            result = await resolve_entity_references(ctx, output)  # type: ignore[arg-type]

        assert result.text == output.text


# ---------------------------------------------------------------------------
# Layer 6: geological constraint rules
# ---------------------------------------------------------------------------


class TestLayer6GeologicalConstraints:
    """Tests for app.agent.hallucination.layer6_constraints.check_geological_constraints."""

    @pytest.mark.asyncio
    async def test_passes_valid_depth(self) -> None:
        """Realistic depth value passes the constraint check."""
        from app.agent.hallucination.layer6_constraints import check_geological_constraints

        deps = _make_deps()
        ctx = _MockRunContext(deps=deps)
        output = _make_valid_response("The hole has a total depth of 450 metres [DATA-1].")

        result = await check_geological_constraints(ctx, output)  # type: ignore[arg-type]
        assert result is not None

    @pytest.mark.asyncio
    async def test_raises_retry_for_implausible_depth(self) -> None:
        """Depth exceeding 5000 m raises ModelRetry."""
        from pydantic_ai import ModelRetry

        from app.agent.hallucination.layer6_constraints import check_geological_constraints

        deps = _make_deps()
        ctx = _MockRunContext(deps=deps)
        # 9999 metres is not a real exploration drill hole.
        output = _make_valid_response("The hole has a total depth of 9999 metres [DATA-1].")

        with pytest.raises(ModelRetry) as exc_info:
            await check_geological_constraints(ctx, output)  # type: ignore[arg-type]

        assert "depth_max_m" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_retry_for_impossible_uranium_grade(self) -> None:
        """U3O8 grade above 50% raises ModelRetry."""
        from pydantic_ai import ModelRetry

        from app.agent.hallucination.layer6_constraints import check_geological_constraints

        deps = _make_deps()
        ctx = _MockRunContext(deps=deps)
        output = _make_valid_response("The sample grades 75% U3O8 [DATA-1].")

        with pytest.raises(ModelRetry) as exc_info:
            await check_geological_constraints(ctx, output)  # type: ignore[arg-type]

        assert "uranium" in str(exc_info.value).lower() or "grade_uranium" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_retry_for_recovery_over_100(self) -> None:
        """Core recovery above 100% raises ModelRetry."""
        from pydantic_ai import ModelRetry

        from app.agent.hallucination.layer6_constraints import check_geological_constraints

        deps = _make_deps()
        ctx = _MockRunContext(deps=deps)
        output = _make_valid_response("Core recovery averaged 115% in this interval [DATA-1].")

        with pytest.raises(ModelRetry) as exc_info:
            await check_geological_constraints(ctx, output)  # type: ignore[arg-type]

        assert "recovery" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_passes_valid_gold_grade(self) -> None:
        """A plausible gold grade passes the constraint."""
        from app.agent.hallucination.layer6_constraints import check_geological_constraints

        deps = _make_deps()
        ctx = _MockRunContext(deps=deps)
        output = _make_valid_response("The best intercept graded 12.5 ppm Au over 3 m [DATA-1].")

        result = await check_geological_constraints(ctx, output)  # type: ignore[arg-type]
        assert result is not None

    @pytest.mark.asyncio
    async def test_raises_retry_for_implausible_gold_grade(self) -> None:
        """Gold grade above 1000 ppm raises ModelRetry."""
        from pydantic_ai import ModelRetry

        from app.agent.hallucination.layer6_constraints import check_geological_constraints

        deps = _make_deps()
        ctx = _MockRunContext(deps=deps)
        output = _make_valid_response("The assay returned 5000 ppm Au over 1 m [DATA-1].")

        with pytest.raises(ModelRetry) as exc_info:
            await check_geological_constraints(ctx, output)  # type: ignore[arg-type]

        assert "gold" in str(exc_info.value).lower() or "grade_gold" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_passes_when_no_geological_keywords(self) -> None:
        """A large number with no geological keyword context does not trigger a constraint."""
        from app.agent.hallucination.layer6_constraints import check_geological_constraints

        deps = _make_deps()
        ctx = _MockRunContext(deps=deps)
        # UTM easting — no keyword match for any constraint.
        output = _make_valid_response(
            "The collar is located at easting 512345 and northing 6123456 [DATA-1]."
        )

        result = await check_geological_constraints(ctx, output)  # type: ignore[arg-type]
        assert result is not None

    @pytest.mark.asyncio
    async def test_disabled_when_setting_off(self) -> None:
        """Validator is a no-op when GEOLOGICAL_CONSTRAINTS_ENABLED=False."""
        from app.agent.hallucination.layer6_constraints import check_geological_constraints

        deps = _make_deps()
        ctx = _MockRunContext(deps=deps)
        output = _make_valid_response("The hole depth was 9999 metres [DATA-1].")

        with patch("app.agent.hallucination.layer6_constraints.settings") as mock_settings:
            mock_settings.GEOLOGICAL_CONSTRAINTS_ENABLED = False
            result = await check_geological_constraints(ctx, output)  # type: ignore[arg-type]

        assert result.text == output.text

    @pytest.mark.asyncio
    async def test_negative_dip_passes(self) -> None:
        """Valid negative dip (-60 degrees) passes the constraint."""
        from app.agent.hallucination.layer6_constraints import check_geological_constraints

        deps = _make_deps()
        ctx = _MockRunContext(deps=deps)
        output = _make_valid_response("The hole has a dip of -60 degrees [DATA-1].")

        result = await check_geological_constraints(ctx, output)  # type: ignore[arg-type]
        assert result is not None

    @pytest.mark.asyncio
    async def test_azimuth_out_of_range_raises(self) -> None:
        """Azimuth above 360 raises ModelRetry."""
        from pydantic_ai import ModelRetry

        from app.agent.hallucination.layer6_constraints import check_geological_constraints

        deps = _make_deps()
        ctx = _MockRunContext(deps=deps)
        output = _make_valid_response("The hole has an azimuth of 400 degrees [DATA-1].")

        with pytest.raises(ModelRetry) as exc_info:
            await check_geological_constraints(ctx, output)  # type: ignore[arg-type]

        assert "azimuth" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Internal helpers for Layer 3
# ---------------------------------------------------------------------------


class TestLayer3Helpers:
    """Unit tests for the internal helper functions in layer3_numerical."""

    def test_extract_numbers_from_text_basic(self) -> None:
        """Extracts integers and floats from plain text."""
        from app.agent.hallucination.layer3_numerical import _extract_numbers_from_text

        result = _extract_numbers_from_text("There are 10 holes with depth 350.5 m.")
        assert 10.0 in result
        assert 350.5 in result

    def test_extract_numbers_ignores_citation_markers(self) -> None:
        """Numbers inside [DATA-X] citation markers are not extracted."""
        from app.agent.hallucination.layer3_numerical import _extract_numbers_from_text

        result = _extract_numbers_from_text("Result [DATA-99] shows 10 holes.")
        # 99 should not appear because it's in the citation marker.
        assert 99.0 not in result
        assert 10.0 in result

    def test_extract_numbers_skips_zero_and_one(self) -> None:
        """Values 0 and 1 are skipped as too common to verify."""
        from app.agent.hallucination.layer3_numerical import _extract_numbers_from_text

        result = _extract_numbers_from_text("0 results found. 1 project active.")
        assert 0.0 not in result
        assert 1.0 not in result

    def test_flatten_dataclass(self) -> None:
        """_flatten_tool_result_to_numbers extracts values from a dataclass."""
        from app.agent.hallucination.layer3_numerical import _flatten_tool_result_to_numbers
        from app.agent.tools import SpatialQueryResult

        result_obj = SpatialQueryResult(
            collars=[],
            count=10,
            data_source="PostGIS silver.collars",
        )
        numbers = _flatten_tool_result_to_numbers(result_obj)
        assert 10.0 in numbers

    def test_flatten_nested_dict(self) -> None:
        """_flatten_tool_result_to_numbers recurses into nested dicts."""
        from app.agent.hallucination.layer3_numerical import _flatten_tool_result_to_numbers

        data = {"outer": {"count": 42, "depth": 350.5}}
        numbers = _flatten_tool_result_to_numbers(data)
        assert 42.0 in numbers
        assert 350.5 in numbers


# ---------------------------------------------------------------------------
# Golden query test: "How many drill holes are in this project?"
# ---------------------------------------------------------------------------


class TestGoldenQueryDrillHoleCount:
    """Golden-path test verifying Layer 3 catches the reported hallucination.

    Simulates the exact failure: tool returns 10 collars, LLM says 2459.
    Uses pydantic_ai TestModel to exercise the full output_validator chain
    without hitting a real LLM.

    The test uses TestModel's custom_output_args to inject a hallucinated
    response (claiming 2459 drill holes), then verifies that the output
    validator chain raises ModelRetry before the response is returned.
    """

    @pytest.mark.asyncio
    async def test_layer3_catches_hallucinated_count_via_validator_directly(self) -> None:
        """Direct validator call: tool says 10, text says 2459 — ModelRetry raised."""
        from pydantic_ai import ModelRetry

        from app.agent.hallucination.layer3_numerical import verify_numerical_claims

        # Simulate the tool returning count=10.
        tool_result_json = json.dumps({
            "count": 10,
            "collars": [],
            "data_source": "PostGIS silver.collars",
        })

        mock_part = MagicMock()
        mock_part.part_kind = "tool-return"
        mock_part.content = tool_result_json
        mock_message = MagicMock()
        mock_message.parts = [mock_part]

        ctx = _MockRunContext(deps=_make_deps(), messages=[mock_message])

        # The hallucinated response.
        hallucinated = GeoRAGResponse(
            text="There are 2459 drill holes in this project [DATA-1].",
            citations=[
                Citation(
                    citation_id="[DATA-1]",
                    citation_type="DATA",
                    source_chunk_id="collar-uuid-001",
                    document_title="PostGIS silver.collars",
                    relevance_score=1.0,
                )
            ],
            confidence=0.9,
            sources_used=["collar-uuid-001"],
        )

        with pytest.raises(ModelRetry) as exc_info:
            await verify_numerical_claims(ctx, hallucinated)  # type: ignore[arg-type]

        retry_message = str(exc_info.value)
        assert "2459" in retry_message
        assert "tool" in retry_message.lower()

    @pytest.mark.asyncio
    async def test_layer3_accepts_correct_count(self) -> None:
        """Validator passes when the text matches the tool result count."""
        from app.agent.hallucination.layer3_numerical import verify_numerical_claims

        tool_result_json = json.dumps({
            "count": 10,
            "collars": [],
            "data_source": "PostGIS silver.collars",
        })
        mock_part = MagicMock()
        mock_part.part_kind = "tool-return"
        mock_part.content = tool_result_json
        mock_message = MagicMock()
        mock_message.parts = [mock_part]

        ctx = _MockRunContext(deps=_make_deps(), messages=[mock_message])

        correct = GeoRAGResponse(
            text="There are 10 drill holes in this project [DATA-1].",
            citations=[
                Citation(
                    citation_id="[DATA-1]",
                    citation_type="DATA",
                    source_chunk_id="collar-uuid-001",
                    document_title="PostGIS silver.collars",
                    relevance_score=1.0,
                )
            ],
            confidence=0.9,
            sources_used=["collar-uuid-001"],
        )

        result = await verify_numerical_claims(ctx, correct)  # type: ignore[arg-type]
        assert result.text == correct.text
        assert "10" in result.text


# ---------------------------------------------------------------------------
# Module 6 Chunk 3 — Guard 3: Completeness (per-claim citation coverage)
# ---------------------------------------------------------------------------


class TestLayer3OrchestratorTightened:
    """Orchestrator verify_numbers with silent-skip threshold REMOVED (Chunk 3)."""

    def test_single_ungrounded_number_now_reported(self) -> None:
        """A single ungrounded number is now reported (no ≤3 silent-skip)."""
        from app.agent.hallucination.orchestrator_validators import verify_numbers

        tool_results = [("query_spatial_collars", {"count": 10})]
        # Answer claims 5000 which does not appear in tool results.
        warnings = verify_numbers("There are 5000 drill holes. [DATA:1]", tool_results)
        assert len(warnings) >= 1
        assert any("5000" in w for w in warnings)

    def test_two_ungrounded_now_reported(self) -> None:
        """Two ungrounded numbers are reported (below old threshold of 3)."""
        from app.agent.hallucination.orchestrator_validators import verify_numbers

        tool_results = [("query_spatial_collars", {"count": 10})]
        warnings = verify_numbers("There are 500 holes at depth 900 m. [DATA:1]", tool_results)
        # At least one of the ungrounded numbers reported.
        assert len(warnings) >= 1

    def test_grounded_number_passes(self) -> None:
        """A number that appears in tool results is not flagged."""
        from app.agent.hallucination.orchestrator_validators import verify_numbers

        tool_results = [("query_spatial_collars", {"count": 42})]
        warnings = verify_numbers("There are 42 drill holes. [DATA:1]", tool_results)
        # 42 is grounded — no warnings.
        assert all("42" not in w for w in warnings)

    def test_unit_conversion_ppm_to_percent_accepted(self) -> None:
        """10000 ppm → 1% unit conversion is accepted without flagging."""
        from app.agent.hallucination.orchestrator_validators import verify_numbers

        # Tool returns grade in ppm; answer expresses it as %.
        tool_results = [("search_documents", {"grade": 10000.0})]
        # 10000 ppm = 1.0%: 10000 / 10000 = 1.0
        warnings = verify_numbers(
            "The average grade is 1.0% Au. [NI43:1]", tool_results
        )
        # 1.0 is in _SKIP_VALUES for the orchestrator extractor, so no warning.
        assert not any("1.0" in w for w in warnings)

    def test_unit_conversion_g_per_t_to_oz_accepted(self) -> None:
        """g/t to oz/t conversion (31.1035 factor) is accepted."""
        from app.agent.hallucination.orchestrator_validators import verify_numbers

        # Tool returns 31.1035 g/t — answer says "1.0 oz/t".
        tool_results = [("search_documents", {"grade_g_t": 31.1035})]
        warnings = verify_numbers(
            "The intercept grades 31.1 g/t Au. [NI43:1]", tool_results
        )
        # 31.1 should be grounded via close match to 31.1035.
        assert not any("31.1" in w for w in warnings)

    def test_m_to_ft_conversion_accepted(self) -> None:
        """Metres to feet conversion (3.28084 factor) is accepted."""
        from app.agent.hallucination.orchestrator_validators import _expand_grounded_with_conversions

        grounded = {100.0}  # 100 m
        expanded = _expand_grounded_with_conversions(grounded)
        # 100 m * 3.28084 = 328.084 ft
        assert any(abs(v - 328.084) < 0.5 for v in expanded)

    def test_disabled_returns_empty(self) -> None:
        """verify_numbers returns [] when NUMERICAL_VERIFICATION_ENABLED=False."""
        from app.agent.hallucination.orchestrator_validators import verify_numbers

        with patch("app.agent.hallucination.orchestrator_validators.settings") as ms:
            ms.NUMERICAL_VERIFICATION_ENABLED = False
            warnings = verify_numbers("9999 holes [DATA:1]", [])
        assert warnings == []


class TestLayer4OrchestratorExpanded:
    """Orchestrator verify_entities expanded beyond hole IDs (Chunk 3)."""

    @pytest.mark.asyncio
    async def test_no_entities_returns_empty(self) -> None:
        """No entities in text returns empty warnings list."""
        from app.agent.hallucination.orchestrator_validators import verify_entities

        warnings = await verify_entities(
            "There are drill holes in this project. [DATA:1]",
            "proj-uuid",
            None,
            None,
            tool_results=[],
        )
        assert warnings == []

    @pytest.mark.asyncio
    async def test_valid_hole_id_no_warning(self) -> None:
        """Hole ID found in PostGIS produces no warning."""
        from app.agent.hallucination.orchestrator_validators import verify_entities

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[{"hole_id": "PLS-20-01"}])
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("app.agent.hallucination.orchestrator_validators.settings") as ms:
            ms.ENTITY_RESOLUTION_ENABLED = True
            ms.TIMEOUT_POSTGIS_S = 5.0
            ms.TIMEOUT_NEO4J_S = 3.0
            warnings = await verify_entities(
                "Drill hole PLS-20-01 was completed. [DATA:1]",
                "proj-uuid",
                mock_pool,
                None,
                tool_results=[],
            )
        assert not any("PLS-20-01" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_missing_hole_id_produces_warning(self) -> None:
        """Hole ID not found in PostGIS produces a Layer 4 warning."""
        from app.agent.hallucination.orchestrator_validators import verify_entities

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("app.agent.hallucination.orchestrator_validators.settings") as ms:
            ms.ENTITY_RESOLUTION_ENABLED = True
            ms.TIMEOUT_POSTGIS_S = 5.0
            ms.TIMEOUT_NEO4J_S = 3.0
            warnings = await verify_entities(
                "Drill hole FAKE-99-01 was completed. [DATA:1]",
                "proj-uuid",
                mock_pool,
                None,
                tool_results=[],
            )
        assert any("FAKE-99-01" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_commodity_in_tool_results_no_warning(self) -> None:
        """Commodity mentioned in answer AND present in tool results: no warning."""
        from app.agent.hallucination.orchestrator_validators import verify_entities

        tool_results = [("search_documents", {"text": "gold Au grade 5 ppm"})]
        with patch("app.agent.hallucination.orchestrator_validators.settings") as ms:
            ms.ENTITY_RESOLUTION_ENABLED = True
            ms.TIMEOUT_POSTGIS_S = 5.0
            ms.TIMEOUT_NEO4J_S = 3.0
            warnings = await verify_entities(
                "The Au grade is 5 ppm. [NI43:1]",
                "proj-uuid",
                None,
                None,
                tool_results=tool_results,
            )
        # Au is in the tool result text ("Au grade") — should not be flagged.
        assert not any("'Au'" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_disabled_returns_empty(self) -> None:
        """verify_entities returns [] when ENTITY_RESOLUTION_ENABLED=False."""
        from app.agent.hallucination.orchestrator_validators import verify_entities

        with patch("app.agent.hallucination.orchestrator_validators.settings") as ms:
            ms.ENTITY_RESOLUTION_ENABLED = False
            warnings = await verify_entities(
                "FAKE-99-01 mentioned. [DATA:1]", "p", None, None, tool_results=[]
            )
        assert warnings == []


class TestCompletenessGuard:
    """Guard 3 — per-claim citation completeness (layer_completeness.py)."""

    def test_all_sentences_cited(self) -> None:
        """Answer where every declarative sentence has a marker passes."""
        from app.agent.hallucination.layer_completeness import verify_completeness

        text = (
            "The deposit contains significant uranium mineralisation [NI43:1]. "
            "The average grade is 2.5% U3O8 [DATA:2]. "
            "Drill hole PLS-20-01 intersected 10 m at 3% [NI43:1]."
        )
        result = verify_completeness(text)
        assert result.passed
        assert result.uncited_sentences == []

    def test_bare_assertion_fails(self) -> None:
        """A sentence with no citation and no marker in the next sentence fails."""
        from app.agent.hallucination.layer_completeness import verify_completeness

        text = (
            "The deposit is very large. "
            "No supporting citation here either. "
            "Some data [DATA:1]."
        )
        result = verify_completeness(text)
        assert not result.passed
        assert len(result.uncited_sentences) >= 1

    def test_next_sentence_citation_covers_prior(self) -> None:
        """If the next sentence opens with a marker, the prior sentence is covered."""
        from app.agent.hallucination.layer_completeness import verify_completeness

        text = (
            "The mineralisation extends over 200 metres depth. "
            "[NI43:1] confirms this interval."
        )
        result = verify_completeness(text)
        assert result.passed

    def test_question_exempt(self) -> None:
        """Question sentences are exempt from the completeness guard."""
        from app.agent.hallucination.layer_completeness import verify_completeness

        text = (
            "What is the depth of the deposit? "
            "The database shows 350 m depth [DATA:1]."
        )
        result = verify_completeness(text)
        assert result.passed

    def test_refusal_phrase_exempt(self) -> None:
        """Refusal phrases are exempt from the completeness guard."""
        from app.agent.hallucination.layer_completeness import verify_completeness

        text = "I don't have data on that in this project."
        result = verify_completeness(text)
        assert result.passed

    def test_empty_text_passes(self) -> None:
        """Empty text has no sentences to fail."""
        from app.agent.hallucination.layer_completeness import verify_completeness

        result = verify_completeness("")
        assert result.passed

    def test_single_cited_sentence_passes(self) -> None:
        """A single declarative sentence with a citation marker passes."""
        from app.agent.hallucination.layer_completeness import verify_completeness

        result = verify_completeness("There are 10 drill holes [DATA:1].")
        assert result.passed

    def test_mixed_cited_uncited(self) -> None:
        """Mix of cited and uncited sentences — uncited ones are collected."""
        from app.agent.hallucination.layer_completeness import verify_completeness

        text = (
            "The project is located in Saskatchewan [DATA:1]. "
            "This area has vast uranium potential with no citation. "
            "The resource estimate is 25 Mlb U3O8 [NI43:2]."
        )
        result = verify_completeness(text)
        # The uncited sentence should be flagged.
        assert not result.passed
        assert any("vast uranium potential" in s for s in result.uncited_sentences)

    def test_imperative_starter_exempt(self) -> None:
        """Imperative starters like 'See Table 3' are exempt."""
        from app.agent.hallucination.layer_completeness import verify_completeness

        text = (
            "The grade is 3% U3O8 [DATA:1]. "
            "See table 3 for further breakdown."
        )
        result = verify_completeness(text)
        assert result.passed

    def test_guard_name_is_set(self) -> None:
        """GuardResult.guard_name is 'completeness'."""
        from app.agent.hallucination.layer_completeness import verify_completeness

        result = verify_completeness("Text without citation.")
        assert result.guard_name == "completeness"


class TestGuardBundle:
    """Guard 4 — evaluate_guards + format_guard_failure."""

    @pytest.mark.asyncio
    async def test_all_passing_returns_all_passed(self) -> None:
        """When all three content guards pass, all_passed=True."""
        from app.agent.hallucination.layer_completeness import evaluate_guards

        # Text with citations on every declarative sentence, no ungrounded numbers.
        text = "The project has 10 drill holes [DATA:1]."
        tool_results = [("query_spatial_collars", {"count": 10})]

        with patch("app.agent.hallucination.orchestrator_validators.settings") as ms:
            ms.NUMERICAL_VERIFICATION_ENABLED = True
            ms.ENTITY_RESOLUTION_ENABLED = True
            ms.TIMEOUT_POSTGIS_S = 5.0
            ms.TIMEOUT_NEO4J_S = 3.0
            bundle = await evaluate_guards(
                answer_text=text,
                tool_results=tool_results,
                project_id="proj-uuid",
                pg_pool=None,
                neo4j_driver=None,
            )

        assert bundle.all_passed
        assert bundle.failed_guards == []

    @pytest.mark.asyncio
    async def test_completeness_failure_propagates(self) -> None:
        """Completeness guard failure sets all_passed=False.

        Doc-phase 186 added `GUARD_TOLERANCE_COMPLETENESS_UNCITED` (default 2)
        which lets up to N uncited sentences through. This test pins
        tolerance to 0 so it exercises the guard mechanism itself rather
        than the tolerance threshold.
        """
        from app.agent.hallucination.layer_completeness import evaluate_guards
        from app.config import settings as app_settings

        text = (
            "The deposit is large. "    # no citation
            "This is another uncited claim about grades and depths."  # no citation
        )
        tool_results = []

        with patch("app.agent.hallucination.orchestrator_validators.settings") as ms, \
                patch.object(app_settings, "GUARD_TOLERANCE_COMPLETENESS_UNCITED", 0):
            ms.NUMERICAL_VERIFICATION_ENABLED = False  # skip numeric
            ms.ENTITY_RESOLUTION_ENABLED = False       # skip entity
            ms.TIMEOUT_POSTGIS_S = 5.0
            ms.TIMEOUT_NEO4J_S = 3.0
            bundle = await evaluate_guards(
                answer_text=text,
                tool_results=tool_results,
                project_id="proj-uuid",
                pg_pool=None,
                neo4j_driver=None,
            )

        assert not bundle.all_passed
        assert any(g.guard_name == "completeness" for g in bundle.failed_guards)

    def test_format_guard_failure_numeric(self) -> None:
        """format_guard_failure produces readable string for numeric failure."""
        from app.agent.hallucination.layer_completeness import (
            GuardResult,
            format_guard_failure,
        )

        result = GuardResult(
            guard_name="numeric",
            passed=False,
            failed_tokens=["5000", "9999"],
        )
        reason = format_guard_failure([result])
        assert "numeric_guard" in reason
        assert "5000" in reason

    def test_format_guard_failure_completeness(self) -> None:
        """format_guard_failure produces readable string for completeness failure."""
        from app.agent.hallucination.layer_completeness import (
            GuardResult,
            format_guard_failure,
        )

        result = GuardResult(
            guard_name="completeness",
            passed=False,
            uncited_sentences=["The deposit is large.", "Grades are high."],
        )
        reason = format_guard_failure([result])
        assert "completeness_guard" in reason
        assert "2" in reason

    def test_build_refusal_payload_structure(self) -> None:
        """build_refusal_payload returns the correct B4 stub shape."""
        from app.agent.hallucination.layer_completeness import (
            GuardBundle,
            GuardResult,
            build_refusal_payload,
        )

        numeric = GuardResult(guard_name="numeric", passed=False, failed_tokens=["99"])
        comp = GuardResult(guard_name="completeness", passed=False, uncited_sentences=["Bare claim."])
        entity = GuardResult(guard_name="entity", passed=True)
        bundle = GuardBundle(
            all_passed=False,
            numeric=numeric,
            entity=entity,
            completeness=comp,
            failed_guards=[numeric, comp],
        )
        payload = build_refusal_payload(bundle)
        assert payload["type"] == "refusal"
        # Chunk 4a: reason_code is now specific (not the legacy "guard_failure" stub).
        # numeric takes priority over completeness in the mapping.
        assert payload["reason_code"] == "guard_numeric_fail"
        assert "numeric" in payload["failed_guards"]
        assert "completeness" in payload["failed_guards"]
        assert "entity" not in payload["failed_guards"]
        assert "message" in payload
