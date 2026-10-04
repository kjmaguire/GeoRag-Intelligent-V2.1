"""Audit 2026-10-04, tool/node-side items 3, 4, 12, 14 and 17 of the query path."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.agentic_retrieval import nodes as _nodes_mod
from app.agent.agentic_retrieval.nodes import (
    _render_assay_result,
    _render_structured_result,
    execute_node,
)
from app.agent.agentic_retrieval.retrieval_profile import profile_for_intent
from app.agent.agentic_retrieval.state import AgenticRetrievalState
from app.agent.hallucination.orchestrator_validators import verify_numbers
from app.agent.tools import (
    AssayDataResult,
    AssaySample,
    CollarDetailsResult,
    CollarRecord,
    DocumentChunk,
    DocumentSearchResult,
    ProjectOverviewResult,
    SpatialQueryResult,
    query_assay_data,
    query_project_overview,
    query_spatial_collars,
)

PROJECT = "00000000-0000-0000-0000-0000000000aa"
WORKSPACE = "a0000000-0000-0000-0000-000000000001"


class _Txn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _ctx(conn: Any, *, workspace_id: str | None = WORKSPACE) -> Any:
    conn.transaction = MagicMock(return_value=_Txn())
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    from app.agent.deps import AgentDeps

    deps = AgentDeps(
        pg_pool=pool, qdrant_client=None, neo4j_driver=None,  # type: ignore[arg-type]
        project_id=PROJECT, embedding_model=None, reranker=None,
        workspace_id=workspace_id,
    )

    @dataclass
    class _C:
        deps: AgentDeps

    return _C(deps=deps)


def _collar_row(i: int, total: int) -> dict[str, Any]:
    return {
        "collar_id": f"c-{i}", "hole_id": f"H-{i:03d}", "easting": 1.0 * i,
        "northing": 2.0, "elevation": 3.0, "total_depth": 100.0 + i,
        "hole_type": "Diamond", "azimuth": 0.0, "dip": -60.0, "status": "Done",
        "drill_date": None, "longitude": -106.0, "latitude": 52.0,
        "total_count": total,
    }


# ---------------------------------------------------------------------------
# Item 3 -- the matching total, not the LIMIT sample, answers "how many holes"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spatial_collars_expose_the_matching_total() -> None:
    sqls: list[str] = []

    async def fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        sqls.append(sql)
        return [_collar_row(i, 567) for i in range(50)]

    conn = AsyncMock()
    conn.fetch = fetch
    result = await query_spatial_collars(_ctx(conn), project_id=PROJECT)

    assert "COUNT(*) OVER() AS total_count" in sqls[0]
    assert "LIMIT" in sqls[0]
    assert result.count == 50
    assert result.total_count == 567
    assert result.retrieval_failure is None


def test_rendered_context_says_567_not_50() -> None:
    result = SpatialQueryResult(
        collars=[CollarRecord(**{k: v for k, v in _collar_row(i, 567).items() if k != "total_count"})
                 for i in range(50)],
        count=50, data_source="PostGIS silver.collars", total_count=567,
    )
    text = _render_structured_result(result)
    assert "total_holes_matching=567" in text
    assert "showing 12 of 567 (alphabetical sample)" in text
    assert "never as 50" in text


def test_layer3_grounds_the_total_but_not_the_sample_size() -> None:
    from app.agent.hallucination.orchestrator_validators import _collect_evidence

    result = SpatialQueryResult(
        collars=[CollarRecord(**{k: v for k, v in _collar_row(i, 567).items() if k != "total_count"})
                 for i in range(50)],
        count=50, data_source="PostGIS silver.collars", total_count=567,
    )
    literal = _collect_evidence([("query_spatial_collars", result)]).literal
    assert 567.0 in literal
    # Neither the returned-row count nor the list length is a fact about the
    # project, so neither may ground "50 holes".
    assert 50.0 not in literal
    assert verify_numbers("The project has 567 holes [DATA-1].", [("query_spatial_collars", result)]) == []


def test_a_result_without_a_total_still_grounds_its_row_count() -> None:
    from app.agent.hallucination.orchestrator_validators import _collect_evidence

    result = SpatialQueryResult(
        collars=[CollarRecord(**{k: v for k, v in _collar_row(i, 7).items() if k != "total_count"})
                 for i in range(7)],
        count=7, data_source="PostGIS silver.collars",
    )
    assert 7.0 in _collect_evidence([("query_spatial_collars", result)]).literal


# ---------------------------------------------------------------------------
# Item 4 -- the narration rows ARE the highest values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assay_rows_are_ordered_by_value_and_capped_at_50() -> None:
    data_sqls: list[tuple[str, tuple[Any, ...]]] = []
    agg_sqls: list[str] = []

    async def fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        if "jsonb_object_keys" in sql:
            return [{"elem": "Au_ppb"}]
        data_sqls.append((sql, args))
        return [
            {"hole_id": "H-1", "collar_id": "c1", "from_depth": 1.0, "to_depth": 2.0,
             "sample_type": "core", "val": 9.5},
        ]

    async def fetchrow(sql: str, *args: Any) -> dict[str, Any]:
        agg_sqls.append(sql)
        return {"min_v": 0.1, "max_v": 9.5, "mean_v": 1.2, "std_v": 0.8,
                "median_v": 1.0, "total_n": 4000}

    conn = AsyncMock()
    conn.fetch = fetch
    conn.fetchrow = fetchrow
    result = await query_assay_data(_ctx(conn), project_id=PROJECT)

    sql, args = data_sqls[0]
    assert "ORDER BY val DESC NULLS LAST" in sql
    assert "ORDER BY c.hole_id, s.from_depth" not in sql
    assert args[-1] == 50  # the narration cap, not 5000
    # The aggregate query is unchanged in spirit: full set, max included.
    assert "MAX(val)" in agg_sqls[0] and "COUNT(*)" in agg_sqls[0]
    assert result.count == 4000
    assert result.max_value == 9.5
    assert result.std_value == 0.8


def test_highest_label_is_true_when_rows_come_sorted() -> None:
    samples = [
        AssaySample(f"H-{i}", f"c{i}", 0.0, 1.0, "Au_ppb", 100.0 - i, "core")
        for i in range(50)
    ]
    result = AssayDataResult(
        samples=samples, count=4000, element="Au_ppb", available_elements=["Au_ppb"],
        min_value=0.1, max_value=100.0, mean_value=1.0, median_value=1.0,
        data_source="PostGIS silver.samples",
    )
    text = _render_assay_result(result)
    assert "highest 12 of 4000 samples" in text
    assert "H-0 " in text and "H-11 " in text and "H-12 " not in text


def test_anomaly_detector_uses_full_set_aggregates_not_the_top_rows() -> None:
    from app.agent.anomaly_detector import _assay_anomalies

    samples = [
        AssaySample(f"H-{i}", f"c{i}", 0.0, 1.0, "Au_ppb", v, "core")
        for i, v in enumerate([100.0, 99.0, 98.0, 97.0])
    ]
    result = AssayDataResult(
        samples=samples, count=4000, element="Au_ppb", available_elements=["Au_ppb"],
        min_value=0.0, max_value=100.0, mean_value=10.0, median_value=5.0,
        data_source="x", std_value=5.0,
    )
    insights = _assay_anomalies(result)
    # Against the project mean (10) and sigma (5) all four top values are far
    # outliers; against their own mean (98.5) none would be.
    assert len(insights) == 4
    assert "project mean of 10" in insights[0]


# ---------------------------------------------------------------------------
# Item 12 -- an outage is reported, not read as empty data
# ---------------------------------------------------------------------------


class _BoomConn:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.transaction = MagicMock(return_value=_Txn())

    async def execute(self, *a: Any, **k: Any) -> None:
        return None

    async def fetch(self, *a: Any, **k: Any) -> Any:
        raise self._exc

    async def fetchrow(self, *a: Any, **k: Any) -> Any:
        raise self._exc


@pytest.mark.asyncio
@pytest.mark.parametrize(("exc", "expected"), [
    (RuntimeError("connection reset"), "error"),
    (TimeoutError(), "timeout"),
])
async def test_structured_tools_report_the_failure(exc: Exception, expected: str) -> None:
    conn = _BoomConn(exc)
    ctx = _ctx(conn)
    spatial = await query_spatial_collars(ctx, project_id=PROJECT)
    assay = await query_assay_data(ctx, project_id=PROJECT)
    overview = await query_project_overview(ctx, project_id=PROJECT)
    for result in (spatial, assay, overview):
        assert result.retrieval_failure == expected
    assert isinstance(spatial, SpatialQueryResult) and spatial.count == 0
    assert isinstance(assay, AssayDataResult) and assay.count == 0
    assert isinstance(overview, ProjectOverviewResult)


class _NodeDeps:
    project_id = PROJECT
    workspace_id = WORKSPACE
    pg_pool = None
    redis_client = None


def _chunk(cid: str = "c1") -> DocumentChunk:
    return DocumentChunk(
        chunk_id=cid, text="Resource 12.5 Mt", source_document_id="rep-1",
        document_title="R", section_number=None, section_title=None, section=None,
        page=1, document_type="NI43", report_id="rep-1", relevance_score=0.9,
    )


def _state(query: str, intent: str = "factual_lookup") -> AgenticRetrievalState:
    return AgenticRetrievalState(
        query=query, deps=_NodeDeps(), intent=intent, effective_intent=intent,
        retrieval_profile=profile_for_intent(intent),
    )


@pytest.mark.asyncio
async def test_execute_node_surfaces_a_structured_failure_without_failing_the_query(
    monkeypatch,
) -> None:
    failed = SpatialQueryResult(
        collars=[], count=0, data_source="PostGIS silver.collars",
        retrieval_failure="timeout",
    )
    docs = DocumentSearchResult(chunks=[_chunk()], count=1, data_source="qdrant (reranked)")

    async def fake_call(tool_name, query, deps):
        if tool_name == "search_documents":
            return docs
        if tool_name == "query_spatial_collars":
            return failed
        return None

    monkeypatch.setattr(_nodes_mod, "_call_tool_safely", fake_call)
    profile = profile_for_intent("factual_lookup").model_copy(
        update={"primary_tools": ["search_documents", "query_spatial_collars"],
                "secondary_tools": []}
    )
    state = _state("what is the resource?").model_copy(update={"retrieval_profile": profile})
    update = await execute_node(state)

    assert update["retrieval_failures"] == ["Drill-hole collars (temporarily unavailable)"]
    assert [n for n, _ in update["tool_results"]] == ["search_documents"]


@pytest.mark.asyncio
async def test_hole_prepass_failure_is_noted(monkeypatch) -> None:
    import app.agent.tools as _tools

    async def failing_details(deps, workspace_id, project_id, hole_id):
        return CollarDetailsResult(
            collar_id=None, hole_id=None, hole_id_canonical=None, project_id=project_id,
            workspace_id=workspace_id, total_depth=None, drill_type=None, hole_type=None,
            drill_date=None, easting=None, northing=None, elevation=None, azimuth=None,
            dip=None, geologist=None, assay_count=0, lithology_count=0, sample_count=0,
            structure_count=0, max_assay_value=None, lithology_summary=[],
            source_row_ids=[], count=0, retrieval_failure="timeout",
        )

    monkeypatch.setattr(_tools, "query_collar_details", failing_details)

    async def fake_call(tool_name, query, deps):
        return DocumentSearchResult(chunks=[_chunk()], count=1, data_source="qdrant (reranked)") \
            if tool_name == "search_documents" else None

    monkeypatch.setattr(_nodes_mod, "_call_tool_safely", fake_call)
    update = await execute_node(_state("tell me about hole PLS-22-08"))
    assert update["retrieval_failures"] == ["Drill-hole details (temporarily unavailable)"]


# ---------------------------------------------------------------------------
# Item 14 -- public geoscience gets tokens, not the whole question
# ---------------------------------------------------------------------------


@pytest.fixture
def pgeo_calls(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_search(ctx, *, text_query=None, commodities=None, **kw):
        calls.append({"text_query": text_query, "commodities": commodities, **kw})
        from app.agent.public_geoscience_tool import PublicGeoscienceSearchResult

        return PublicGeoscienceSearchResult(
            records=[], count=0, jurisdictions_queried=[], canonical_types_queried=[],
        )

    monkeypatch.setattr(
        "app.agent.public_geoscience_tool.search_public_geoscience", fake_search
    )
    return calls


@pytest.mark.asyncio
async def test_public_geoscience_gets_an_entity_token_not_the_question(pgeo_calls) -> None:
    await _nodes_mod._call_tool_safely(
        "search_public_geoscience",
        "What mineral occurrences are near Wollaston Lake?",
        _NodeDeps(),
    )
    assert len(pgeo_calls) == 1
    assert pgeo_calls[0]["text_query"] == "Wollaston Lake"
    assert pgeo_calls[0]["text_query"] != "What mineral occurrences are near Wollaston Lake?"


@pytest.mark.asyncio
async def test_public_geoscience_commodity_only_question_passes_the_commodity(pgeo_calls) -> None:
    await _nodes_mod._call_tool_safely(
        "search_public_geoscience", "what is the average gold grade?", _NodeDeps(),
    )
    assert pgeo_calls == [{"text_query": None, "commodities": ["gold"]}]


@pytest.mark.asyncio
async def test_public_geoscience_is_not_called_without_a_token(pgeo_calls) -> None:
    result = await _nodes_mod._call_tool_safely(
        "search_public_geoscience", "what is nearby?", _NodeDeps(),
    )
    assert result is None
    assert pgeo_calls == []


# ---------------------------------------------------------------------------
# Item 17 -- the three independent passes run concurrently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepass_primary_and_adversarial_run_concurrently(monkeypatch) -> None:
    import app.agent.tools as _tools

    in_flight = 0
    all_started = asyncio.Event()
    started: list[str] = []

    async def _rendezvous(name: str) -> None:
        nonlocal in_flight
        started.append(name)
        in_flight += 1
        if in_flight >= 3:
            all_started.set()
        # Serial execution would never reach 3 in flight -> timeout.
        await asyncio.wait_for(all_started.wait(), timeout=2.0)

    async def fake_details(deps, workspace_id, project_id, hole_id):
        await _rendezvous("prepass")
        return None

    monkeypatch.setattr(_tools, "query_collar_details", fake_details)

    primary_docs = DocumentSearchResult(
        chunks=[_chunk("c1"), _chunk("c2")], count=2, data_source="qdrant (reranked)",
    )
    adv_docs = DocumentSearchResult(
        chunks=[_chunk("c2"), _chunk("c3")], count=2, data_source="qdrant (reranked)",
    )

    async def fake_call(tool_name, query, deps):
        if tool_name == "search_documents":
            is_adv = query.startswith("Find evidence that CONTRADICTS")
            await _rendezvous("adversarial" if is_adv else "primary")
            return adv_docs if is_adv else primary_docs
        return None

    monkeypatch.setattr(_nodes_mod, "_call_tool_safely", fake_call)
    profile = profile_for_intent("hypothesis_generation").model_copy(
        update={"primary_tools": ["search_documents"], "secondary_tools": []}
    )
    assert profile.adversarial_pass_enabled
    state = _state("Does hole PLS-22-08 support the unconformity model?",
                   "hypothesis_generation").model_copy(update={"retrieval_profile": profile})

    update = await execute_node(state)

    assert sorted(started) == ["adversarial", "prepass", "primary"]
    names = [n for n, _ in update["tool_results"]]
    assert names == ["search_documents", "search_documents_adversarial"]
    # De-duplicated AFTER the gather: c2 appears once, under the primary id.
    adv = dict(update["tool_results"])["search_documents_adversarial"]
    assert [c.chunk_id for c in adv.chunks] == ["c3"]


# ---------------------------------------------------------------------------
# Items 26 and 27 -- caches, and an unsearchable query
# ---------------------------------------------------------------------------


def test_ttl_cache_expires_and_evicts(monkeypatch) -> None:
    import app.agent.tools as _tools

    now = [1000.0]
    monkeypatch.setattr(_tools._metric_time, "monotonic", lambda: now[0])
    cache = _tools._TTLCache(max_entries=2, ttl_s=10.0)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1
    cache.put("c", 3)  # evicts the least recently used: "b"
    assert cache.get("b") is None
    assert cache.get("a") == 1 and cache.get("c") == 3
    now[0] += 11
    assert cache.get("a") is None and cache.get("c") is None


def _search_ctx(model: Any) -> Any:
    import numpy as np  # noqa: F401

    from app.agent.deps import AgentDeps

    qdrant = AsyncMock()
    response = MagicMock()
    response.points = []
    qdrant.query_points = AsyncMock(return_value=response)
    deps = AgentDeps(
        pg_pool=None, qdrant_client=qdrant, neo4j_driver=None,  # type: ignore[arg-type]
        project_id=PROJECT, embedding_model=model, reranker=None, workspace_id=WORKSPACE,
    )

    @dataclass
    class _C:
        deps: AgentDeps

    return _C(deps=deps), qdrant


@pytest.mark.asyncio
async def test_query_embedding_is_cached_per_model_and_text() -> None:
    from unittest.mock import patch

    import numpy as np

    from app.agent.tools import search_documents

    class _Model:
        model_name = "cohere-embed-v4"
        calls = 0

        def embed_query(self, text: str):
            type(self).calls += 1
            return np.array([0.1, 0.2], dtype="float32")

    ctx, _q = _search_ctx(_Model())
    with patch("app.services.sparse_encoder.encode_sparse", return_value={1: 0.5}):
        await search_documents(ctx, query_text="uranium grade", project_id="p")
        await search_documents(ctx, query_text="uranium grade", project_id="p")
        assert _Model.calls == 1
        await search_documents(ctx, query_text="gold grade", project_id="p")
        assert _Model.calls == 2


@pytest.mark.asyncio
async def test_unnamed_embedding_model_is_never_cached() -> None:
    from unittest.mock import patch

    import numpy as np

    from app.agent.tools import search_documents

    class _Unnamed:
        calls = 0

        def encode(self, text: str, normalize_embeddings: bool = True):
            type(self).calls += 1
            return np.array([0.1, 0.2], dtype="float32")

    ctx, _q = _search_ctx(_Unnamed())
    with patch("app.services.sparse_encoder.encode_sparse", return_value={1: 0.5}):
        await search_documents(ctx, query_text="uranium grade", project_id="p")
        await search_documents(ctx, query_text="uranium grade", project_id="p")
    assert _Unnamed.calls == 2


@pytest.mark.asyncio
async def test_empty_sparse_vector_is_a_typed_failure_not_a_dense_only_query() -> None:
    from unittest.mock import patch

    import numpy as np

    from app.agent.tools import search_documents

    model = MagicMock()
    model.encode = MagicMock(return_value=np.array([0.1, 0.2], dtype="float32"))
    ctx, qdrant = _search_ctx(model)
    with patch("app.services.sparse_encoder.encode_sparse", return_value={}):
        result = await search_documents(ctx, query_text="???", project_id="p")
    assert result.retrieval_failure == "sparse_query_empty"
    assert result.chunks == []
    qdrant.query_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_node_raises_the_typed_query_error_and_it_classifies_for_the_user(monkeypatch) -> None:
    from app.agent.errors import EmptySparseQuery, ErrorCode, classify_error

    failed = DocumentSearchResult(
        chunks=[], count=0, data_source="Qdrant georag_chunks (query not searchable)",
        retrieval_failure="sparse_query_empty",
    )

    async def fake_call(tool_name, query, deps):
        return failed if tool_name == "search_documents" else None

    monkeypatch.setattr(_nodes_mod, "_call_tool_safely", fake_call)
    with pytest.raises(EmptySparseQuery) as exc_info:
        await execute_node(_state("???"))
    code, message = classify_error(exc_info.value)
    assert code == ErrorCode.QUERY_NOT_SEARCHABLE
    assert "rephrase" in message.lower()


@pytest.mark.asyncio
async def test_assay_element_metadata_is_cached_between_calls() -> None:
    avail_calls: list[int] = []

    async def fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        if "jsonb_object_keys" in sql:
            avail_calls.append(1)
            return [{"elem": "Au_ppb"}]
        return []

    async def fetchrow(sql: str, *args: Any) -> dict[str, Any]:
        return {"min_v": 0.1, "max_v": 9.5, "mean_v": 1.2, "std_v": 0.8,
                "median_v": 1.0, "total_n": 0}

    conn = AsyncMock()
    conn.fetch = fetch
    conn.fetchrow = fetchrow
    ctx = _ctx(conn)
    await query_assay_data(ctx, project_id=PROJECT)
    await query_assay_data(ctx, project_id=PROJECT)
    assert len(avail_calls) == 1


@pytest.mark.asyncio
async def test_assay_metadata_cache_is_per_workspace_and_never_caches_empty() -> None:
    avail_calls: list[Any] = []
    available = {"value": []}

    async def fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        if "jsonb_object_keys" in sql:
            avail_calls.append(args)
            return [{"elem": e} for e in available["value"]]
        return []

    conn = AsyncMock()
    conn.fetch = fetch
    conn.fetchrow = AsyncMock(return_value={"total_n": 0})
    await query_assay_data(_ctx(conn), project_id=PROJECT)
    await query_assay_data(_ctx(conn), project_id=PROJECT)
    assert len(avail_calls) == 2  # empty (not yet ingested) is not cached

    available["value"] = ["Au_ppb"]
    await query_assay_data(_ctx(conn, workspace_id=WORKSPACE), project_id=PROJECT)
    await query_assay_data(_ctx(conn, workspace_id="b0000000-0000-0000-0000-000000000002"),
                           project_id=PROJECT)
    assert len(avail_calls) == 4  # another workspace never reads this one's entry


# ---------------------------------------------------------------------------
# Item 22 -- user-visible strings are plain
# ---------------------------------------------------------------------------

_TECHNICAL = ("Qdrant", "georag_", "silver.", "PostGIS", "via ", "search_documents",
              "query_", "reranker", "provenance", "tool call")


def test_degraded_source_labels_are_plain() -> None:
    from app.agent.response_assembler import _collect_degraded_sources, plain_source_label

    assert plain_source_label("search_documents") == "Documents (temporarily unavailable)"
    assert plain_source_label("query_assay_data") == "Assay data (temporarily unavailable)"
    assert plain_source_label("query_something_new") == "Something new (temporarily unavailable)"

    degraded = DocumentSearchResult(
        chunks=[_chunk()], count=1, data_source="qdrant:georag_chunks (rerank unavailable)",
        rerank_degraded=True,
    )
    timed_out = SpatialQueryResult(
        collars=[], count=0, data_source="PostGIS silver.collars (timeout)",
    )
    labels = _collect_degraded_sources([
        ("search_documents", degraded), ("query_spatial_collars", timed_out),
    ])
    assert labels == [
        "Document ranking (temporarily unavailable)",
        "Drill-hole collars (temporarily unavailable)",
    ]
    for label in labels:
        assert not any(t in label for t in _TECHNICAL), label


def test_placeholder_citation_titles_are_plain() -> None:
    from app.agent.hallucination.layer2_typed_output import enforce_claim_citations
    from app.agent.hallucination.layer5_provenance import gate_citation_provenance
    from app.agent.response_assembler import assemble_response
    from app.models.rag import Citation, GeoRAGResponse

    titles = [assemble_response("Hello", []).citations[0].document_title]

    resp = GeoRAGResponse(
        text="The grade is 1.85 g/t Au over 4.2 m.",
        citations=[Citation(
            citation_id="[NI43-1]", citation_type="NI43",
            source_chunk_id="georag_reports:rep-1:section=1:chunk=c1",
            document_title="R", relevance_score=0.9,
        )],
        confidence=0.8, sources_used=["georag_reports:rep-1:section=1:chunk=c1"],
    )
    out, _f = enforce_claim_citations(resp)
    titles.append(out.citations[0].document_title)

    bad = resp.model_copy(update={
        "text": "The grade is 1.85 g/t Au [NI43-1].",
        "citations": [resp.citations[0].model_copy(update={
            "source_chunk_id": "georag_reports:rep-1:section=1:chunk=never-retrieved",
        })],
    })
    gated, _w = gate_citation_provenance(bad, [("search_documents", _docs_for_gate())])
    titles.append(gated.citations[0].document_title)

    assert titles == ["No source retrieved", "No supporting source", "No supporting source"]
    for title in titles:
        assert not any(t in title for t in _TECHNICAL), title


def _docs_for_gate():
    return DocumentSearchResult(chunks=[_chunk("other")], count=1, data_source="qdrant")
