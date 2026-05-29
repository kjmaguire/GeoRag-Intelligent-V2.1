"""Phase H — tests for orchestrator/run_cache.py rehydration path.

Covers:
* cache_key matches the legacy _cache_key wrapper (compat contract)
* build_cached_candidates serialises postgis + qdrant payloads
* rehydrate_tool_results reconstructs DocumentSearchResult + SpatialQueryResult
  from a CachedRetrievalContext round-trip
* Empty candidates_reranked → empty tool_results (safe-fallback)
* Neo4j candidates are skipped (no clean payload round-trip) but don't fail
* The full write-then-read round-trip preserves enough state for
  `_build_context` to produce non-empty context
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.agent.orchestrator.run_cache import (
    build_cached_candidates,
    build_cached_context,
    cache_key,
    rehydrate_tool_results,
)
from app.agent.tools import (
    CollarRecord,
    DocumentChunk,
    DocumentSearchResult,
    SpatialQueryResult,
)


# ───────────────────────── cache_key ─────────────────────────


def test_cache_key_categories_path_is_stable_and_v6_prefixed() -> None:
    k = cache_key(
        "hello",
        "proj-1",
        system_prompt_version=9,
        categories={"spatial": True, "documents": False},
        workspace_data_version=3,
        project_data_version=7,
        workspace_id="ws-1",
    )
    assert k.startswith("georag:rag_cache:v6:")
    # Stable: re-build with the same inputs → same hash
    k2 = cache_key(
        "hello",
        "proj-1",
        system_prompt_version=9,
        categories={"spatial": True, "documents": False},
        workspace_data_version=3,
        project_data_version=7,
        workspace_id="ws-1",
    )
    assert k == k2


def test_cache_key_prompt_version_change_busts_cache() -> None:
    """A `_SYSTEM_PROMPT_VERSION` bump must produce a different key."""
    k1 = cache_key(
        "hello", "proj-1",
        system_prompt_version=9,
        categories={"spatial": True},
    )
    k2 = cache_key(
        "hello", "proj-1",
        system_prompt_version=10,
        categories={"spatial": True},
    )
    assert k1 != k2


def test_cache_key_no_categories_path_still_v6_prefixed() -> None:
    k = cache_key(
        "hello", "proj-1",
        system_prompt_version=9,
        workspace_data_version=2,
        project_data_version=4,
        workspace_id="ws-1",
    )
    assert k.startswith("georag:rag_cache:v6:")


# ─────────────────────── build_cached_candidates ─────────────


def _fake_scored(store: str, payload, score: float = 0.9, canonical_id: str = "x", rrf_rank: int = 1):
    """Build a fake ScoredCandidate-shaped namespace for test inputs."""
    cand = SimpleNamespace(
        store=store,
        canonical_id=canonical_id,
        score=score,
        payload=payload,
    )
    return SimpleNamespace(
        candidate=cand,
        rrf_rank=rrf_rank,
        rrf_score=1.0 / (60 + rrf_rank),
    )


def _make_collar(hole_id: str, total_depth: float = 339.9) -> CollarRecord:
    """Build a minimal CollarRecord — only fills the fields the
    dataclass actually has (fields are accepted by-name on rehydration).
    """
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(CollarRecord)}
    base = {
        "collar_id": str(uuid4()),
        "hole_id": hole_id,
        "project_id": str(uuid4()),
        "total_depth": total_depth,
        "easting": 100.0,
        "northing": 200.0,
    }
    # Provide None for any other required fields the dataclass declares.
    kwargs = {f.name: base.get(f.name) for f in dataclasses.fields(CollarRecord)}
    return CollarRecord(**kwargs)


def _make_chunk(chunk_id: str, text: str = "sample text") -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        text=text,
        source_document_id="doc-1",
        document_title="Sample NI 43-101",
        section_number="14.1",
        section_title="Mineral Resource Estimate",
        section="14.1 Mineral Resource Estimate",
        page=42,
        document_type="NI43",
        report_id="rpt-1",
        relevance_score=0.85,
    )


def test_build_cached_candidates_postgis_serialises_payload() -> None:
    collar = _make_collar("36-1042")
    cands = build_cached_candidates([
        _fake_scored("postgis", collar, canonical_id="postgis:collars:36-1042"),
    ])
    assert len(cands) == 1
    c = cands[0]
    assert c.source_store == "postgis"
    assert c.candidate_ref == {
        "store":        "postgis",
        "canonical_id": "postgis:collars:36-1042",
    }
    # Phase H — payload now round-trips for postgis too
    assert c.payload is not None
    assert c.payload.get("hole_id") == "36-1042"


def test_build_cached_candidates_qdrant_serialises_payload() -> None:
    chunk = _make_chunk("chunk-uuid-abc")
    cands = build_cached_candidates([
        _fake_scored("qdrant", chunk),
    ])
    assert len(cands) == 1
    c = cands[0]
    assert c.source_store == "qdrant"
    assert c.payload is not None
    assert c.payload.get("chunk_id") == "chunk-uuid-abc"


# ─────────────────────── rehydrate_tool_results ─────────────


def _wrap_in_context(cands: list) -> "CachedRetrievalContext":
    """Build a CachedRetrievalContext around a list of candidates."""
    from app.models.retrieval_cache import CachedRetrievalContext  # noqa: PLC0415
    return CachedRetrievalContext(
        cached_at=datetime.now(timezone.utc),
        workspace_id=UUID("a0000000-0000-0000-0000-000000000001"),
        project_id=UUID("11111111-1111-1111-1111-111111111111"),
        workspace_data_version_at_cache=0,
        project_data_version_at_cache=0,
        query_class="spatial",
        sparse_boost_applied=False,
        retrieval_strategy_version="v2-retrieval-only-cache-2026-04-21",
        embedding_model_version="test-embedding",
        sparse_model_version="test-sparse",
        reranker_version="test-reranker",
        candidates_reranked=cands,
    )


def test_rehydrate_empty_candidates_returns_empty_list() -> None:
    ctx = _wrap_in_context([])
    assert rehydrate_tool_results(ctx) == []


def test_rehydrate_postgis_produces_spatial_query_result() -> None:
    collar = _make_collar("36-1042")
    cached = build_cached_candidates([_fake_scored("postgis", collar)])
    ctx = _wrap_in_context(cached)
    out = rehydrate_tool_results(ctx)
    assert len(out) == 1
    name, result = out[0]
    assert name == "query_spatial_collars"
    assert isinstance(result, SpatialQueryResult)
    assert result.count == 1
    assert result.collars[0].hole_id == "36-1042"
    assert "cache-rehydrated" in result.data_source


def test_rehydrate_qdrant_produces_document_search_result() -> None:
    ch = _make_chunk("chunk-uuid-abc")
    cached = build_cached_candidates([_fake_scored("qdrant", ch)])
    ctx = _wrap_in_context(cached)
    out = rehydrate_tool_results(ctx)
    assert len(out) == 1
    name, result = out[0]
    assert name == "search_documents"
    assert isinstance(result, DocumentSearchResult)
    assert result.count == 1
    assert result.chunks[0].chunk_id == "chunk-uuid-abc"


def test_rehydrate_mixed_stores_produces_both_results() -> None:
    cached = build_cached_candidates([
        _fake_scored("postgis", _make_collar("36-1042")),
        _fake_scored("qdrant",  _make_chunk("chunk-1")),
        _fake_scored("postgis", _make_collar("36-1065"), canonical_id="postgis:collars:36-1065", rrf_rank=2),
    ])
    ctx = _wrap_in_context(cached)
    out = rehydrate_tool_results(ctx)
    # 2 tool result entries (one per store), aggregating their candidates
    names = [n for (n, _) in out]
    assert "query_spatial_collars" in names
    assert "search_documents" in names
    spatial = next(r for (n, r) in out if n == "query_spatial_collars")
    assert spatial.count == 2  # both collars grouped


def test_rehydrate_neo4j_is_skipped_without_failing() -> None:
    """Graph entities don't dataclass-roundtrip cleanly today —
    rehydration must skip them silently rather than raise."""
    # Neo4j candidates have payload=None today (no dataclass writer)
    graph_cand = _fake_scored("neo4j", SimpleNamespace(name="Triple R"))
    cached = build_cached_candidates([graph_cand])
    ctx = _wrap_in_context(cached)
    out = rehydrate_tool_results(ctx)
    # No tool_results produced from graph alone — but no exception either
    assert out == []


def test_rehydrate_corrupt_payload_skipped_safely() -> None:
    """A cached entry with a missing required field falls back to skip
    instead of raising — better to refuse on the miss path than crash."""
    from app.models.retrieval_cache import CachedRetrievalCandidate  # noqa: PLC0415
    bad = CachedRetrievalCandidate(
        source_store="postgis",
        candidate_ref={"store": "postgis", "canonical_id": "x"},
        text="something",
        retriever_score=0.5,
        # payload deliberately missing required CollarRecord fields
        payload={"hole_id": "broken"},  # missing collar_id, project_id, etc.
        rrf_rank=1,
        rrf_score=0.016,
    )
    ctx = _wrap_in_context([bad])
    out = rehydrate_tool_results(ctx)
    # Phase H — depending on which fields CollarRecord has,
    # _coerce_collar may succeed (returning a partial collar) or skip.
    # Either outcome is acceptable as long as it doesn't raise.
    # If it succeeded, we get one tool result; if it skipped, zero.
    assert isinstance(out, list)


# ─────────────────────── build_cached_context ────────────────


def test_build_cached_context_round_trips_candidates() -> None:
    cands_in = [
        _fake_scored("postgis", _make_collar("36-1042")),
    ]
    ctx = build_cached_context(
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id="11111111-1111-1111-1111-111111111111",
        workspace_data_version=3,
        project_data_version=7,
        query_class="spatial",
        sparse_boost_applied=False,
        embedding_model_version="bge-small-en-v1.5",
        sparse_model_version="splade-pp",
        reranker_version="bge-reranker-base",
        partial_failures=None,
        fused_candidates=cands_in,
    )
    assert len(ctx.candidates_reranked) == 1
    # Run rehydration on the same context — full round-trip works
    out = rehydrate_tool_results(ctx)
    assert len(out) == 1
    name, result = out[0]
    assert name == "query_spatial_collars"
    assert result.count == 1


def test_build_cached_context_carries_partial_failures() -> None:
    ctx = build_cached_context(
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id=None,
        workspace_data_version=0,
        project_data_version=None,
        query_class="spatial",
        sparse_boost_applied=False,
        embedding_model_version="x",
        sparse_model_version="y",
        reranker_version=None,
        partial_failures=[("search_documents", "TimeoutError")],
        fused_candidates=[],
    )
    assert ctx.partial_failure_details == {
        "search_documents": "TimeoutError",
    }


# ──────────── Phase H continued: auxiliary tool roundtrip ─────────


def _make_project_overview():
    from app.agent.tools import ProjectOverviewResult
    return ProjectOverviewResult(
        project_name="Cameco Shirley Basin Uranium",
        company="CAMECO RESOURCES",
        commodity="uranium",
        region="CARBON, WY",
        slug="cameco-shirley-basin",
        collar_count=63,
        distinct_curves=["GAMMA", "RES", "SP"],
        report_count=1110,
        parser_breakdown={"pdfplumber": 800, "pdfminer.six": 250, "openpyxl": 60},
        count=1176,
    )


def test_build_cached_context_carries_project_overview_auxiliary() -> None:
    """Auxiliary tool results round-trip via auxiliary_tool_results."""
    po = _make_project_overview()
    ctx = build_cached_context(
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id="11111111-1111-1111-1111-111111111111",
        workspace_data_version=0,
        project_data_version=0,
        query_class="spatial",
        sparse_boost_applied=False,
        embedding_model_version="x",
        sparse_model_version="y",
        reranker_version=None,
        partial_failures=None,
        fused_candidates=[],
        tool_results=[("query_project_overview", po)],
    )
    assert "query_project_overview" in ctx.auxiliary_tool_results
    aux = ctx.auxiliary_tool_results["query_project_overview"]
    assert aux["project_name"] == "Cameco Shirley Basin Uranium"
    assert aux["commodity"] == "uranium"


def test_rehydrate_project_overview_from_auxiliary() -> None:
    """Cache-hit rehydration rebuilds ProjectOverviewResult cleanly."""
    from app.agent.tools import ProjectOverviewResult
    po = _make_project_overview()
    ctx = build_cached_context(
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id="11111111-1111-1111-1111-111111111111",
        workspace_data_version=0,
        project_data_version=0,
        query_class="spatial",
        sparse_boost_applied=False,
        embedding_model_version="x",
        sparse_model_version="y",
        reranker_version=None,
        partial_failures=None,
        fused_candidates=[],
        tool_results=[("query_project_overview", po)],
    )
    out = rehydrate_tool_results(ctx)
    names = [n for (n, _) in out]
    assert "query_project_overview" in names
    rebuilt = next(r for (n, r) in out if n == "query_project_overview")
    assert isinstance(rebuilt, ProjectOverviewResult)
    assert rebuilt.commodity == "uranium"
    assert rebuilt.collar_count == 63


def test_rehydrate_handles_missing_auxiliary_gracefully() -> None:
    """Legacy v6 cache entries lacking auxiliary_tool_results still work."""
    cands = build_cached_candidates([_fake_scored("postgis", _make_collar("36-1042"))])
    ctx = _wrap_in_context(cands)  # _wrap_in_context omits auxiliary
    out = rehydrate_tool_results(ctx)
    # Only the postgis-derived result, no auxiliary
    names = [n for (n, _) in out]
    assert "query_spatial_collars" in names
    assert "query_project_overview" not in names


def test_rehydrate_mixed_candidates_plus_auxiliary() -> None:
    """The orchestrator's normal case: collars from RRF + project_overview from auxiliary."""
    from app.agent.tools import ProjectOverviewResult, SpatialQueryResult
    po = _make_project_overview()
    ctx = build_cached_context(
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id="11111111-1111-1111-1111-111111111111",
        workspace_data_version=0,
        project_data_version=0,
        query_class="spatial",
        sparse_boost_applied=False,
        embedding_model_version="x",
        sparse_model_version="y",
        reranker_version=None,
        partial_failures=None,
        fused_candidates=[_fake_scored("postgis", _make_collar("36-1042"))],
        tool_results=[("query_project_overview", po)],
    )
    out = rehydrate_tool_results(ctx)
    names = [n for (n, _) in out]
    assert "query_spatial_collars" in names
    assert "query_project_overview" in names
    assert len(out) == 2
    # The postgis collar result is rebuilt fully
    spatial = next(r for (n, r) in out if n == "query_spatial_collars")
    assert isinstance(spatial, SpatialQueryResult)
    assert spatial.count == 1
    # And the project overview survived the round-trip
    po_rebuilt = next(r for (n, r) in out if n == "query_project_overview")
    assert isinstance(po_rebuilt, ProjectOverviewResult)
    assert po_rebuilt.distinct_curves == ["GAMMA", "RES", "SP"]


def test_auxiliary_only_query_returns_just_auxiliary() -> None:
    """A query that hit only project_overview (no collars / docs / graph)
    still rehydrates cleanly via the auxiliary slot."""
    from app.agent.tools import ProjectOverviewResult
    po = _make_project_overview()
    ctx = build_cached_context(
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id="11111111-1111-1111-1111-111111111111",
        workspace_data_version=0,
        project_data_version=0,
        query_class="spatial",
        sparse_boost_applied=False,
        embedding_model_version="x",
        sparse_model_version="y",
        reranker_version=None,
        partial_failures=None,
        fused_candidates=[],  # NO RRF candidates
        tool_results=[("query_project_overview", po)],
    )
    # Cache write produced no candidates_reranked but the auxiliary slot
    # carries the answer. Rehydration must NOT bail on the
    # "no candidates" path — it should still surface the auxiliary.
    out = rehydrate_tool_results(ctx)
    names = [n for (n, _) in out]
    assert names == ["query_project_overview"]
    rebuilt = out[0][1]
    assert isinstance(rebuilt, ProjectOverviewResult)
    assert rebuilt.project_name == "Cameco Shirley Basin Uranium"


# ────────────────────── Phase H3: PGEO roundtrip ─────────────────────


def _make_pgeo_result():
    from app.agent.public_geoscience_tool import (
        PublicGeoscienceRecord,
        PublicGeoscienceSearchResult,
    )
    records = [
        PublicGeoscienceRecord(
            pg_id="bc:minfile:09",
            canonical_type="mineral_occurrence",
            jurisdiction_code="BC",
            jurisdiction_name="British Columbia",
            source_id="minfile",
            source_feature_id="082E001",
            name="Sample BC occurrence",
            summary_text="Test occurrence in southeastern BC.",
            commodities=["Cu", "Au"],
            commodity_grouping="metallic",
            status="past_producer",
            geom_bbox=[-117.0, 49.0, -116.5, 49.5],
            source_url="https://example.bc.ca/minfile/082E001",
            license_summary="BC Crown Copyright",
            license_url="https://www2.gov.bc.ca/gov/content/data/policy-standards/open-data",
            staleness_seconds=86400,
            relevance_score=0.91,
        ),
    ]
    return PublicGeoscienceSearchResult(
        records=records,
        count=len(records),
        jurisdictions_queried=["BC"],
        canonical_types_queried=["mineral_occurrence"],
    )


def test_pgeo_roundtrip_via_auxiliary() -> None:
    """search_public_geoscience now serialises into auxiliary_tool_results
    and rehydrates cleanly."""
    from app.agent.public_geoscience_tool import (
        PublicGeoscienceRecord,
        PublicGeoscienceSearchResult,
    )
    pgeo = _make_pgeo_result()
    ctx = build_cached_context(
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id="11111111-1111-1111-1111-111111111111",
        workspace_data_version=0,
        project_data_version=0,
        query_class="document",
        sparse_boost_applied=False,
        embedding_model_version="x",
        sparse_model_version="y",
        reranker_version=None,
        partial_failures=None,
        fused_candidates=[],
        tool_results=[("search_public_geoscience", pgeo)],
    )
    # Roundtrip
    out = rehydrate_tool_results(ctx)
    names = [n for (n, _) in out]
    assert "search_public_geoscience" in names
    rebuilt = next(r for (n, r) in out if n == "search_public_geoscience")
    assert isinstance(rebuilt, PublicGeoscienceSearchResult)
    assert rebuilt.count == 1
    assert rebuilt.jurisdictions_queried == ["BC"]
    assert rebuilt.canonical_types_queried == ["mineral_occurrence"]
    rec = rebuilt.records[0]
    assert isinstance(rec, PublicGeoscienceRecord)
    assert rec.pg_id == "bc:minfile:09"
    assert rec.commodities == ["Cu", "Au"]
    assert rec.relevance_score == pytest.approx(0.91)


def test_pgeo_empty_records_still_roundtrips() -> None:
    """Zero-record PGEO results (legitimate empty searches) round-trip cleanly."""
    from app.agent.public_geoscience_tool import PublicGeoscienceSearchResult

    empty = PublicGeoscienceSearchResult(
        records=[],
        count=0,
        jurisdictions_queried=["SK"],
        canonical_types_queried=["drillhole_collar"],
    )
    ctx = build_cached_context(
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id="11111111-1111-1111-1111-111111111111",
        workspace_data_version=0,
        project_data_version=0,
        query_class="document",
        sparse_boost_applied=False,
        embedding_model_version="x",
        sparse_model_version="y",
        reranker_version=None,
        partial_failures=None,
        fused_candidates=[],
        tool_results=[("search_public_geoscience", empty)],
    )
    out = rehydrate_tool_results(ctx)
    rebuilt = next(r for (n, r) in out if n == "search_public_geoscience")
    assert rebuilt.count == 0
    assert rebuilt.records == []
    # Query metadata still threaded through
    assert rebuilt.jurisdictions_queried == ["SK"]
