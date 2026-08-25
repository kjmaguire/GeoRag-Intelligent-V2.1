"""Unit tests for the GeoRAG agent tool functions.

These tests mock all external I/O (asyncpg, Qdrant, Neo4j) so they run
without any live infrastructure.  They verify:

  - Correct SQL construction and parameter binding for query_spatial_collars
  - Graceful timeout handling (returns empty list, does not raise)
  - Graceful database exception handling (returns empty list, does not raise)
  - search_documents returns empty when embedding_model is None (pre-M2)
  - traverse_knowledge_graph maps Neo4j records to GraphEntity correctly
  - verify_numerical_claim returns verified=True when values match within tol
  - verify_numerical_claim returns verified=False when values diverge
  - verify_numerical_claim blocks disallowed table names

Run with:
    pytest tests/test_agent_tools.py -v
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.deps import AgentDeps
from app.agent.tools import (
    AssayDataResult,
    CollarRecord,
    DocumentSearchResult,
    DownholeLogsResult,
    GraphTraversalResult,
    NumericalClaimVerification,
    ProjectOverviewResult,
    SpatialQueryResult,
    query_assay_data,
    query_downhole_logs,
    query_project_overview,
    query_spatial_collars,
    search_documents,
    traverse_knowledge_graph,
    verify_numerical_claim,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_deps(
    *,
    pg_pool: object = None,
    qdrant_client: object = None,
    neo4j_driver: object = None,
    project_id: str = "00000000-0000-0000-0000-0000000000aa",
    embedding_model: object = None,
    reranker: object = None,
    workspace_id: str | None = None,
) -> AgentDeps:
    """Build a minimal AgentDeps for testing.

    ``workspace_id`` mirrors the JWT-sourced tenant carried in production. Pass
    it for retrieval-path tests: audit C3 makes search_documents FAIL CLOSED
    when no workspace can be resolved (no JWT and no pg_pool lookup), so the
    reranker/quality-gate tests must supply one to exercise the happy path.

    ``project_id`` must be UUID-shaped: ``AgentDeps.acquire_scoped()``
    (2026-08-15 audit — now used by query_spatial_collars,
    query_project_overview, query_downhole_logs, query_assay_data) rejects
    non-UUID project_id values before issuing the ``SET LOCAL app.project_id``
    GUC bind. The tool-call-site ``project_id=...`` kwarg used throughout
    this file's test bodies (e.g. ``project_id="proj-test-uuid"``) is a
    SEPARATE value — the SQL bind for the WHERE clause — and is unaffected.
    """
    return AgentDeps(
        pg_pool=pg_pool,  # type: ignore[arg-type]
        qdrant_client=qdrant_client,  # type: ignore[arg-type]
        neo4j_driver=neo4j_driver,  # type: ignore[arg-type]
        project_id=project_id,
        embedding_model=embedding_model,
        reranker=reranker,
        workspace_id=workspace_id,
    )


@dataclass
class _MockRunContext:
    """Minimal stand-in for pydantic_ai.RunContext[AgentDeps]."""

    deps: AgentDeps


class _TxnCM:
    """Async context manager stand-in for asyncpg's ``conn.transaction()``."""

    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _wire_scoped_conn(mock_conn: AsyncMock) -> None:
    """Make an ``AsyncMock`` connection usable with ``AgentDeps.acquire_scoped()``.

    ``acquire_scoped()`` does ``async with conn.transaction():``. Calling
    ``.transaction()`` on a bare ``AsyncMock`` returns an un-awaited coroutine
    (AsyncMock's own async call semantics), which does not implement the
    async context manager protocol and raises a TypeError. Override it with
    a plain ``MagicMock`` that returns a real (trivial) async CM instead —
    same pattern as ``tests/test_acquire_scoped.py``'s ``_make_pool`` helper.
    """
    mock_conn.transaction = MagicMock(return_value=_TxnCM())


# ---------------------------------------------------------------------------
# query_spatial_collars
# ---------------------------------------------------------------------------


class TestQuerySpatialCollars:
    """Tests for query_spatial_collars tool."""

    @pytest.mark.asyncio
    async def test_returns_collar_records(self) -> None:
        """Tool maps asyncpg Row dicts to CollarRecord instances."""
        fake_row = {
            "collar_id": "collar-uuid-001",
            "hole_id": "ATDD-001",
            "easting": 512345.0,
            "northing": 6123456.0,
            "elevation": 245.5,
            "total_depth": 350.0,
            "hole_type": "Diamond",
            "azimuth": 270.0,
            "dip": -60.0,
            "status": "Completed",
            "drill_date": "2023-06-15",
            # lon/lat columns are added by tools.py via ST_Transform(geom, 4326)
            # for the MapLibre client; mocked rows have to supply them too.
            "longitude": -106.5,
            "latitude": 52.1,
        }

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[fake_row])
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        # asyncpg pool.acquire() is an async context manager
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        deps = _make_deps(pg_pool=mock_pool)
        ctx = _MockRunContext(deps=deps)

        result: SpatialQueryResult = await query_spatial_collars(
            ctx,  # type: ignore[arg-type]
            project_id="proj-test-uuid",
        )

        assert result.count == 1
        assert result.data_source == "PostGIS silver.collars"
        collar: CollarRecord = result.collars[0]
        assert collar.hole_id == "ATDD-001"
        assert collar.total_depth == 350.0
        assert collar.dip == -60.0
        assert collar.status == "Completed"

    @pytest.mark.asyncio
    async def test_spatial_filter_searches_the_source_grid(self) -> None:
        """A radius search compares against the easting/northing COLUMNS.

        This used to assert `"ST_DWithin" in sql`, which pinned the
        implementation rather than the behaviour — and the implementation was
        wrong. `geom` is declared geometry(POINT, 32613) and every collar is
        transformed into that SRID at insert, so `Find_SRID` returns 32613
        whatever the project's real CRS is, while the caller's easting and
        northing are in the PROJECT's grid.

        Measured against a live Postgres with RedStar's Sitka collars
        (EPSG:26904): a 500 m search around their true coordinates returned
        0 rows via ST_DWithin on `geom`, and all 5 against the columns. The
        columns hold the untouched source values, so both sides of the
        comparison are in one grid and no SRID is involved.
        """
        captured_sql: list[str] = []
        captured_args: list[tuple[object, ...]] = []

        mock_conn = AsyncMock()

        async def _capture_fetch(sql: str, *args: object) -> list:
            captured_sql.append(sql)
            captured_args.append(args)
            return []

        mock_conn.fetch = _capture_fetch
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        deps = _make_deps(pg_pool=mock_pool)
        ctx = _MockRunContext(deps=deps)

        await query_spatial_collars(
            ctx,  # type: ignore[arg-type]
            project_id="proj-test-uuid",
            center_easting=512000.0,
            center_northing=6120000.0,
            radius_m=500.0,
        )

        assert captured_sql, "fetch was never called"
        sql = captured_sql[0]

        # A radius filter is applied, over the source-grid columns.
        assert "easting -" in sql and "northing -" in sql
        assert "ST_DWithin" not in sql, (
            "ST_DWithin on `geom` searches SRID 32613, not the project's grid"
        )
        assert "Find_SRID" not in sql, (
            "Find_SRID reads the COLUMN's declared SRID (32613), which is not "
            "the CRS the caller's easting/northing are expressed in"
        )
        # The radius must be cast: `$n * $n` on two untyped parameters is
        # `unknown * unknown`, which Postgres rejects outright.
        assert "::double precision" in sql

        # The caller's own numbers reach the query unmodified — no reprojection
        # is applied to them, because none is needed.
        assert 512000.0 in captured_args[0]
        assert 6120000.0 in captured_args[0]
        assert 500.0 in captured_args[0]

    @pytest.mark.asyncio
    async def test_collar_coordinates_come_from_the_columns_not_the_geometry(
        self,
    ) -> None:
        """`ST_X(geom)` is not the easting the file supplied.

        Since every collar is transformed into the column's declared SRID at
        insert, ST_X(geom) equals the source easting only for a UTM 13N
        project. Measured on Sitka: the columns hold (400807, 6117291) and
        ST_X/ST_Y(geom) return (-2765464.8, 7604657.1). The agent is told to
        cite returned numerics verbatim, so it would report the second pair.
        """
        captured_sql: list[str] = []

        mock_conn = AsyncMock()

        async def _capture_fetch(sql: str, *args: object) -> list:
            captured_sql.append(sql)
            return []

        mock_conn.fetch = _capture_fetch
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        deps = _make_deps(pg_pool=mock_pool)
        ctx = _MockRunContext(deps=deps)

        await query_spatial_collars(ctx, project_id="proj-test-uuid")  # type: ignore[arg-type]

        assert captured_sql, "fetch was never called"
        sql = captured_sql[0]
        assert "ST_X(geom) AS easting" not in sql
        assert "ST_Y(geom) AS northing" not in sql
        assert "easting, northing" in sql
        # lon/lat come from the stored geom_4326, which is transformed from the
        # SOURCE srid and is therefore exact.
        assert "ST_X(geom_4326) AS longitude" in sql

    @pytest.mark.asyncio
    async def test_returns_empty_on_timeout(self) -> None:
        """Tool returns empty SpatialQueryResult on asyncio.TimeoutError — does not raise."""

        async def _slow_fetch(*args: object, **kwargs: object) -> list:
            await asyncio.sleep(999)
            return []

        mock_conn = AsyncMock()
        mock_conn.fetch = _slow_fetch
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        deps = _make_deps(pg_pool=mock_pool)
        ctx = _MockRunContext(deps=deps)

        with patch("app.agent.tools.settings") as mock_settings:
            mock_settings.TIMEOUT_POSTGIS_S = 0.01  # force near-instant timeout
            result: SpatialQueryResult = await query_spatial_collars(
                ctx,  # type: ignore[arg-type]
                project_id="proj-test-uuid",
            )

        assert result.count == 0
        assert result.collars == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_db_exception(self) -> None:
        """Tool returns empty SpatialQueryResult on database error — does not raise."""
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(side_effect=RuntimeError("connection refused"))
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        deps = _make_deps(pg_pool=mock_pool)
        ctx = _MockRunContext(deps=deps)

        result: SpatialQueryResult = await query_spatial_collars(
            ctx,  # type: ignore[arg-type]
            project_id="proj-test-uuid",
        )
        assert result.count == 0

    @pytest.mark.asyncio
    async def test_limit_capped_at_200(self) -> None:
        """Limit parameter is silently capped to 200."""
        captured_args: list[tuple] = []

        async def _capture_fetch(sql: str, *args: object) -> list:
            captured_args.append(args)
            return []

        mock_conn = AsyncMock()
        mock_conn.fetch = _capture_fetch
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        deps = _make_deps(pg_pool=mock_pool)
        ctx = _MockRunContext(deps=deps)

        await query_spatial_collars(
            ctx,  # type: ignore[arg-type]
            project_id="proj-test-uuid",
            limit=9999,
        )

        # The last bound arg is the limit value — must be capped to 200.
        assert captured_args[0][-1] == 200


# ---------------------------------------------------------------------------
# search_documents
# ---------------------------------------------------------------------------


class TestSearchDocuments:
    """Tests for search_documents tool."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_model_not_loaded(self) -> None:
        """search_documents returns empty DocumentSearchResult when embedding_model is None."""
        deps = _make_deps(embedding_model=None)
        ctx = _MockRunContext(deps=deps)

        result: DocumentSearchResult = await search_documents(
            ctx,  # type: ignore[arg-type]
            query_text="what is the average copper grade?",
            project_id="proj-test-uuid",
        )

        assert result.count == 0
        assert result.chunks == []
        assert "not loaded" in result.data_source

    @pytest.mark.asyncio
    async def test_collection_selection_follows_adr_0010_flag(self) -> None:
        """ADR-0010 hard flag flip — RETRIEVAL_USE_DOCUMENT_PASSAGES routes
        search_documents to the canonical georag_chunks collection when True
        and the legacy georag_reports when False. Pins both branches so a
        future edit that hard-codes either collection name is caught."""
        import numpy as np

        async def _run_once(flag_value: bool) -> str:
            mock_qdrant_response = MagicMock()
            mock_qdrant_response.points = []  # no hits — keeps the test cheap

            mock_qdrant = AsyncMock()
            mock_qdrant.query_points = AsyncMock(return_value=mock_qdrant_response)

            mock_model = MagicMock()
            mock_model.encode = MagicMock(
                return_value=np.array([0.1] * 384, dtype="float32")
            )

            deps = _make_deps(
                qdrant_client=mock_qdrant,
                embedding_model=mock_model,
                workspace_id="a0000000-0000-0000-0000-000000000001",
            )
            ctx = _MockRunContext(deps=deps)

            # Patch encode_sparse to avoid loading SPLADE in this unit test.
            with patch("app.agent.tools.settings") as mock_settings, \
                 patch("app.services.sparse_encoder.encode_sparse", return_value={1: 0.5}):
                mock_settings.TIMEOUT_QDRANT_S = 5.0
                mock_settings.RETRIEVAL_TOP_N = 20
                mock_settings.RETRIEVAL_QUALITY_THRESHOLD = 0.3
                mock_settings.RETRIEVAL_USE_DOCUMENT_PASSAGES = flag_value
                mock_settings.QDRANT_DOCUMENT_PROJECT_SCOPE = "cross_project"

                result = await search_documents(
                    ctx,  # type: ignore[arg-type]
                    query_text="uranium grade",
                    project_id="proj-test-uuid",
                )

            # Capture the collection_name argument flowed through hybrid_query.
            assert mock_qdrant.query_points.await_count == 1
            kwargs = mock_qdrant.query_points.await_args.kwargs
            collection = kwargs.get("collection_name")
            # Also assert the data_source label reflects the routed collection.
            assert collection in result.data_source
            return collection

        assert await _run_once(False) == "georag_reports"
        assert await _run_once(True) == "georag_chunks"

    @pytest.mark.asyncio
    async def test_returns_empty_on_qdrant_timeout(self) -> None:
        """search_documents returns empty on Qdrant timeout — does not raise."""

        async def _slow_query(*args: object, **kwargs: object) -> object:
            await asyncio.sleep(999)

        mock_qdrant = AsyncMock()
        mock_qdrant.query_points = _slow_query

        # Use a non-None stub embedding model so we reach the Qdrant call.
        mock_model = MagicMock()
        mock_model.encode = MagicMock(return_value=MagicMock(tolist=lambda: [0.1] * 768))

        deps = _make_deps(
            qdrant_client=mock_qdrant,
            embedding_model=mock_model,
            workspace_id="a0000000-0000-0000-0000-000000000001",
        )
        ctx = _MockRunContext(deps=deps)

        with patch("app.agent.tools.settings") as mock_settings:
            mock_settings.TIMEOUT_QDRANT_S = 0.01
            mock_settings.RETRIEVAL_QUALITY_THRESHOLD = 0.3
            result: DocumentSearchResult = await search_documents(
                ctx,  # type: ignore[arg-type]
                query_text="uranium grade",
                project_id="proj-test-uuid",
            )

        assert result.count == 0

    @pytest.mark.asyncio
    async def test_reranker_overwrites_cosine_scores_and_sorts(self) -> None:
        """When reranker is present, relevance_score is replaced by cross-encoder logit
        and candidates are sorted descending by that logit."""
        import numpy as np

        # Build two fake Qdrant points with different cosine scores.
        # The second point has a higher cosine score but should end up ranked
        # lower after the cross-encoder assigns it a worse logit.
        fake_point_low_cosine = MagicMock()
        fake_point_low_cosine.id = "chunk-uuid-001"
        fake_point_low_cosine.score = 0.45  # lower cosine
        fake_point_low_cosine.payload = {
            "text": "Indicated resources: 12.5 Mt at 0.45% Cu",
            "document_title": "NI 43-101 Tech Report",
            "report_id": "rep-001",
            "document_type": "NI43",
        }

        fake_point_high_cosine = MagicMock()
        fake_point_high_cosine.id = "chunk-uuid-002"
        fake_point_high_cosine.score = 0.72  # higher cosine
        fake_point_high_cosine.payload = {
            "text": "Background information about the company.",
            "document_title": "NI 43-101 Tech Report",
            "report_id": "rep-001",
            "document_type": "NI43",
        }

        mock_qdrant_response = MagicMock()
        mock_qdrant_response.points = [fake_point_low_cosine, fake_point_high_cosine]

        mock_qdrant = AsyncMock()
        mock_qdrant.query_points = AsyncMock(return_value=mock_qdrant_response)

        mock_model = MagicMock()
        mock_model.encode = MagicMock(
            return_value=np.array([0.1] * 384, dtype="float32")
        )

        # Reranker: first pair gets logit 8.5 (very relevant), second gets 1.2.
        mock_reranker = MagicMock()
        mock_reranker.predict = MagicMock(return_value=np.array([8.5, 1.2]))

        deps = _make_deps(
            qdrant_client=mock_qdrant,
            embedding_model=mock_model,
            reranker=mock_reranker,
            workspace_id="a0000000-0000-0000-0000-000000000001",
        )
        ctx = _MockRunContext(deps=deps)

        with patch("app.agent.tools.settings") as mock_settings, \
             patch("app.services.sparse_encoder.encode_sparse", return_value={1: 0.5}):
            mock_settings.TIMEOUT_QDRANT_S = 5.0
            mock_settings.TIMEOUT_RERANKER_S = 8.0
            mock_settings.RETRIEVAL_TOP_N = 20
            mock_settings.RETRIEVAL_QUALITY_THRESHOLD = 0.3
            mock_settings.RERANKER_SCORE_THRESHOLD = 0.0
            mock_settings.RERANKER_TOP_K = 5

            result: DocumentSearchResult = await search_documents(
                ctx,  # type: ignore[arg-type]
                query_text="What is the indicated copper resource?",
                project_id="proj-test-uuid",
            )

        # Two chunks pass the reranker threshold (both > 0.0).
        assert result.count == 2
        # First chunk should be the one with the higher reranker logit (8.5).
        # tools.py applies sigmoid(logit) before storing so relevance_score
        # fits Citation's confloat(0..1) contract.
        import math
        assert result.chunks[0].chunk_id == "chunk-uuid-001"
        assert result.chunks[0].relevance_score == pytest.approx(1 / (1 + math.exp(-8.5)))
        # Second chunk has the lower logit (1.2 → ~0.768 after sigmoid).
        assert result.chunks[1].chunk_id == "chunk-uuid-002"
        assert result.chunks[1].relevance_score == pytest.approx(1 / (1 + math.exp(-1.2)))
        # Order is preserved — higher logit first.
        assert result.chunks[0].relevance_score > result.chunks[1].relevance_score
        # data_source must indicate reranking occurred.
        assert "reranked" in result.data_source

    @pytest.mark.asyncio
    async def test_reranker_foundry_backend_does_not_double_sigmoid(self) -> None:
        """_FoundryReranker (Cohere Rerank v4) returns an ALREADY-calibrated
        [0,1] relevance_score, unlike cross_encoder/qwen3_causal's raw
        logits. Sigmoiding it a second time compresses every foundry score
        into ~[0.5, 0.73], silently corrupting the min_relevance gates that
        assume real [0,1] calibration (plan_executor.py, decomposer.py) —
        caught in a live review session. With RERANKER_BACKEND="foundry",
        the stored relevance_score must equal the raw score exactly, not
        sigmoid(raw score)."""
        import numpy as np

        fake_point = MagicMock()
        fake_point.id = "chunk-uuid-foundry-001"
        fake_point.score = 0.5
        fake_point.payload = {
            "text": "Indicated resources: 12.5 Mt at 0.45% Cu",
            "document_title": "NI 43-101 Tech Report",
            "report_id": "rep-001",
            "document_type": "NI43",
        }

        mock_qdrant_response = MagicMock()
        mock_qdrant_response.points = [fake_point]

        mock_qdrant = AsyncMock()
        mock_qdrant.query_points = AsyncMock(return_value=mock_qdrant_response)

        mock_model = MagicMock()
        mock_model.encode = MagicMock(
            return_value=np.array([0.1] * 384, dtype="float32")
        )

        # Cohere's own relevance_score — already a calibrated probability.
        mock_reranker = MagicMock()
        mock_reranker.predict = MagicMock(return_value=np.array([0.95]))

        deps = _make_deps(
            qdrant_client=mock_qdrant,
            embedding_model=mock_model,
            reranker=mock_reranker,
            workspace_id="a0000000-0000-0000-0000-000000000001",
        )
        ctx = _MockRunContext(deps=deps)

        with patch("app.agent.tools.settings") as mock_settings, \
             patch("app.agent.tools.RERANKER_BACKEND", "foundry"), \
             patch("app.services.sparse_encoder.encode_sparse", return_value={1: 0.5}):
            mock_settings.TIMEOUT_QDRANT_S = 5.0
            mock_settings.TIMEOUT_RERANKER_S = 8.0
            mock_settings.RETRIEVAL_TOP_N = 20
            mock_settings.RETRIEVAL_QUALITY_THRESHOLD = 0.3
            mock_settings.RERANKER_SCORE_THRESHOLD = 0.0
            mock_settings.RERANKER_SCORE_THRESHOLD_FOUNDRY = 0.2
            mock_settings.RERANKER_TOP_K = 5

            result: DocumentSearchResult = await search_documents(
                ctx,  # type: ignore[arg-type]
                query_text="What is the indicated copper resource?",
                project_id="proj-test-uuid",
            )

        assert result.count == 1
        # Must be the raw Cohere score (0.95), NOT sigmoid(0.95) (~0.721).
        assert result.chunks[0].relevance_score == pytest.approx(0.95)

    @pytest.mark.asyncio
    async def test_reranker_foundry_threshold_drops_low_relevance_candidates(self) -> None:
        """2026-08-15 audit fix: RERANKER_SCORE_THRESHOLD (0.0) was a no-op
        against the foundry backend because Cohere's relevance_score is a
        [0,1] probability that is never negative — ``score >= 0.0`` passed
        every candidate through regardless of actual relevance. With
        RERANKER_BACKEND="foundry", the gate must use
        RERANKER_SCORE_THRESHOLD_FOUNDRY instead: a clearly-irrelevant
        candidate (0.05) must be dropped while a relevant one (0.6) survives.
        """
        import numpy as np

        fake_point_irrelevant = MagicMock()
        fake_point_irrelevant.id = "chunk-uuid-irrelevant"
        fake_point_irrelevant.score = 0.5
        fake_point_irrelevant.payload = {
            "text": "Unrelated boilerplate paragraph.",
            "document_title": "NI 43-101 Tech Report",
            "report_id": "rep-001",
            "document_type": "NI43",
        }
        fake_point_relevant = MagicMock()
        fake_point_relevant.id = "chunk-uuid-relevant"
        fake_point_relevant.score = 0.5
        fake_point_relevant.payload = {
            "text": "Indicated resources: 12.5 Mt at 0.45% Cu",
            "document_title": "NI 43-101 Tech Report",
            "report_id": "rep-001",
            "document_type": "NI43",
        }

        mock_qdrant_response = MagicMock()
        mock_qdrant_response.points = [fake_point_irrelevant, fake_point_relevant]

        mock_qdrant = AsyncMock()
        mock_qdrant.query_points = AsyncMock(return_value=mock_qdrant_response)

        mock_model = MagicMock()
        mock_model.encode = MagicMock(
            return_value=np.array([0.1] * 384, dtype="float32")
        )

        # Cohere relevance_score: 0.05 (clearly irrelevant) and 0.6 (relevant).
        mock_reranker = MagicMock()
        mock_reranker.predict = MagicMock(return_value=np.array([0.05, 0.6]))

        deps = _make_deps(
            qdrant_client=mock_qdrant,
            embedding_model=mock_model,
            reranker=mock_reranker,
            workspace_id="a0000000-0000-0000-0000-000000000001",
        )
        ctx = _MockRunContext(deps=deps)

        with patch("app.agent.tools.settings") as mock_settings, \
             patch("app.agent.tools.RERANKER_BACKEND", "foundry"), \
             patch("app.services.sparse_encoder.encode_sparse", return_value={1: 0.5}):
            mock_settings.TIMEOUT_QDRANT_S = 5.0
            mock_settings.TIMEOUT_RERANKER_S = 8.0
            mock_settings.RETRIEVAL_TOP_N = 20
            mock_settings.RETRIEVAL_QUALITY_THRESHOLD = 0.3
            # Sign-only threshold would pass BOTH (0.05 >= 0.0 and 0.6 >= 0.0).
            mock_settings.RERANKER_SCORE_THRESHOLD = 0.0
            mock_settings.RERANKER_SCORE_THRESHOLD_FOUNDRY = 0.2
            mock_settings.RERANKER_TOP_K = 5

            result: DocumentSearchResult = await search_documents(
                ctx,  # type: ignore[arg-type]
                query_text="What is the indicated copper resource?",
                project_id="proj-test-uuid",
            )

        # Only the 0.6-relevance chunk survives the 0.2 foundry floor.
        assert result.count == 1
        assert result.chunks[0].chunk_id == "chunk-uuid-relevant"
        assert result.chunks[0].relevance_score == pytest.approx(0.6)

    @pytest.mark.asyncio
    async def test_reranker_top_k_caps_results(self) -> None:
        """RERANKER_TOP_K=2 means only the two highest-logit chunks are returned
        even when more candidates pass the score threshold."""
        import numpy as np

        # Build 5 fake Qdrant points, all with text payloads.
        fake_points = []
        for i in range(5):
            p = MagicMock()
            p.id = f"chunk-uuid-{i:03d}"
            p.score = 0.5
            p.payload = {
                "text": f"Chunk text {i}",
                "document_title": "Report",
                "report_id": "rep-001",
                "document_type": "NI43",
            }
            fake_points.append(p)

        mock_qdrant_response = MagicMock()
        mock_qdrant_response.points = fake_points

        mock_qdrant = AsyncMock()
        mock_qdrant.query_points = AsyncMock(return_value=mock_qdrant_response)

        mock_model = MagicMock()
        mock_model.encode = MagicMock(
            return_value=np.array([0.1] * 384, dtype="float32")
        )

        # Reranker scores: all positive, descending.
        mock_reranker = MagicMock()
        mock_reranker.predict = MagicMock(
            return_value=np.array([9.0, 7.5, 6.0, 4.5, 2.0])
        )

        deps = _make_deps(
            qdrant_client=mock_qdrant,
            embedding_model=mock_model,
            reranker=mock_reranker,
            workspace_id="a0000000-0000-0000-0000-000000000001",
        )
        ctx = _MockRunContext(deps=deps)

        with patch("app.agent.tools.settings") as mock_settings, \
             patch("app.services.sparse_encoder.encode_sparse", return_value={1: 0.5}):
            mock_settings.TIMEOUT_QDRANT_S = 5.0
            mock_settings.TIMEOUT_RERANKER_S = 8.0
            mock_settings.RETRIEVAL_TOP_N = 20
            mock_settings.RETRIEVAL_QUALITY_THRESHOLD = 0.3
            mock_settings.RERANKER_SCORE_THRESHOLD = 0.0
            mock_settings.RERANKER_TOP_K = 2  # only top-2

            result: DocumentSearchResult = await search_documents(
                ctx,  # type: ignore[arg-type]
                query_text="resource estimate",
                project_id="proj-test-uuid",
            )

        # Must be capped at top-K=2. Sigmoid-transformed logits are stored.
        import math
        assert result.count == 2
        assert result.chunks[0].relevance_score == pytest.approx(1 / (1 + math.exp(-9.0)))
        assert result.chunks[1].relevance_score == pytest.approx(1 / (1 + math.exp(-7.5)))

    @pytest.mark.asyncio
    async def test_reranker_score_threshold_drops_negative_logits(self) -> None:
        """Chunks with reranker logit below RERANKER_SCORE_THRESHOLD are dropped."""
        import numpy as np

        fake_points = []
        for i in range(3):
            p = MagicMock()
            p.id = f"chunk-uuid-{i:03d}"
            p.score = 0.5
            p.payload = {
                "text": f"Chunk {i}",
                "document_title": "Report",
                "report_id": "rep-001",
                "document_type": "NI43",
            }
            fake_points.append(p)

        mock_qdrant_response = MagicMock()
        mock_qdrant_response.points = fake_points

        mock_qdrant = AsyncMock()
        mock_qdrant.query_points = AsyncMock(return_value=mock_qdrant_response)

        mock_model = MagicMock()
        mock_model.encode = MagicMock(
            return_value=np.array([0.1] * 384, dtype="float32")
        )

        # Two negative logits; default RERANKER_SCORE_THRESHOLD is 0.0.
        mock_reranker = MagicMock()
        mock_reranker.predict = MagicMock(
            return_value=np.array([5.0, -1.2, -3.4])
        )

        deps = _make_deps(
            qdrant_client=mock_qdrant,
            embedding_model=mock_model,
            reranker=mock_reranker,
            workspace_id="a0000000-0000-0000-0000-000000000001",
        )
        ctx = _MockRunContext(deps=deps)

        with patch("app.agent.tools.settings") as mock_settings, \
             patch("app.services.sparse_encoder.encode_sparse", return_value={1: 0.5}):
            mock_settings.TIMEOUT_QDRANT_S = 5.0
            mock_settings.TIMEOUT_RERANKER_S = 8.0
            mock_settings.RETRIEVAL_TOP_N = 20
            mock_settings.RETRIEVAL_QUALITY_THRESHOLD = 0.3
            mock_settings.RERANKER_SCORE_THRESHOLD = 0.0
            mock_settings.RERANKER_TOP_K = 5

            result: DocumentSearchResult = await search_documents(
                ctx,  # type: ignore[arg-type]
                query_text="resource estimate",
                project_id="proj-test-uuid",
            )

        # Only the one chunk with logit 5.0 (>= 0.0) survives; sigmoid-transformed.
        import math
        assert result.count == 1
        assert result.chunks[0].relevance_score == pytest.approx(1 / (1 + math.exp(-5.0)))

    @pytest.mark.asyncio
    async def test_falls_back_to_rrf_ordering_when_no_reranker(self) -> None:
        """Without a reranker, search_documents returns RRF order, degraded.

        This test used to assert the opposite, and its fixture is why the
        bug survived: it gave the Qdrant points cosine-shaped scores (0.72,
        0.15) and asserted the 0.15 one was filtered out by the Layer 1
        quality gate.

        Real points on this path do not carry cosine scores. `hybrid_query`
        fuses a dense and a sparse prefetch with server-side RRF, so
        `point.score` is rank-derived — a small number bounded by the fusion
        constant, with no relation to similarity. Gating those with a
        calibrated 0.5 dropped every chunk, and because `reranker is None`
        is reachable from nothing more exotic than an unset
        AZURE_FOUNDRY_API_KEY, that turned every document query in the
        system into "insufficient information".

        The contract now matches the `rerank_degraded` branch, which is the
        same situation: RRF order, truncated to RERANKER_TOP_K, flagged so
        the answer can say the precision stage did not run.
        """
        import numpy as np

        # RRF-shaped scores, which is what hybrid_query actually returns.
        # Both sit far below RETRIEVAL_QUALITY_THRESHOLD; under the old code
        # that meant zero chunks reached the agent.
        fake_point_top = MagicMock()
        fake_point_top.id = "chunk-uuid-001"
        fake_point_top.score = 0.0328  # top of both prefetch branches
        fake_point_top.payload = {
            "text": "Resource estimate paragraph.",
            "document_title": "NI 43-101",
            "report_id": "rep-001",
            "document_type": "NI43",
        }

        fake_point_lower = MagicMock()
        fake_point_lower.id = "chunk-uuid-002"
        fake_point_lower.score = 0.0161  # further down one branch
        fake_point_lower.payload = {
            "text": "Boilerplate legal text.",
            "document_title": "NI 43-101",
            "report_id": "rep-001",
            "document_type": "NI43",
        }

        mock_qdrant_response = MagicMock()
        mock_qdrant_response.points = [fake_point_top, fake_point_lower]

        mock_qdrant = AsyncMock()
        mock_qdrant.query_points = AsyncMock(return_value=mock_qdrant_response)

        mock_model = MagicMock()
        mock_model.encode = MagicMock(
            return_value=np.array([0.1] * 384, dtype="float32")
        )

        # reranker=None — no cross-encoder step.
        deps = _make_deps(
            qdrant_client=mock_qdrant,
            embedding_model=mock_model,
            reranker=None,
            workspace_id="a0000000-0000-0000-0000-000000000001",
        )
        ctx = _MockRunContext(deps=deps)

        # Patch encode_sparse to avoid a real call to the SPLADE sidecar (which
        # now requires X-Service-Key; the test env uses a dummy key). Matches
        # the 4 sibling search_documents tests — this one was missed.
        with patch("app.agent.tools.settings") as mock_settings, \
                patch("app.services.sparse_encoder.encode_sparse", return_value={1: 0.5}):
            mock_settings.TIMEOUT_QDRANT_S = 5.0
            mock_settings.RETRIEVAL_TOP_N = 20
            # Deliberately above every RRF score in the fixture. It must
            # not be consulted on this path; that it was is the bug.
            mock_settings.RETRIEVAL_QUALITY_THRESHOLD = 0.3
            # RERANKER_TOP_K IS read here — it is how many chunks the
            # precision stage would have handed back, so the degraded path
            # returns the same number rather than the full candidate set.
            mock_settings.RERANKER_SCORE_THRESHOLD = 0.0
            mock_settings.RERANKER_TOP_K = 5

            result: DocumentSearchResult = await search_documents(
                ctx,  # type: ignore[arg-type]
                query_text="resource estimate",
                project_id="proj-test-uuid",
            )

        # Both survive, in RRF order. Neither would have, before.
        assert result.count == 2
        assert [c.chunk_id for c in result.chunks] == [
            "chunk-uuid-001",
            "chunk-uuid-002",
        ]
        # The caller has to be able to tell that these scores are not
        # comparable to a reranked run's — plan_executor keys its
        # min_relevance gate off exactly this.
        assert result.rerank_degraded is True
        assert "reranked" not in result.data_source
        assert "rerank unavailable" in result.data_source


# ---------------------------------------------------------------------------
# traverse_knowledge_graph
# ---------------------------------------------------------------------------


class TestTraverseKnowledgeGraph:
    """Tests for traverse_knowledge_graph tool."""

    @pytest.mark.asyncio
    async def test_maps_neo4j_records_to_graph_entities(self) -> None:
        """Tool maps Neo4j record dicts to GraphEntity instances."""
        fake_records = [
            {
                "entity_id": "elem-id-001",
                "entity_type": "Formation",
                "name": "Athabasca Group",
                "props": {"age_ma": "1700", "rock_type": "Sandstone"},
                "rel_type": "OVERLIES",
                "direction": "INBOUND",
            }
        ]

        mock_result = AsyncMock()
        mock_result.data = AsyncMock(return_value=fake_records)

        mock_session = AsyncMock()
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_driver = MagicMock()
        mock_driver.session = MagicMock(return_value=mock_session)

        deps = _make_deps(neo4j_driver=mock_driver)
        ctx = _MockRunContext(deps=deps)

        result: GraphTraversalResult = await traverse_knowledge_graph(
            ctx,  # type: ignore[arg-type]
            entity_name="Basement",
            project_id="proj-test-uuid",
        )

        assert result.count == 1
        entity = result.entities[0]
        assert entity.name == "Athabasca Group"
        assert entity.entity_type == "Formation"
        assert entity.relationship_type == "OVERLIES"
        assert entity.relationship_direction == "INBOUND"
        assert entity.properties["age_ma"] == "1700"

    @pytest.mark.asyncio
    async def test_returns_empty_on_neo4j_timeout(self) -> None:
        """Tool returns empty GraphTraversalResult on Neo4j timeout — does not raise."""

        async def _slow_run(*args: object, **kwargs: object) -> object:
            await asyncio.sleep(999)

        mock_session = AsyncMock()
        mock_session.run = _slow_run
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_driver = MagicMock()
        mock_driver.session = MagicMock(return_value=mock_session)

        deps = _make_deps(neo4j_driver=mock_driver)
        ctx = _MockRunContext(deps=deps)

        with patch("app.agent.tools.settings") as mock_settings:
            mock_settings.TIMEOUT_NEO4J_S = 0.01
            result: GraphTraversalResult = await traverse_knowledge_graph(
                ctx,  # type: ignore[arg-type]
                entity_name="Basement",
                project_id="proj-test-uuid",
            )

        assert result.count == 0

    @pytest.mark.asyncio
    async def test_depth_capped_at_3(self) -> None:
        """Depth parameter is capped to 3 regardless of input."""
        # We just check no error is raised with depth=99; the cap is internal.
        mock_result = AsyncMock()
        mock_result.data = AsyncMock(return_value=[])
        mock_session = AsyncMock()
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_driver = MagicMock()
        mock_driver.session = MagicMock(return_value=mock_session)

        deps = _make_deps(neo4j_driver=mock_driver)
        ctx = _MockRunContext(deps=deps)

        result = await traverse_knowledge_graph(
            ctx,  # type: ignore[arg-type]
            entity_name="Zone",
            project_id="proj-test-uuid",
            depth=99,
        )
        assert result.count == 0


# ---------------------------------------------------------------------------
# verify_numerical_claim
# ---------------------------------------------------------------------------


class TestVerifyNumericalClaim:
    """Tests for verify_numerical_claim tool (Layer 3 hallucination prevention)."""

    @pytest.mark.asyncio
    async def test_verified_true_within_tolerance(self) -> None:
        """Returns verified=True when claimed and db values match within tolerance."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"total_depth": 350.001})
        # acquire_scoped() opens a transaction; a bare AsyncMock's
        # .transaction() returns a coroutine, not an async CM.
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        deps = _make_deps(pg_pool=mock_pool)
        ctx = _MockRunContext(deps=deps)

        result: NumericalClaimVerification = await verify_numerical_claim(
            ctx,  # type: ignore[arg-type]
            table="silver.collars",
            column="total_depth",
            row_id="collar-uuid-001",
            claimed_value=350.0,
            tolerance=0.01,
        )

        assert result.verified is True
        assert result.db_value == 350.001

    @pytest.mark.asyncio
    async def test_verified_false_outside_tolerance(self) -> None:
        """Returns verified=False when claimed and db values diverge beyond tolerance."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"total_depth": 400.0})
        # acquire_scoped() opens a transaction; a bare AsyncMock's
        # .transaction() returns a coroutine, not an async CM.
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        deps = _make_deps(pg_pool=mock_pool)
        ctx = _MockRunContext(deps=deps)

        result: NumericalClaimVerification = await verify_numerical_claim(
            ctx,  # type: ignore[arg-type]
            table="silver.collars",
            column="total_depth",
            row_id="collar-uuid-001",
            claimed_value=350.0,
        )

        assert result.verified is False
        assert result.db_value == 400.0
        assert result.claim_value == 350.0

    @pytest.mark.asyncio
    async def test_blocks_disallowed_table(self) -> None:
        """Returns verified=False and never queries the database for disallowed tables."""
        deps = _make_deps(pg_pool=MagicMock())
        ctx = _MockRunContext(deps=deps)

        result: NumericalClaimVerification = await verify_numerical_claim(
            ctx,  # type: ignore[arg-type]
            table="public.users",  # not in allowlist
            column="id",
            row_id="some-uuid",
            claimed_value=1.0,
        )

        assert result.verified is False
        assert result.db_value is None
        assert "BLOCKED" in result.verification_query

    @pytest.mark.asyncio
    async def test_verified_false_row_not_found(self) -> None:
        """Returns verified=False when the row does not exist in the database."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        # acquire_scoped() opens a transaction; a bare AsyncMock's
        # .transaction() returns a coroutine, not an async CM.
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        deps = _make_deps(pg_pool=mock_pool)
        ctx = _MockRunContext(deps=deps)

        result: NumericalClaimVerification = await verify_numerical_claim(
            ctx,  # type: ignore[arg-type]
            table="silver.collars",
            column="total_depth",
            row_id="nonexistent-uuid",
            claimed_value=350.0,
        )

        assert result.verified is False
        assert result.db_value is None

    @pytest.mark.asyncio
    async def test_verified_false_on_timeout(self) -> None:
        """Returns verified=False on PostGIS timeout — does not raise."""

        async def _slow_fetchrow(*args: object, **kwargs: object) -> object:
            await asyncio.sleep(999)

        mock_conn = AsyncMock()
        mock_conn.fetchrow = _slow_fetchrow
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        deps = _make_deps(pg_pool=mock_pool)
        ctx = _MockRunContext(deps=deps)

        with patch("app.agent.tools.settings") as mock_settings:
            mock_settings.TIMEOUT_POSTGIS_S = 0.01
            result: NumericalClaimVerification = await verify_numerical_claim(
                ctx,  # type: ignore[arg-type]
                table="silver.samples",
                column="to_depth",
                row_id="sample-uuid-001",
                claimed_value=100.5,
            )

        assert result.verified is False

    @pytest.mark.asyncio
    async def test_direct_table_scoped_by_workspace_and_project(self) -> None:
        """2026-08-15 audit fix: a direct table (silver.collars, which
        carries workspace_id + project_id itself) must bind BOTH into the
        WHERE clause when AgentDeps carries a workspace - previously this
        tool was scoped only by primary key, with zero tenant check, despite
        the query planner (_dispatch_factual_lookup) using it as a
        general-purpose value-retrieval oracle.
        """
        captured: dict = {}

        async def _capture_fetchrow(sql: str, *args: object) -> dict:
            captured["sql"] = sql
            captured["args"] = args
            return {"total_depth": 350.0}

        mock_conn = AsyncMock()
        mock_conn.fetchrow = _capture_fetchrow
        # acquire_scoped() opens a transaction; a bare AsyncMock's
        # .transaction() returns a coroutine, not an async CM.
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        ws_id = "a0000000-0000-0000-0000-000000000001"
        proj_id = "b0000000-0000-0000-0000-000000000002"
        deps = _make_deps(pg_pool=mock_pool, project_id=proj_id, workspace_id=ws_id)
        ctx = _MockRunContext(deps=deps)

        await verify_numerical_claim(
            ctx,  # type: ignore[arg-type]
            table="silver.collars",
            column="total_depth",
            row_id="collar-uuid-001",
            claimed_value=350.0,
        )

        assert "workspace_id = $2::uuid" in captured["sql"]
        assert "project_id = $3::uuid" in captured["sql"]
        assert captured["args"] == ("collar-uuid-001", ws_id, proj_id)

    @pytest.mark.asyncio
    async def test_collar_join_table_scoped_via_collars(self) -> None:
        """A collar-join table (silver.samples - carries workspace_id itself
        but not project_id) must scope project_id via a join to
        silver.collars, and must use the real "sample_id" pk column against
        the "t" alias.
        """
        captured: dict = {}

        async def _capture_fetchrow(sql: str, *args: object) -> dict:
            captured["sql"] = sql
            captured["args"] = args
            return {"to_depth": 12.5}

        mock_conn = AsyncMock()
        mock_conn.fetchrow = _capture_fetchrow
        # acquire_scoped() opens a transaction; a bare AsyncMock's
        # .transaction() returns a coroutine, not an async CM.
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        ws_id = "a0000000-0000-0000-0000-000000000001"
        proj_id = "b0000000-0000-0000-0000-000000000002"
        deps = _make_deps(pg_pool=mock_pool, project_id=proj_id, workspace_id=ws_id)
        ctx = _MockRunContext(deps=deps)

        await verify_numerical_claim(
            ctx,  # type: ignore[arg-type]
            table="silver.samples",
            column="to_depth",
            row_id="sample-uuid-001",
            claimed_value=12.5,
        )

        assert "JOIN silver.collars c ON c.collar_id = t.collar_id" in captured["sql"]
        assert "t.sample_id = $1::uuid" in captured["sql"]
        assert "t.workspace_id = $2::uuid" in captured["sql"]
        assert "c.project_id = $3::uuid" in captured["sql"]
        assert captured["args"] == ("sample-uuid-001", ws_id, proj_id)

    @pytest.mark.asyncio
    async def test_no_workspace_falls_back_to_unscoped(self) -> None:
        """When AgentDeps carries no workspace (single-tenant / admin path),
        the query stays unscoped by workspace - lenient/absent fallback,
        matching acquire_scoped()'s own GUC-bind behaviour."""
        captured: dict = {}

        async def _capture_fetchrow(sql: str, *args: object) -> dict:
            captured["sql"] = sql
            captured["args"] = args
            return {"total_depth": 350.0}

        mock_conn = AsyncMock()
        mock_conn.fetchrow = _capture_fetchrow
        # acquire_scoped() opens a transaction; a bare AsyncMock's
        # .transaction() returns a coroutine, not an async CM.
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        deps = _make_deps(pg_pool=mock_pool, workspace_id=None)
        ctx = _MockRunContext(deps=deps)

        await verify_numerical_claim(
            ctx,  # type: ignore[arg-type]
            table="silver.collars",
            column="total_depth",
            row_id="collar-uuid-001",
            claimed_value=350.0,
        )

        assert "workspace_id" not in captured["sql"]
        assert "project_id = $2::uuid" in captured["sql"]

    @pytest.mark.asyncio
    async def test_renamed_structure_table_resolves(self) -> None:
        """silver.structure (singular - replaces the DROPPED "silver.structures"
        plural table) must be reachable with its real "id" pk column, not the
        old (never-real) "structure_id"."""
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"true_dip": 45.0})
        # acquire_scoped() opens a transaction; a bare AsyncMock's
        # .transaction() returns a coroutine, not an async CM.
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        deps = _make_deps(pg_pool=mock_pool)
        ctx = _MockRunContext(deps=deps)

        result = await verify_numerical_claim(
            ctx,  # type: ignore[arg-type]
            table="silver.structure",
            column="true_dip",
            row_id="structure-uuid-001",
            claimed_value=45.0,
        )

        assert result.verified is True
        assert "BLOCKED" not in result.verification_query

    @pytest.mark.asyncio
    async def test_old_plural_structures_table_now_blocked(self) -> None:
        """The old "silver.structures" (plural) allowlist key pointed at a
        table that was dropped from the schema; it is no longer in the
        allowlist at all (renamed to the real "silver.structure")."""
        deps = _make_deps(pg_pool=MagicMock())
        ctx = _MockRunContext(deps=deps)

        result = await verify_numerical_claim(
            ctx,  # type: ignore[arg-type]
            table="silver.structures",
            column="true_dip",
            row_id="structure-uuid-001",
            claimed_value=45.0,
        )

        assert result.verified is False
        assert "BLOCKED" in result.verification_query


# ---------------------------------------------------------------------------
# query_downhole_logs / query_project_overview / query_assay_data - tenancy
# ---------------------------------------------------------------------------
#
# These three tools (alongside query_spatial_collars above) previously
# acquired their PostGIS connection via ctx.deps.pg_pool.acquire()
# directly and filtered SQL only on project_id - never workspace_id - so
# no RLS GUC was ever bound and a caller from one workspace could read
# another workspace's rows sharing the same project_id space (2026-08-15
# audit). Fixed to use ctx.deps.acquire_scoped() + explicit workspace_id
# SQL binds. These were previously untested at the unit level; the tests
# below close that gap by asserting the workspace filter + bound value
# appear in the generated SQL whenever AgentDeps carries a workspace_id.


class TestQueryDownholeLogsTenancy:
    """Tenancy-scoping tests for query_downhole_logs."""

    @pytest.mark.asyncio
    async def test_workspace_id_bound_when_present(self) -> None:
        captured_sql: list[str] = []
        captured_args: list[tuple] = []

        async def _capture_fetchrow(sql: str, *args: object) -> None:
            captured_sql.append(sql)
            captured_args.append(args)
            return None

        async def _capture_fetch(sql: str, *args: object) -> list:
            captured_sql.append(sql)
            captured_args.append(args)
            return []

        mock_conn = AsyncMock()
        mock_conn.fetchrow = _capture_fetchrow
        mock_conn.fetch = _capture_fetch
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        ws_id = "a0000000-0000-0000-0000-000000000001"
        deps = _make_deps(pg_pool=mock_pool, workspace_id=ws_id)
        ctx = _MockRunContext(deps=deps)

        result: DownholeLogsResult = await query_downhole_logs(
            ctx,  # type: ignore[arg-type]
            project_id="proj-test-uuid",
            hole_id="MS-117",
        )

        assert result.count == 0
        # Both the collar_sql (fetchrow) and logs_sql (fetch) must scope by
        # workspace_id, and the bound value must be the caller's workspace.
        assert any("workspace_id = $3" in s for s in captured_sql)
        assert any(ws_id in a for a in captured_args)

    @pytest.mark.asyncio
    async def test_no_workspace_id_falls_back_unscoped(self) -> None:
        """Absent workspace (single-tenant path) - SQL has no workspace filter."""
        captured_sql: list[str] = []

        async def _capture_fetchrow(sql: str, *args: object) -> None:
            captured_sql.append(sql)
            return None

        async def _capture_fetch(sql: str, *args: object) -> list:
            captured_sql.append(sql)
            return []

        mock_conn = AsyncMock()
        mock_conn.fetchrow = _capture_fetchrow
        mock_conn.fetch = _capture_fetch
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        deps = _make_deps(pg_pool=mock_pool, workspace_id=None)
        ctx = _MockRunContext(deps=deps)

        await query_downhole_logs(
            ctx,  # type: ignore[arg-type]
            project_id="proj-test-uuid",
            hole_id="MS-117",
        )

        assert all("workspace_id" not in s for s in captured_sql)


class TestQueryProjectOverviewTenancy:
    """Tenancy-scoping tests for query_project_overview."""

    @pytest.mark.asyncio
    async def test_workspace_id_bound_on_every_query(self) -> None:
        captured_sql: list[str] = []
        captured_args: list[tuple] = []

        async def _capture_fetchrow(sql: str, *args: object) -> dict | None:
            captured_sql.append(sql)
            captured_args.append(args)
            return None

        async def _capture_fetch(sql: str, *args: object) -> list:
            captured_sql.append(sql)
            captured_args.append(args)
            return []

        mock_conn = AsyncMock()
        mock_conn.fetchrow = _capture_fetchrow
        mock_conn.fetch = _capture_fetch
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        ws_id = "a0000000-0000-0000-0000-000000000001"
        deps = _make_deps(pg_pool=mock_pool, workspace_id=ws_id)
        ctx = _MockRunContext(deps=deps)

        result: ProjectOverviewResult = await query_project_overview(
            ctx,  # type: ignore[arg-type]
            project_id="proj-test-uuid",
        )

        assert result.count == 0
        # 5 underlying queries (project_sql, collar_count_sql, curves_sql,
        # reports_count_sql, reports_breakdown_sql) - every one must scope
        # by workspace_id, and every bind tuple must carry the workspace.
        assert len(captured_sql) == 5
        assert all("workspace_id = $2" in s for s in captured_sql)
        assert all(args == ("proj-test-uuid", ws_id) for args in captured_args)


class TestQueryAssayDataTenancy:
    """Tenancy-scoping tests for query_assay_data."""

    @pytest.mark.asyncio
    async def test_workspace_id_bound_when_present(self) -> None:
        captured_sql: list[str] = []
        captured_args: list[tuple] = []

        async def _capture_fetch(sql: str, *args: object) -> list:
            captured_sql.append(sql)
            captured_args.append(args)
            return []

        mock_conn = AsyncMock()
        mock_conn.fetch = _capture_fetch
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        ws_id = "a0000000-0000-0000-0000-000000000001"
        deps = _make_deps(pg_pool=mock_pool, workspace_id=ws_id)
        ctx = _MockRunContext(deps=deps)

        result: AssayDataResult = await query_assay_data(
            ctx,  # type: ignore[arg-type]
            project_id="proj-test-uuid",
        )

        # avail_sql returns no elements -> early-return, but it must still
        # have been scoped by workspace_id.
        assert result.count == 0
        assert captured_sql, "avail_sql fetch was never called"
        assert "s.workspace_id = $2" in captured_sql[0]
        assert captured_args[0] == ("proj-test-uuid", ws_id)

    @pytest.mark.asyncio
    async def test_no_workspace_id_falls_back_unscoped(self) -> None:
        captured_sql: list[str] = []

        async def _capture_fetch(sql: str, *args: object) -> list:
            captured_sql.append(sql)
            return []

        mock_conn = AsyncMock()
        mock_conn.fetch = _capture_fetch
        _wire_scoped_conn(mock_conn)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        deps = _make_deps(pg_pool=mock_pool, workspace_id=None)
        ctx = _MockRunContext(deps=deps)

        await query_assay_data(
            ctx,  # type: ignore[arg-type]
            project_id="proj-test-uuid",
        )

        assert captured_sql
        assert "workspace_id" not in captured_sql[0]


# ---------------------------------------------------------------------------
# Golden query tests
# ---------------------------------------------------------------------------


class TestLayer2OutputValidation:
    """Layer 2 hallucination-prevention tests — the Pydantic contracts that
    reject malformed RAG responses before they leave the service.

    Replaces the historical TestGoldenQueries class that exercised the now-
    archived pydantic_ai Agent (app.agent.geo_agent). Tool-calling golden-path
    coverage lives in the integration suite now; here we keep only the pure
    schema tests that guarantee fabrication-shaped outputs can never leave
    the typed-output boundary.
    """

    @pytest.mark.asyncio
    async def test_geo_rag_response_rejects_empty_citations(self) -> None:
        """GeoRAGResponse with empty citations list fails Pydantic validation (Layer 2)."""
        from pydantic import ValidationError

        from app.models.rag import GeoRAGResponse

        with pytest.raises(ValidationError) as exc_info:
            GeoRAGResponse(
                text="Some answer",
                citations=[],  # must be non-empty
                confidence=0.9,
                sources_used=["chunk-001"],
            )

        errors = exc_info.value.errors()
        assert any("citations" in str(e) for e in errors)

    @pytest.mark.asyncio
    async def test_geo_rag_response_rejects_empty_source_chunk_id(self) -> None:
        """Citation with empty source_chunk_id fails Pydantic validation (Layer 2)."""
        from pydantic import ValidationError

        from app.models.rag import Citation

        with pytest.raises(ValidationError):
            Citation(
                citation_id="[DATA-1]",
                citation_type="DATA",
                source_chunk_id="",  # must be non-empty
                document_title="Test",
                relevance_score=0.9,
            )

