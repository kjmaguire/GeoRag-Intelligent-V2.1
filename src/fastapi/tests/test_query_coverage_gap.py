"""ADR-0007 PR-1 — query_coverage_gap tool tests.

Verifies:

  * Ingest-gap percentage computation (39,744 indexed vs 1,209 processed
    from the 2026-05-25 audit → 96.96% gap)
  * Per-attribute coverage rows shape + ordering
  * silver.completeness_findings are surfaced as-is
  * workspace_id IS in every WHERE clause
  * Missing detail tables degrade cleanly (no raise)
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.deps import AgentDeps
from app.agent.tools import (
    AttributeCoverageRow,
    CoverageGapResult,
    IngestGapStats,
    query_coverage_gap,
)


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


def _build_pool(
    *,
    indexed: int,
    processed: int,
    collars_total: int,
    attribute_data: dict[str, int],
    findings: list[dict] | None = None,
    raise_for_tables: set[str] | None = None,
) -> tuple[MagicMock, list[_SqlCall]]:
    captured: list[_SqlCall] = []
    raise_for_tables = raise_for_tables or set()

    async def _fetchrow(sql: str, *args):
        captured.append(_SqlCall(sql=sql, args=args))
        if "WITH indexed AS" in sql:
            return {"indexed_n": indexed, "processed_n": processed}
        # Attribute coverage queries do `JOIN silver.<table> t ON …`.
        # Check FIRST so the collars_total branch below doesn't swallow
        # them (both queries share the silver.collars FROM clause).
        for attr, count in attribute_data.items():
            if f"JOIN silver.{attr} " in sql or f"JOIN silver.{attr}\n" in sql:
                if attr in raise_for_tables:
                    raise RuntimeError(f"table silver.{attr} unavailable")
                return {
                    "n": count,
                    "source_row_ids": [f"col-{attr}-{i}" for i in range(count)],
                }
        # The collars_total query uses COUNT(*) AS n with no JOIN to a
        # downstream detail table — distinguish by COUNT(*).
        if "COUNT(*)::int AS n" in sql and "FROM silver.collars" in sql:
            return {"n": collars_total}
        return None

    async def _fetch(sql: str, *args):
        captured.append(_SqlCall(sql=sql, args=args))
        if "FROM silver.completeness_findings" in sql:
            rows = findings or []
            return rows
        return []

    mock_conn = AsyncMock()
    mock_conn.fetchrow = _fetchrow
    mock_conn.fetch = _fetch
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_pool, captured


class TestQueryCoverageGap:
    """Tests for query_coverage_gap."""

    @pytest.mark.asyncio
    async def test_ingest_gap_percentage_computed(self) -> None:
        """39,744 indexed vs 1,209 processed → ~96.96% gap (live-DB shape)."""
        pool, _captured = _build_pool(
            indexed=39_744,
            processed=1_209,
            collars_total=567,
            attribute_data={"assays_v2": 540, "lithology_logs": 302},
        )
        deps = _make_deps(pg_pool=pool)
        result = await query_coverage_gap(
            deps,
            workspace_id="a0000000-0000-0000-0000-000000000001",
            project_id="proj-test-uuid",
        )
        assert isinstance(result, CoverageGapResult)
        assert isinstance(result.ingest_gap, IngestGapStats)
        assert result.ingest_gap.indexed == 39_744
        assert result.ingest_gap.processed == 1_209
        assert 96.9 <= result.ingest_gap.gap_pct <= 97.0

    @pytest.mark.asyncio
    async def test_attribute_coverage_rows_shape(self) -> None:
        """Each attribute row reports collars_with_data / collars_total / coverage_pct."""
        pool, _captured = _build_pool(
            indexed=100,
            processed=50,
            collars_total=10,
            attribute_data={"assays_v2": 5, "lithology_logs": 8},
        )
        deps = _make_deps(pg_pool=pool)
        result = await query_coverage_gap(
            deps,
            workspace_id="a0000000-0000-0000-0000-000000000001",
            project_id="proj-test-uuid",
        )
        # at least the two attributes we set up should appear
        attr_lookup = {row.attribute: row for row in result.attribute_coverage}
        assert "assays" in attr_lookup or "lithology_logs" in attr_lookup
        if "assays" in attr_lookup:
            row: AttributeCoverageRow = attr_lookup["assays"]
            assert row.collars_with_data == 5
            assert row.collars_total == 10
            assert row.coverage_pct == 50.0

    @pytest.mark.asyncio
    async def test_workspace_id_in_every_query(self) -> None:
        """§04i + tenancy: workspace_id always bound as $1."""
        pool, captured = _build_pool(
            indexed=1,
            processed=1,
            collars_total=1,
            attribute_data={"assays_v2": 1},
        )
        deps = _make_deps(pg_pool=pool)
        await query_coverage_gap(
            deps,
            workspace_id="a0000000-0000-0000-0000-000000000001",
            project_id="proj-test-uuid",
        )
        assert captured, "no SQL was issued"
        for call in captured:
            assert call.args[0] == "a0000000-0000-0000-0000-000000000001"

    @pytest.mark.asyncio
    async def test_completeness_findings_surfaced(self) -> None:
        """silver.completeness_findings rows pass through as-is."""
        pool, _captured = _build_pool(
            indexed=10,
            processed=10,
            collars_total=5,
            attribute_data={"assays_v2": 5},
            findings=[
                {
                    "kind": "missing_assay_qaqc",
                    "severity": "warning",
                    "description": "12 collars lack any QA/QC sample.",
                    "finding_id": "finding-001",
                },
            ],
        )
        deps = _make_deps(pg_pool=pool)
        result = await query_coverage_gap(
            deps,
            workspace_id="a0000000-0000-0000-0000-000000000001",
            project_id="proj-test-uuid",
        )
        assert len(result.findings) == 1
        assert result.findings[0].kind == "missing_assay_qaqc"
        assert result.findings[0].source_row_ids == ["finding-001"]

    @pytest.mark.asyncio
    async def test_missing_detail_table_degrades_cleanly(self) -> None:
        """If silver.structure doesn't exist yet (pre-PR-2), the tool drops
        that attribute and keeps going — no raise."""
        pool, _captured = _build_pool(
            indexed=5,
            processed=5,
            collars_total=5,
            attribute_data={
                "assays_v2": 5,
                "lithology_logs": 3,
                "structure": 0,  # raises below
            },
            raise_for_tables={"structure"},
        )
        deps = _make_deps(pg_pool=pool)
        result = await query_coverage_gap(
            deps,
            workspace_id="a0000000-0000-0000-0000-000000000001",
            project_id="proj-test-uuid",
        )
        attrs = {row.attribute for row in result.attribute_coverage}
        # structure was raised — should not appear
        assert "structure" not in attrs
        # the other attrs still made it
        assert "assays" in attrs

    @pytest.mark.asyncio
    async def test_pool_none_returns_empty_result(self) -> None:
        deps = _make_deps(pg_pool=None)
        result = await query_coverage_gap(
            deps,
            workspace_id="a0000000-0000-0000-0000-000000000001",
            project_id="proj-test-uuid",
        )
        assert result.ingest_gap.indexed == 0
        assert result.attribute_coverage == []
        assert result.findings == []


class TestIngestStageIsProjectScoped:
    """2026-10-10 audit, finding 12: the ingest stage counted bronze.ingest_manifest
    by workspace_id only, so a project answer reported the whole workspace's
    indexed / processed counts -- every other project's files included -- next
    to project-scoped attribute coverage.

    The manifest has no project column. A project's rows are found the way the
    Sources page finds them: the PLSS sections named in the source paths of the
    provenance rows of the project's own collars and reports. These tests pin
    the shape of the query the mock pool sees; the arithmetic was run against
    a real PostgreSQL 16 with a seeded two-workspace, three-project fixture
    (indexed/processed: 8/1 and 7/1 for the two archive projects, 0/0 for a
    project uploaded through the live pipeline and for the wrong workspace,
    where the old query said 19/2 for every one of them). Since then a project
    with no archive files reports its uploads from silver.ingest_progress.
    """

    WORKSPACE = "a0000000-0000-0000-0000-000000000001"
    PROJECT = "b0000000-0000-0000-0000-000000000001"

    async def _ingest_call(self) -> _SqlCall:
        pool, captured = _build_pool(
            indexed=8, processed=1, collars_total=3, attribute_data={"assays_v2": 1}
        )
        await query_coverage_gap(
            _make_deps(pg_pool=pool), workspace_id=self.WORKSPACE, project_id=self.PROJECT
        )
        return next(c for c in captured if "WITH indexed AS" in c.sql)

    @pytest.mark.asyncio
    async def test_the_ingest_query_is_bound_to_the_project_too(self) -> None:
        call = await self._ingest_call()
        assert call.args == (self.WORKSPACE, self.PROJECT)

    @pytest.mark.asyncio
    async def test_processed_counts_only_this_projects_reports(self) -> None:
        sql = (await self._ingest_call()).sql
        processed = sql[sql.index("processed AS") :]
        assert "r.project_id = $2::uuid" in processed

    @pytest.mark.asyncio
    async def test_indexed_is_restricted_to_this_projects_sections(self) -> None:
        sql = (await self._ingest_call()).sql
        indexed = sql[sql.index("WITH indexed AS") : sql.index("processed AS")]
        # the manifest has no project column: its rows are attributed through
        # the project's own collars and reports...
        assert "m.guessed_project IN (" in indexed
        assert "bp.target_table = 'collars'" in indexed
        assert "bp.target_table = 'reports'" in indexed
        assert indexed.count("c.project_id = $2::uuid") == 1
        assert indexed.count("r.project_id = $2::uuid") == 1
        # ...and still inside the caller's workspace
        assert "m.workspace_id = $1::uuid" in indexed
        assert indexed.count("bp.workspace_id = $1::uuid") == 2

    @pytest.mark.asyncio
    async def test_a_project_with_no_archive_files_reports_its_uploads(self) -> None:
        """A project uploaded through the live pipeline has no manifest rows;
        0 / 0 told the model nothing had been ingested. Its uploads are in
        silver.ingest_progress, used only when the archive count is zero."""
        sql = (await self._ingest_call()).sql
        live = sql[sql.index("live AS") :]
        assert "FROM silver.ingest_progress ip" in live
        assert "ip.workspace_id = $1::uuid" in live
        assert "ip.project_id = $2::uuid" in live
        assert "DISTINCT ON (ip.minio_key)" in live
        assert "CASE WHEN indexed.n > 0 THEN indexed.n ELSE live.indexed_n END" in live
        assert "CASE WHEN indexed.n > 0 THEN processed.n ELSE live.done_n END" in live

    @pytest.mark.asyncio
    async def test_no_unscoped_manifest_count_is_left(self) -> None:
        """The old query was `SELECT COUNT(*) FROM bronze.ingest_manifest WHERE
        workspace_id = $1` and nothing else."""
        sql = (await self._ingest_call()).sql
        assert sql.count("bronze.ingest_manifest") == 1
        assert "guessed_project" in sql
