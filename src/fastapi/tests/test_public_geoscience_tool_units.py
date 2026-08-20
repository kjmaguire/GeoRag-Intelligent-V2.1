"""Unit tests for the Public Geoscience retrieval tool.

Covers the pure helpers, the generated SQL, and the graceful-degradation
paths in ``app/agent/public_geoscience_tool.py``. No live services and no
database — the pool is mocked.

Rewritten 2026-08-20. The previous version tested the Qdrant-era helpers
(``_qdrant_filter``, ``_passes_bbox``, ``_maybe_bbox``, ``_derive_name``),
which existed to post-filter vector hits in Python. The tool now reads
``public_geo.*``, where the database does the filtering, so those helpers are
gone and the coverage moved with them:

    _qdrant_filter  -> the generated WHERE clause (TestGeneratedQuery)
    _passes_bbox    -> `geom && ST_MakeEnvelope(...)`, tested as SQL shape
    _maybe_bbox     -> `ST_XMin(geom)…` in the projection
    _derive_name    -> _NAME_EXPR per type, plus _to_record's fallback

Run with:
    pytest tests/test_public_geoscience_tool_units.py -v
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.deps import AgentDeps
from app.agent.public_geoscience_tool import (
    _NAME_EXPR,
    _TABLES,
    PublicGeoscienceRecord,
    PublicGeoscienceSearchResult,
    _build_query,
    _commodity_tokens,
    _normalize_bbox,
    _normalize_strings,
    _staleness,
    _to_record,
    search_public_geoscience,
)
from app.services.public_geo.registry import CANONICAL_TYPES

# ---------------------------------------------------------------------------
# Test context shim (matches _MockRunContext in test_agent_tools.py)
# ---------------------------------------------------------------------------


@dataclass
class _MockRunContext:
    """Minimal stand-in for pydantic_ai.RunContext[AgentDeps]."""

    deps: AgentDeps


def _make_deps(*, pg_pool: object = None) -> AgentDeps:
    """Build a minimal AgentDeps for unit tests."""
    return AgentDeps(
        pg_pool=pg_pool,  # type: ignore[arg-type]
        qdrant_client=MagicMock(),  # type: ignore[arg-type]
        neo4j_driver=MagicMock(),  # type: ignore[arg-type]
        project_id="test-project-uuid",
        embedding_model=None,
    )


def _mock_pool(rows: list[dict[str, Any]]) -> MagicMock:
    """A pg_pool whose one connection returns `rows` from fetch()."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    pool._conn = conn  # exposed so tests can assert on the bound arguments
    return pool


def _row(**overrides: Any) -> dict[str, Any]:
    """A plausible result row; asyncpg Records are Mapping-like."""
    base = {
        "canonical_type": "mine",
        "jurisdiction_code": "CA-SK",
        "source_id": "CA-SK-MINE-LOC",
        "source_feature_id": "803",
        "name": "Collins Bay B Uranium Deposit",
        "commodities": ["U"],
        "commodity_grouping": "uranium",
        "status": "past-producer",
        "last_seen_at": datetime.now(UTC),
        "bbox": [-103.647, 58.262, -103.647, 58.262],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _normalize_strings
# ---------------------------------------------------------------------------


class TestNormalizeStrings:
    def test_none_returns_empty_list(self) -> None:
        assert _normalize_strings(None) == []

    def test_empty_list_returns_empty_list(self) -> None:
        assert _normalize_strings([]) == []

    def test_whitespace_only_strings_are_filtered(self) -> None:
        assert _normalize_strings(["CA-SK", "  ", "\t", "CA-BC"]) == ["CA-SK", "CA-BC"]

    def test_non_str_values_are_coerced_to_string(self) -> None:
        result = _normalize_strings([1, None, "Au"])  # type: ignore[list-item]
        assert result == ["1", "Au"]

    def test_strips_surrounding_whitespace(self) -> None:
        assert _normalize_strings(["  CA-SK  ", " Au "]) == ["CA-SK", "Au"]


# ---------------------------------------------------------------------------
# _normalize_bbox
# ---------------------------------------------------------------------------


class TestNormalizeBbox:
    def test_none_returns_none(self) -> None:
        assert _normalize_bbox(None) is None

    def test_four_element_list_returns_tuple_of_floats(self) -> None:
        result = _normalize_bbox([1, 2, 3, 4])
        assert result == (1.0, 2.0, 3.0, 4.0)
        assert isinstance(result[0], float)

    def test_wrong_length_returns_none(self) -> None:
        assert _normalize_bbox([1, 2, 3]) is None

    def test_non_numeric_returns_none(self) -> None:
        assert _normalize_bbox(["a", "b", "c", "d"]) is None

    def test_tuple_input_accepted(self) -> None:
        assert _normalize_bbox((10.0, 20.0, 30.0, 40.0)) == (10.0, 20.0, 30.0, 40.0)


# ---------------------------------------------------------------------------
# _commodity_tokens
# ---------------------------------------------------------------------------


class TestCommodityTokens:
    """The substring trap this replaced is worth a regression test.

    `LIKE '%U%'` matched "Ind*u*strial Mineral" and returned 24 of 25
    Saskatchewan mines. The SQL now compares whole values and whole words, and
    this helper is what supplies both forms.
    """

    def test_lowercases_the_whole_value(self) -> None:
        assert "uranium" in _commodity_tokens(["Uranium"])

    def test_multiword_yields_phrase_and_words(self) -> None:
        tokens = _commodity_tokens(["Rare Earth Elements"])
        assert "rare earth elements" in tokens
        assert {"rare", "earth", "elements"} <= set(tokens)

    def test_single_letter_code_is_kept_intact(self) -> None:
        # "u" must survive as its own token so `commodities = {U}` matches …
        assert _commodity_tokens(["U"]) == ["u"]

    def test_no_substring_fragments_are_produced(self) -> None:
        # … and must NOT be produced as a fragment of another word, which is
        # what would resurrect the Industrial-matches-U bug.
        assert "u" not in _commodity_tokens(["Industrial Mineral"])

    def test_deduplicates(self) -> None:
        assert _commodity_tokens(["Gold", "gold", "GOLD"]) == ["gold"]

    def test_empty_input(self) -> None:
        assert _commodity_tokens(None) == []
        assert _commodity_tokens([]) == []


# ---------------------------------------------------------------------------
# _staleness
# ---------------------------------------------------------------------------


class TestStaleness:
    """staleness_seconds is now real data, not a hardcoded zero."""

    def test_none_last_seen_returns_none(self) -> None:
        assert _staleness(None) is None

    def test_recent_timestamp_is_near_zero(self) -> None:
        assert _staleness(datetime.now(UTC)) < 5

    def test_old_timestamp_reports_the_gap(self) -> None:
        seen = datetime.now(UTC) - timedelta(days=7)
        assert 7 * 86400 - 60 < _staleness(seen) < 7 * 86400 + 60

    def test_naive_timestamp_is_treated_as_utc(self) -> None:
        """A naive datetime must not raise or produce a negative age."""
        naive = datetime.now(UTC).replace(tzinfo=None)
        assert _staleness(naive) is not None
        assert _staleness(naive) >= 0

    def test_future_timestamp_clamps_to_zero(self) -> None:
        future = datetime.now(UTC) + timedelta(hours=1)
        assert _staleness(future) == 0


# ---------------------------------------------------------------------------
# _build_query
# ---------------------------------------------------------------------------


class TestGeneratedQuery:
    """The SQL is generated, so its shape is what needs asserting."""

    def test_one_branch_per_requested_type(self) -> None:
        sql = _build_query(["mine", "rock_sample"])
        assert sql.count("UNION ALL") == 1
        assert _TABLES["mine"] in sql
        assert _TABLES["rock_sample"] in sql

    def test_single_type_has_no_union(self) -> None:
        assert "UNION ALL" not in _build_query(["mine"])

    def test_every_canonical_type_generates(self) -> None:
        """A type in the registry with no branch would silently return zero."""
        sql = _build_query(list(CANONICAL_TYPES))
        for t in CANONICAL_TYPES:
            assert _TABLES[t] in sql

    def test_limit_is_applied_per_branch_not_once_overall(self) -> None:
        """A single outer LIMIT would let one big type crowd out the rest."""
        sql = _build_query(["mine", "rock_sample", "assessment_survey"])
        assert sql.count("LIMIT $8::int") == 3

    def test_every_branch_binds_the_same_eight_parameters(self) -> None:
        """The union is bound once, so a branch that skipped a parameter
        would raise 'bind message supplies 8 parameters'."""
        for t in CANONICAL_TYPES:
            branch = _build_query([t])
            for n in range(1, 9):
                assert f"${n}" in branch, f"{t} branch never references ${n}"

    def test_bbox_uses_the_indexed_operator(self) -> None:
        """`&&` hits the GIST index; ST_Intersects alone would not."""
        sql = _build_query(["mine"])
        assert "geom && ST_MakeEnvelope($4, $5, $6, $7, 4326)" in sql

    def test_commodity_match_is_not_a_substring_test(self) -> None:
        sql = _build_query(["mine"])
        assert "unnest(" in sql
        assert "LIKE '%' || $3" not in sql

    def test_name_filter_uses_the_same_expression_as_the_projection(self) -> None:
        """The WHERE runs before the output alias exists, so both sites must
        use the raw expression — aliasing one and not the other silently
        filters on a different column."""
        for t in CANONICAL_TYPES:
            branch = _build_query([t])
            assert f"{_NAME_EXPR[t]} ILIKE" in branch

    def test_results_are_ordered_by_freshness(self) -> None:
        assert "ORDER BY last_seen_at DESC" in _build_query(["mine"])


# ---------------------------------------------------------------------------
# _to_record
# ---------------------------------------------------------------------------


class TestToRecord:
    def test_maps_a_full_row(self) -> None:
        rec = _to_record(_row())
        assert rec.canonical_type == "mine"
        assert rec.name == "Collins Bay B Uranium Deposit"
        assert rec.commodities == ["U"]
        assert rec.status == "past-producer"
        assert rec.geom_bbox == [-103.647, 58.262, -103.647, 58.262]

    def test_pg_id_is_the_upstream_composite(self) -> None:
        """Not our UUID: a citation must stay resolvable against the survey
        itself even if the row is rebuilt by a later sync."""
        assert _to_record(_row()).pg_id == "CA-SK-MINE-LOC:803"

    def test_licence_is_resolved_from_the_code_registry(self) -> None:
        """Attribution is a licence obligation, so it must not depend on a
        join the query deliberately skips."""
        rec = _to_record(_row())
        assert rec.jurisdiction_name == "Saskatchewan"
        assert rec.license_summary
        assert rec.license_url

    def test_unknown_source_id_degrades_without_raising(self) -> None:
        rec = _to_record(_row(source_id="CA-XX-NOT-REGISTERED"))
        assert rec.jurisdiction_name is None
        assert rec.license_summary is None
        assert rec.source_url is None

    def test_missing_name_falls_back_to_type_and_feature_id(self) -> None:
        """Replaces the old _derive_name coverage. Rock samples and surveys
        routinely have no name at all."""
        rec = _to_record(_row(canonical_type="rock_sample", name=None))
        assert "Rock Sample" in rec.name
        assert "803" in rec.name

    def test_null_geometry_yields_no_bbox(self) -> None:
        assert _to_record(_row(bbox=None)).geom_bbox is None

    def test_relevance_score_is_always_zero(self) -> None:
        """Nothing here is semantically ranked; a non-zero value would be
        rendered as confidence it does not have."""
        assert _to_record(_row()).relevance_score == 0.0

    def test_commodities_are_capped(self) -> None:
        rec = _to_record(_row(commodities=[f"C{i}" for i in range(30)]))
        assert len(rec.commodities) == 12

    def test_null_commodity_entries_are_dropped(self) -> None:
        rec = _to_record(_row(commodities=["Au", None, "", "Ag"]))
        assert rec.commodities == ["Au", "Ag"]

    def test_summary_text_carries_the_facts_the_llm_reads(self) -> None:
        summary = _to_record(_row()).summary_text
        assert "Collins Bay B Uranium Deposit" in summary
        assert "Saskatchewan" in summary
        assert "past-producer" in summary


# ---------------------------------------------------------------------------
# search_public_geoscience — graceful degradation + happy path
# ---------------------------------------------------------------------------


class TestSearchPublicGeoscience:
    @pytest.mark.asyncio
    async def test_returns_empty_when_pool_is_none(self) -> None:
        """Matches the convention in app.agent.tools: degrade, never raise."""
        ctx = _MockRunContext(deps=_make_deps(pg_pool=None))
        result = await search_public_geoscience(ctx, text_query="gold")  # type: ignore[arg-type]
        assert result.count == 0
        assert result.records == []

    @pytest.mark.asyncio
    async def test_unknown_canonical_types_return_empty_without_querying(self) -> None:
        pool = _mock_pool([])
        ctx = _MockRunContext(deps=_make_deps(pg_pool=pool))
        result = await search_public_geoscience(
            ctx,  # type: ignore[arg-type]
            canonical_types=["not_a_real_type"],
            text_query="something",
        )
        assert result.count == 0
        pool._conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_database_error_degrades_to_empty(self) -> None:
        pool = _mock_pool([])
        pool._conn.fetch = AsyncMock(side_effect=RuntimeError("connection reset"))
        ctx = _MockRunContext(deps=_make_deps(pg_pool=pool))
        result = await search_public_geoscience(ctx)  # type: ignore[arg-type]
        assert result.count == 0
        assert result.records == []

    @pytest.mark.asyncio
    async def test_happy_path_assembles_records(self) -> None:
        pool = _mock_pool([_row(), _row(source_feature_id="899", name="Key Lake")])
        ctx = _MockRunContext(deps=_make_deps(pg_pool=pool))

        result: PublicGeoscienceSearchResult = await search_public_geoscience(
            ctx,  # type: ignore[arg-type]
            jurisdiction_codes=["CA-SK"],
            canonical_types=["mine"],
            commodities=["U"],
        )

        assert result.count == 2
        rec: PublicGeoscienceRecord = result.records[0]
        assert rec.jurisdiction_name == "Saskatchewan"
        assert rec.pg_id == "CA-SK-MINE-LOC:803"
        assert rec.staleness_seconds is not None
        assert "public_geo" in result.data_source

    @pytest.mark.asyncio
    async def test_arguments_are_bound_not_interpolated(self) -> None:
        """SQL injection guard: user text must arrive as a bind parameter."""
        pool = _mock_pool([])
        ctx = _MockRunContext(deps=_make_deps(pg_pool=pool))
        await search_public_geoscience(  # type: ignore[arg-type]
            ctx, canonical_types=["mine"], text_query="'; DROP TABLE pg_mine; --",
        )
        sql, *args = pool._conn.fetch.await_args.args
        assert "DROP TABLE" not in sql
        assert args[1] == "'; DROP TABLE pg_mine; --"

    @pytest.mark.asyncio
    async def test_limit_per_type_is_capped(self) -> None:
        """A bad caller must not be able to flood the prompt."""
        pool = _mock_pool([])
        ctx = _MockRunContext(deps=_make_deps(pg_pool=pool))
        await search_public_geoscience(ctx, canonical_types=["mine"], limit_per_type=9999)  # type: ignore[arg-type]
        _sql, *args = pool._conn.fetch.await_args.args
        assert args[7] == 25

    @pytest.mark.asyncio
    async def test_empty_filters_bind_as_null_not_empty_list(self) -> None:
        """`= ANY('{}')` matches nothing; NULL is what disables the filter."""
        pool = _mock_pool([])
        ctx = _MockRunContext(deps=_make_deps(pg_pool=pool))
        await search_public_geoscience(ctx, canonical_types=["mine"])  # type: ignore[arg-type]
        _sql, *args = pool._conn.fetch.await_args.args
        assert args[0] is None  # jurisdictions
        assert args[1] is None  # text
        assert args[2] is None  # commodities
        assert args[3] is None  # bbox minLon
