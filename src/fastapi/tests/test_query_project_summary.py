"""ADR-0007 PR-1 — query_project_summary tool tests.

Mocks asyncpg so the tests run without a live PostgreSQL. Verifies:

  * SQL parameter binding (workspace_id + project_id only)
  * Aggregation rows from all four source tables flow into one breakdown
  * ``extraction_pending_fields`` always lists contractor / geologist /
    lab_name (the 0%-populated columns per the 2026-05-25 schema audit)
  * Per-row ``source_row_ids`` are non-empty when the underlying rows
    carry IDs — satisfies §04i citation contract
  * Workspace_id IS in the WHERE clause of every aggregation query
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.deps import AgentDeps
from app.agent.tools import (
    ProjectSummaryResult,
    TechniqueBreakdownRow,
    query_project_summary,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_deps(pg_pool: object) -> AgentDeps:
    return AgentDeps(
        pg_pool=pg_pool,  # type: ignore[arg-type]
        qdrant_client=None,  # type: ignore[arg-type]
        neo4j_driver=None,  # type: ignore[arg-type]
        project_id="proj-test-uuid",
        embedding_model=None,
        reranker=None,
    )


@dataclass
class _SqlCall:
    sql: str
    args: tuple


def _make_pool_capturing(
    rows_by_table: dict[str, list[dict]],
) -> tuple[MagicMock, list[_SqlCall]]:
    """Build a mock pg_pool whose conn.fetch returns rows keyed by table name.

    The mock inspects the SQL string for the source table name and dispatches
    to the matching row list. Also captures every (sql, args) pair so the
    test can assert binding shape.
    """
    captured: list[_SqlCall] = []

    async def _fetch(sql: str, *args) -> list[dict]:
        captured.append(_SqlCall(sql=sql, args=args))
        if "FROM silver.campaigns" in sql:
            return rows_by_table.get("campaigns", [])
        if "FROM silver.collars" in sql:
            return rows_by_table.get("collars", [])
        if "FROM silver.geophysics_surveys" in sql:
            return rows_by_table.get("geophysics", [])
        if "FROM silver.reports" in sql:
            return rows_by_table.get("reports", [])
        return []

    async def _fetchrow(sql: str, *args):
        """Default fetchrow: None (= field unpopulated).

        The ADR-0007 PR-3 pending-fields probe issues per-column LIMIT 1
        SELECTs; in the default test fixtures none of the 0%-populated
        columns have any rows, so every probe returns None and every
        candidate stays on the pending list.
        """
        captured.append(_SqlCall(sql=sql, args=args))
        return None

    mock_conn = AsyncMock()
    mock_conn.fetch = _fetch
    mock_conn.fetchrow = _fetchrow
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_pool, captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestQueryProjectSummary:
    """Tests for query_project_summary."""

    @pytest.mark.asyncio
    async def test_aggregates_rows_from_all_four_tables(self) -> None:
        """Each silver table's rows flow into the unified breakdown list."""
        pool, _captured = _make_pool_capturing({
            "campaigns": [
                {
                    "technique": "DDH",
                    "year": 2022,
                    "n": 3,
                    "total_metres": 1500.0,
                    "contractor": None,  # 0%-populated audit
                    "geologist": None,
                    "source_row_ids": [
                        "00000000-0000-0000-0000-000000000001",
                        "00000000-0000-0000-0000-000000000002",
                    ],
                }
            ],
            "collars": [
                {
                    "technique": "RC",
                    "year": 2023,
                    "n": 5,
                    "total_metres": 750.0,
                    "source_row_ids": [
                        "00000000-0000-0000-0000-00000000000a"
                    ],
                }
            ],
            "geophysics": [
                {
                    "technique": "magnetic",
                    "year": 2021,
                    "n": 1,
                    "source_row_ids": [
                        "00000000-0000-0000-0000-00000000000b"
                    ],
                }
            ],
            "reports": [
                {
                    "technique": "pdfplumber",
                    "year": 2020,
                    "n": 12,
                    "source_row_ids": [
                        "00000000-0000-0000-0000-00000000000c"
                    ],
                }
            ],
        })
        deps = _make_deps(pg_pool=pool)
        result = await query_project_summary(
            deps,
            workspace_id="a0000000-0000-0000-0000-000000000001",
            project_id="proj-test-uuid",
        )

        assert isinstance(result, ProjectSummaryResult)
        assert result.count == 4
        techniques = {(r.technique, r.source_table) for r in result.technique_breakdown}
        assert ("DDH", "silver.campaigns") in techniques
        assert ("RC", "silver.collars") in techniques
        assert ("magnetic", "silver.geophysics_surveys") in techniques
        assert ("pdfplumber", "silver.reports") in techniques

    @pytest.mark.asyncio
    async def test_extraction_pending_fields_always_listed(self) -> None:
        """contractor / geologist / lab_name are always reported as pending
        until ADR-0007 PR-3 backfills them."""
        pool, _captured = _make_pool_capturing({})
        deps = _make_deps(pg_pool=pool)
        result = await query_project_summary(
            deps,
            workspace_id="a0000000-0000-0000-0000-000000000001",
            project_id="proj-test-uuid",
        )
        assert "contractor" in result.extraction_pending_fields
        assert "geologist" in result.extraction_pending_fields
        assert "lab_name" in result.extraction_pending_fields

    @pytest.mark.asyncio
    async def test_workspace_id_in_every_query(self) -> None:
        """§04i + tenancy: workspace_id must appear in every WHERE clause."""
        pool, captured = _make_pool_capturing({})
        deps = _make_deps(pg_pool=pool)
        await query_project_summary(
            deps,
            workspace_id="a0000000-0000-0000-0000-000000000001",
            project_id="proj-test-uuid",
        )
        assert captured, "no SQL was issued"
        for call in captured:
            assert "p.workspace_id = $1" in call.sql, (
                f"workspace_id missing from query: {call.sql[:120]}…"
            )
            assert call.args[0] == "a0000000-0000-0000-0000-000000000001"
            assert call.args[1] == "proj-test-uuid"

    @pytest.mark.asyncio
    async def test_source_row_ids_propagate_for_citations(self) -> None:
        """Each breakdown row carries source_row_ids — required by §04i."""
        pool, _captured = _make_pool_capturing({
            "campaigns": [
                {
                    "technique": "DDH",
                    "year": 2022,
                    "n": 1,
                    "total_metres": 100.0,
                    "contractor": None,
                    "geologist": None,
                    "source_row_ids": ["aaaa-bbbb"],
                }
            ],
        })
        deps = _make_deps(pg_pool=pool)
        result = await query_project_summary(
            deps,
            workspace_id="a0000000-0000-0000-0000-000000000001",
            project_id="proj-test-uuid",
        )
        assert result.technique_breakdown
        row: TechniqueBreakdownRow = result.technique_breakdown[0]
        assert row.source_row_ids == ["aaaa-bbbb"]

    @pytest.mark.asyncio
    async def test_returns_empty_on_db_error(self) -> None:
        """Database failure → empty result, NOT a raise."""
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(side_effect=RuntimeError("DB down"))
        # ADR-0007 PR-3: pending-fields probe also issues fetchrow calls.
        # We want the same error path → _compute_pending_fields catches and
        # returns the full candidate list verbatim.
        mock_conn.fetchrow = AsyncMock(side_effect=RuntimeError("DB down"))
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        deps = _make_deps(pg_pool=mock_pool)

        result = await query_project_summary(
            deps,
            workspace_id="a0000000-0000-0000-0000-000000000001",
            project_id="proj-test-uuid",
        )
        assert result.count == 0
        assert result.technique_breakdown == []
        # extraction_pending_fields STILL populated even on error so the
        # answer can call out the gap honestly.
        assert "contractor" in result.extraction_pending_fields

    @pytest.mark.asyncio
    async def test_returns_empty_when_pool_is_none(self) -> None:
        """deps.pg_pool=None short-circuits gracefully."""
        deps = _make_deps(pg_pool=None)
        result = await query_project_summary(
            deps,
            workspace_id="a0000000-0000-0000-0000-000000000001",
            project_id="proj-test-uuid",
        )
        assert result.count == 0
        assert result.project_id == "proj-test-uuid"
