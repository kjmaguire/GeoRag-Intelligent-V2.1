"""ADR-0007 PR-1 — §04i citation contract for project_summary + coverage_gap.

Asserts that both new tools return result objects whose per-row data can
be bound to a Citation by the assembler. Specifically:

  * Every TechniqueBreakdownRow exposes ``source_row_ids: list[str]``
  * Every AttributeCoverageRow exposes ``source_row_ids: list[str]``
  * Every CoverageFindingRow exposes ``source_row_ids: list[str]``
  * The response_assembler's ``_extract_source_id`` returns a non-empty
    citation source ID for both result types — Pydantic AI's typed output
    validation (Layer 2) rejects empty source_chunk_id, so this is the
    hard contract we test here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.deps import AgentDeps
from app.agent.response_assembler import (
    _extract_document_title,
    _extract_relevance,
    _extract_source_id,
)
from app.agent.tools import (
    AttributeCoverageRow,
    CoverageFindingRow,
    CoverageGapResult,
    IngestGapStats,
    ProjectSummaryResult,
    TechniqueBreakdownRow,
    query_coverage_gap,
    query_project_summary,
)


def _make_deps(pool: object) -> AgentDeps:
    return AgentDeps(
        pg_pool=pool,  # type: ignore[arg-type]
        qdrant_client=None,  # type: ignore[arg-type]
        neo4j_driver=None,  # type: ignore[arg-type]
        project_id="proj-test-uuid",
        embedding_model=None,
        reranker=None,
    )


def _pool_with(rows_by_pattern: dict[str, list[dict]]) -> MagicMock:
    async def _fetch(sql, *args):
        for pattern, rows in rows_by_pattern.items():
            if pattern in sql:
                return rows
        return []

    async def _fetchrow(sql, *args):
        for pattern, rows in rows_by_pattern.items():
            if pattern in sql and rows:
                return rows[0]
        return None

    conn = AsyncMock()
    conn.fetch = _fetch
    conn.fetchrow = _fetchrow
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


@pytest.mark.asyncio
async def test_project_summary_rows_carry_source_row_ids() -> None:
    """Every TechniqueBreakdownRow has a non-empty source_row_ids list."""
    pool = _pool_with({
        "FROM silver.campaigns": [
            {
                "technique": "DDH",
                "year": 2022,
                "n": 2,
                "total_metres": 200.0,
                "contractor": None,
                "geologist": None,
                "source_row_ids": ["camp-1", "camp-2"],
            }
        ],
        "FROM silver.collars": [
            {
                "technique": "RC",
                "year": 2023,
                "n": 1,
                "total_metres": 100.0,
                "source_row_ids": ["col-1"],
            }
        ],
    })
    deps = _make_deps(pool=pool)
    result: ProjectSummaryResult = await query_project_summary(
        deps,
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id="proj-test-uuid",
    )
    assert result.technique_breakdown
    for row in result.technique_breakdown:
        assert isinstance(row, TechniqueBreakdownRow)
        # §04i: every claim binds to source rows.
        assert row.source_row_ids, (
            f"row {row.technique}/{row.source_table} missing source_row_ids"
        )


@pytest.mark.asyncio
async def test_coverage_gap_rows_carry_source_row_ids() -> None:
    """Every AttributeCoverageRow + CoverageFindingRow has source_row_ids."""
    async def _fetchrow(sql, *args):
        if "WITH indexed AS" in sql:
            return {"indexed_n": 100, "processed_n": 5}
        if "FROM silver.collars co" in sql and " AS n" in sql:
            return {"n": 4}
        if "JOIN silver.assays_v2" in sql:
            return {"n": 2, "source_row_ids": ["col-a", "col-b"]}
        return {"n": 0, "source_row_ids": []}

    async def _fetch(sql, *args):
        if "FROM silver.completeness_findings" in sql:
            return [
                {
                    "kind": "missing_qc",
                    "severity": "warn",
                    "description": "Missing QA.",
                    "finding_id": "finding-1",
                }
            ]
        return []

    conn = AsyncMock()
    conn.fetchrow = _fetchrow
    conn.fetch = _fetch
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    deps = _make_deps(pool=pool)

    result: CoverageGapResult = await query_coverage_gap(
        deps,
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id="proj-test-uuid",
    )

    # assays attribute should have source rows
    attr_lookup = {row.attribute: row for row in result.attribute_coverage}
    if "assays" in attr_lookup:
        row: AttributeCoverageRow = attr_lookup["assays"]
        # the row reports 2 collars with data → source_row_ids must be non-empty
        assert row.source_row_ids, (
            "assays attribute row missing source_row_ids — citation chain broken"
        )

    # findings carry their own source_row_ids
    for finding in result.findings:
        assert isinstance(finding, CoverageFindingRow)
        assert finding.source_row_ids


def test_assembler_extracts_non_empty_source_id_for_project_summary() -> None:
    """response_assembler._extract_source_id must return non-empty for §04i."""
    result = ProjectSummaryResult(
        technique_breakdown=[
            TechniqueBreakdownRow(
                technique="DDH",
                source_table="silver.campaigns",
                year=2022,
                count=1,
                total_metres=100.0,
                contractor=None,
                geologist=None,
                source_row_ids=["camp-1"],
            )
        ],
        extraction_pending_fields=["contractor", "geologist", "lab_name"],
        project_id="proj-test-uuid",
        workspace_id="ws-test-uuid",
        count=1,
    )
    cid = _extract_source_id("query_project_summary", result)
    assert cid, "source_chunk_id must be non-empty (Pydantic AI Layer 2)"
    assert "proj-test-uuid" in cid
    assert "rows=1" in cid

    # Document title + relevance must also be sensible.
    title = _extract_document_title("query_project_summary", result)
    assert title and "breakdown" in title.lower()
    assert _extract_relevance(result) == 1.0


def test_assembler_extracts_non_empty_source_id_for_coverage_gap() -> None:
    result = CoverageGapResult(
        ingest_gap=IngestGapStats(indexed=100, processed=10, gap_pct=90.0),
        attribute_coverage=[
            AttributeCoverageRow(
                attribute="assays",
                collars_with_data=5,
                collars_total=10,
                coverage_pct=50.0,
                source_row_ids=["col-1"],
            )
        ],
        findings=[],
        project_id="proj-test-uuid",
        workspace_id="ws-test-uuid",
        count=2,
    )
    cid = _extract_source_id("query_coverage_gap", result)
    assert cid, "source_chunk_id must be non-empty (Pydantic AI Layer 2)"
    assert "proj-test-uuid" in cid
    title = _extract_document_title("query_coverage_gap", result)
    assert title and "coverage" in title.lower()
    assert _extract_relevance(result) == 1.0
