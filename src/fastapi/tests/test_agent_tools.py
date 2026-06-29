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
    CollarRecord,
    DocumentSearchResult,
    GraphTraversalResult,
    NumericalClaimVerification,
    SpatialQueryResult,
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
    project_id: str = "proj-test-uuid",
    embedding_model: object = None,
    reranker: object = None,
    workspace_id: str | None = None,
) -> AgentDeps:
    """Build a minimal AgentDeps for testing.

    ``workspace_id`` mirrors the JWT-sourced tenant carried in production. Pass
    it for retrieval-path tests: audit C3 makes search_documents FAIL CLOSED
    when no workspace can be resolved (no JWT and no pg_pool lookup), so the
    reranker/quality-gate tests must supply one to exercise the happy path.
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
    async def test_spatial_filter_adds_st_dwithin(self) -> None:
        """When center coords + radius provided, SQL includes ST_DWithin."""
        captured_sql: list[str] = []

        mock_conn = AsyncMock()

        async def _capture_fetch(sql: str, *args: object) -> list:
            captured_sql.append(sql)
            return []

        mock_conn.fetch = _capture_fetch
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
        assert "ST_DWithin" in captured_sql[0]

    @pytest.mark.asyncio
    async def test_returns_empty_on_timeout(self) -> None:
        """Tool returns empty SpatialQueryResult on asyncio.TimeoutError — does not raise."""

        async def _slow_fetch(*args: object, **kwargs: object) -> list:
            await asyncio.sleep(999)
            return []

        mock_conn = AsyncMock()
        mock_conn.fetch = _slow_fetch
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
    async def test_falls_back_to_cosine_ordering_when_no_reranker(self) -> None:
        """Without a reranker, search_documents uses raw Qdrant cosine scores
        and the Layer 1 quality gate."""
        import numpy as np

        fake_point_pass = MagicMock()
        fake_point_pass.id = "chunk-uuid-001"
        fake_point_pass.score = 0.72  # above threshold
        fake_point_pass.payload = {
            "text": "Resource estimate paragraph.",
            "document_title": "NI 43-101",
            "report_id": "rep-001",
            "document_type": "NI43",
        }

        fake_point_fail = MagicMock()
        fake_point_fail.id = "chunk-uuid-002"
        fake_point_fail.score = 0.15  # below threshold
        fake_point_fail.payload = {
            "text": "Boilerplate legal text.",
            "document_title": "NI 43-101",
            "report_id": "rep-001",
            "document_type": "NI43",
        }

        mock_qdrant_response = MagicMock()
        mock_qdrant_response.points = [fake_point_pass, fake_point_fail]

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
            mock_settings.RETRIEVAL_QUALITY_THRESHOLD = 0.3
            # Reranker settings should not be read, but set them anyway to
            # confirm the fallback path doesn't accidentally use them.
            mock_settings.RERANKER_SCORE_THRESHOLD = 0.0
            mock_settings.RERANKER_TOP_K = 5

            result: DocumentSearchResult = await search_documents(
                ctx,  # type: ignore[arg-type]
                query_text="resource estimate",
                project_id="proj-test-uuid",
            )

        # Only the chunk above RETRIEVAL_QUALITY_THRESHOLD=0.3 survives.
        assert result.count == 1
        assert result.chunks[0].chunk_id == "chunk-uuid-001"
        # data_source must NOT contain "reranked".
        assert "reranked" not in result.data_source


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

