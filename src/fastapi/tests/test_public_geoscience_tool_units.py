"""Unit tests for the Public Geoscience retrieval tool.

Covers all pure utility functions and graceful-degradation paths in
``app/agent/public_geoscience_tool.py``.  No live services required —
all I/O is mocked.

Run with:
    pytest tests/test_public_geoscience_tool_units.py -v
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.deps import AgentDeps
from app.agent.public_geoscience_tool import (
    PublicGeoscienceRecord,
    PublicGeoscienceSearchResult,
    _derive_name,
    _maybe_bbox,
    _normalize_bbox,
    _normalize_strings,
    _passes_bbox,
    _qdrant_filter,
    search_public_geoscience,
)

# ---------------------------------------------------------------------------
# Test context shim (matches _MockRunContext in test_agent_tools.py)
# ---------------------------------------------------------------------------


@dataclass
class _MockRunContext:
    """Minimal stand-in for pydantic_ai.RunContext[AgentDeps]."""

    deps: AgentDeps


def _make_deps(
    *,
    pg_pool: object = None,
    qdrant_client: object = None,
    embedding_model: object = None,
) -> AgentDeps:
    """Build a minimal AgentDeps for unit tests."""
    return AgentDeps(
        pg_pool=pg_pool,  # type: ignore[arg-type]
        qdrant_client=qdrant_client,  # type: ignore[arg-type]
        neo4j_driver=MagicMock(),  # type: ignore[arg-type]
        project_id="test-project-uuid",
        embedding_model=embedding_model,
    )


# ---------------------------------------------------------------------------
# _normalize_strings
# ---------------------------------------------------------------------------


class TestNormalizeStrings:
    """Unit tests for the _normalize_strings helper."""

    def test_none_returns_empty_list(self) -> None:
        assert _normalize_strings(None) == []

    def test_empty_list_returns_empty_list(self) -> None:
        assert _normalize_strings([]) == []

    def test_whitespace_only_strings_are_filtered(self) -> None:
        result = _normalize_strings(["CA-SK", "  ", "\t", "CA-BC"])
        assert result == ["CA-SK", "CA-BC"]

    def test_non_str_values_are_coerced_to_string(self) -> None:
        # list with integers / None mixed in
        result = _normalize_strings([1, None, "Au"])  # type: ignore[list-item]
        # None is filtered by the `if v is not None` guard
        # integers get str()-coerced
        assert "1" in result
        assert "Au" in result
        assert len(result) == 2

    def test_single_valid_string(self) -> None:
        result = _normalize_strings(["Saskatchewan"])
        assert result == ["Saskatchewan"]

    def test_strips_surrounding_whitespace(self) -> None:
        result = _normalize_strings(["  CA-SK  ", " Au "])
        assert result == ["CA-SK", "Au"]


# ---------------------------------------------------------------------------
# _normalize_bbox
# ---------------------------------------------------------------------------


class TestNormalizeBbox:
    """Unit tests for the _normalize_bbox helper."""

    def test_none_returns_none(self) -> None:
        assert _normalize_bbox(None) is None

    def test_four_element_list_returns_tuple_of_floats(self) -> None:
        result = _normalize_bbox([1, 2, 3, 4])
        assert result == (1.0, 2.0, 3.0, 4.0)
        assert isinstance(result[0], float)

    def test_wrong_length_returns_none(self) -> None:
        assert _normalize_bbox([1, 2, 3]) is None
        assert _normalize_bbox([1, 2, 3, 4, 5]) is None

    def test_non_numeric_returns_none(self) -> None:
        assert _normalize_bbox(["a", "b", "c", "d"]) is None

    def test_mixed_numeric_types_coerced(self) -> None:
        result = _normalize_bbox([1, 2.5, 3, 4.0])
        assert result == (1.0, 2.5, 3.0, 4.0)

    def test_tuple_input_accepted(self) -> None:
        result = _normalize_bbox((10.0, 20.0, 30.0, 40.0))
        assert result == (10.0, 20.0, 30.0, 40.0)


# ---------------------------------------------------------------------------
# _passes_bbox
# ---------------------------------------------------------------------------


class TestPassesBbox:
    """Unit tests for the AABB overlap filter."""

    def test_no_requested_bbox_always_passes(self) -> None:
        # No bbox requested — every hit passes.
        assert _passes_bbox(hit=[100.0, 50.0, 110.0, 60.0], requested=None) is True

    def test_overlap_returns_true(self) -> None:
        # Both bboxes overlap.
        hit = [-106.0, 51.0, -104.0, 53.0]
        requested = (-105.0, 50.0, -103.0, 52.0)
        assert _passes_bbox(hit=hit, requested=requested) is True

    def test_non_overlap_x_axis_returns_false(self) -> None:
        # Hit is entirely to the left of requested (h_max_lon < r_min_lon).
        hit = [-120.0, 51.0, -115.0, 55.0]
        requested = (-110.0, 51.0, -105.0, 55.0)
        assert _passes_bbox(hit=hit, requested=requested) is False

    def test_non_overlap_y_axis_returns_false(self) -> None:
        # Hit is entirely above requested (h_min_lat > r_max_lat).
        hit = [-106.0, 60.0, -104.0, 65.0]
        requested = (-106.0, 50.0, -104.0, 55.0)
        assert _passes_bbox(hit=hit, requested=requested) is False

    def test_missing_hit_bbox_is_fail_open(self) -> None:
        # When hit bbox is None we cannot tell — keep the record.
        requested = (-106.0, 51.0, -104.0, 53.0)
        assert _passes_bbox(hit=None, requested=requested) is True

    def test_touching_boundary_is_overlap(self) -> None:
        # Bboxes share an edge — that counts as overlap.
        hit = [-106.0, 51.0, -104.0, 53.0]
        requested = (-104.0, 51.0, -102.0, 53.0)
        assert _passes_bbox(hit=hit, requested=requested) is True


# ---------------------------------------------------------------------------
# _maybe_bbox
# ---------------------------------------------------------------------------


class TestMaybeBbox:
    """Unit tests for the payload bbox coercion helper."""

    def test_four_element_list_returns_list_of_floats(self) -> None:
        result = _maybe_bbox([1, 2, 3, 4])
        assert result == [1.0, 2.0, 3.0, 4.0]
        assert all(isinstance(v, float) for v in result)

    def test_three_element_returns_none(self) -> None:
        assert _maybe_bbox([1, 2, 3]) is None

    def test_none_returns_none(self) -> None:
        assert _maybe_bbox(None) is None

    def test_non_numeric_returns_none(self) -> None:
        assert _maybe_bbox(["a", "b", "c", "d"]) is None


# ---------------------------------------------------------------------------
# _derive_name
# ---------------------------------------------------------------------------


class TestDeriveName:
    """Unit tests for the display-title derivation helper."""

    def test_prefers_summary_text_clipped_at_first_period(self) -> None:
        payload: dict[str, Any] = {
            "summary_text": "Active gold mine at Seabee. Operated by SSR Mining.",
            "canonical_type": "mine",
        }
        name = _derive_name(payload, "mine")
        assert name == "Active gold mine at Seabee"
        assert "." not in name  # clipped at first period

    def test_falls_back_to_canonical_type_when_no_summary(self) -> None:
        payload: dict[str, Any] = {"summary_text": ""}
        name = _derive_name(payload, "mineral_occurrence")
        assert "Mineral Occurrence" in name or "mineral_occurrence" in name.lower()

    def test_falls_back_when_summary_is_none(self) -> None:
        payload: dict[str, Any] = {"summary_text": None}
        name = _derive_name(payload, "drillhole_collar")
        assert name  # non-empty

    def test_long_summary_clipped_at_120_chars(self) -> None:
        payload: dict[str, Any] = {
            "summary_text": "A" * 200 + ". Second sentence."
        }
        name = _derive_name(payload, "mine")
        assert len(name) <= 120


# ---------------------------------------------------------------------------
# _qdrant_filter
# ---------------------------------------------------------------------------


class TestQdrantFilter:
    """Unit tests for the Qdrant payload filter builder."""

    def test_none_jurisdictions_and_none_commodities_returns_none(self) -> None:
        result = _qdrant_filter(jurisdictions=[], commodities=[])
        assert result is None

    def test_both_populated_returns_filter_with_must_clauses(self) -> None:
        from qdrant_client.models import Filter

        result = _qdrant_filter(jurisdictions=["CA-SK"], commodities=["Au"])
        assert isinstance(result, Filter)
        assert len(result.must) == 2

    def test_only_jurisdictions_returns_one_must_clause(self) -> None:
        from qdrant_client.models import Filter

        result = _qdrant_filter(jurisdictions=["CA-SK"], commodities=[])
        assert isinstance(result, Filter)
        assert len(result.must) == 1

    def test_only_commodities_returns_one_must_clause(self) -> None:
        from qdrant_client.models import Filter

        result = _qdrant_filter(jurisdictions=[], commodities=["U"])
        assert isinstance(result, Filter)
        assert len(result.must) == 1

    def test_match_any_values_are_set_correctly(self) -> None:
        from qdrant_client.models import Filter, MatchAny

        result = _qdrant_filter(jurisdictions=["CA-SK", "CA-BC"], commodities=["Au", "Cu"])
        assert isinstance(result, Filter)
        # Inspect the FieldConditions
        for cond in result.must:
            assert isinstance(cond.match, MatchAny)


# ---------------------------------------------------------------------------
# search_public_geoscience — graceful degradation
# ---------------------------------------------------------------------------


class TestSearchPublicGeoscienceGracefulDegradation:
    """Tests that search_public_geoscience returns safe empty results on
    missing or broken dependencies."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_embedding_model_is_none(self) -> None:
        """When deps.embedding_model is None the tool exits early with a
        descriptive data_source label rather than raising."""
        deps = _make_deps(embedding_model=None)
        ctx = _MockRunContext(deps=deps)

        result: PublicGeoscienceSearchResult = await search_public_geoscience(
            ctx,  # type: ignore[arg-type]
            text_query="gold occurrences in Saskatchewan",
        )

        assert result.count == 0
        assert result.records == []
        assert "not loaded" in result.data_source

    @pytest.mark.asyncio
    async def test_empty_canonical_types_after_filter_returns_empty(self) -> None:
        """When canonical_types only contains values outside the known set,
        the tool returns empty with a 'no types requested' data_source."""
        mock_model = MagicMock()
        mock_model.encode = MagicMock(
            return_value=MagicMock(tolist=lambda: [0.1] * 384)
        )
        deps = _make_deps(embedding_model=mock_model)
        ctx = _MockRunContext(deps=deps)

        result: PublicGeoscienceSearchResult = await search_public_geoscience(
            ctx,  # type: ignore[arg-type]
            canonical_types=["not_a_real_type", "another_fake_type"],
            text_query="something",
        )

        assert result.count == 0
        assert "no types requested" in result.data_source

    @pytest.mark.asyncio
    async def test_happy_path_with_mocked_qdrant_and_pg(self) -> None:
        """Full happy-path: Qdrant returns one point, PG hydrates jurisdiction +
        license + staleness. Verifies the assembled record has all fields set."""
        # Build a fake Qdrant point with a full payload.
        fake_point = MagicMock()
        fake_point.id = "qdrant-point-uuid-001"
        fake_point.score = 0.87
        fake_point.payload = {
            "pg_id": "pgeo-rec-001",
            "canonical_type": "mineral_occurrence",
            "jurisdiction_code": "CA-SK",
            "source_id": "sk-smdi",
            "source_feature_id": "SMDI-1234",
            "summary_text": "Moderate gold occurrence at Seabee Lake. Produced 5t Au.",
            "commodities": ["Au"],
            "commodity_grouping": "Precious Metals",
            "status": "Past Producer",
            "geom_bbox": [-107.0, 55.0, -106.0, 56.0],
            "source_url": "https://smdi.gov.sk.ca/1234",
        }

        mock_qdrant_response = MagicMock()
        mock_qdrant_response.points = [fake_point]

        mock_qdrant = AsyncMock()
        mock_qdrant.query_points = AsyncMock(return_value=mock_qdrant_response)

        # PG hydration returns jurisdiction_name + license metadata.
        fake_pg_row = {
            "source_id": "sk-smdi",
            "jurisdiction_code": "CA-SK",
            "license_summary": "Saskatchewan Open Government Licence",
            "license_url": "https://pubsaskdev.blob.core.windows.net/pubsask-prod/license.pdf",
            "last_refreshed_at": None,
            "staleness_seconds": 86400,
            "jurisdiction_name": "Saskatchewan",
        }

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[fake_pg_row])
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_model = MagicMock()
        mock_model.encode = MagicMock(
            return_value=MagicMock(tolist=lambda: [0.1] * 384)
        )

        deps = _make_deps(
            embedding_model=mock_model,
            qdrant_client=mock_qdrant,
            pg_pool=mock_pool,
        )
        ctx = _MockRunContext(deps=deps)

        result: PublicGeoscienceSearchResult = await search_public_geoscience(
            ctx,  # type: ignore[arg-type]
            jurisdiction_codes=["CA-SK"],
            canonical_types=["mineral_occurrence"],
            commodities=["Au"],
            text_query="gold occurrences",
        )

        assert result.count == 1
        rec: PublicGeoscienceRecord = result.records[0]
        assert rec.jurisdiction_name == "Saskatchewan"
        assert rec.license_summary == "Saskatchewan Open Government Licence"
        assert rec.staleness_seconds == 86400
        assert rec.relevance_score == pytest.approx(0.87)
        assert rec.pg_id == "pgeo-rec-001"
        assert rec.source_feature_id == "SMDI-1234"
