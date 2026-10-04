"""Audit 2026-09-29, batch B: what the synthesis model is shown.

AGT-2 / RAG-5  structured results reach the LLM header-first (count and
               aggregates before rows), not as a 1,200-char repr.
AGT-15 / RAG-6 the commodity the question names picks the assay element;
               no commodity -> per-element summaries; named holes filter.
RAG-18         the context budget keeps structured data and the best
               chunks, and what it drops is removed from the evidence the
               citations and guards see.
AGT-5          assemble_node's state writes reach later nodes (graph-level).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.agent.agentic_retrieval.nodes import (
    _render_context_and_evidence,
    _render_structured_result,
)
from app.agent.response_assembler import assign_citation_ids
from app.agent.tools import (
    AssayDataResult,
    AssaySample,
    CollarRecord,
    DocumentChunk,
    DocumentSearchResult,
    ElementSummary,
    SpatialQueryResult,
    commodities_in_query,
    element_key_for_commodity,
)


def _assay(n: int, **extra: Any) -> AssayDataResult:
    samples = [
        AssaySample(
            hole_id=f"AA-{i:03d}", collar_id=f"c-{i}", from_depth=float(i),
            to_depth=float(i) + 1.0, element="U3O8_pct_e", value=0.01 * i,
            sample_type="core",
        )
        for i in range(n)
    ]
    return AssayDataResult(
        samples=samples, count=n, element="U3O8_pct_e",
        available_elements=["Au_ppb", "U3O8_pct_e"],
        min_value=0.0, max_value=0.01 * (n - 1), mean_value=0.4567,
        median_value=0.3333, data_source="PostGIS silver.samples", **extra,
    )


def _collar(i: int) -> CollarRecord:
    return CollarRecord(
        hole_id=f"H-{i:03d}", collar_id=f"c{i}", easting=500000.0 + i,
        northing=6000000.0, elevation=400.0, total_depth=300.0 + i,
        hole_type="DD", azimuth=0.0, dip=-90.0, status="done", drill_date=None,
    )


def _chunk(n: int, score: float, size: int = 5000) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=f"chunk-{n}", text="x" * size, source_document_id=f"doc-{n}",
        document_title=f"Report {n}", section_number=None, section_title=None,
        section=None, page=n, document_type="NI43", report_id=f"r-{n}",
        relevance_score=score,
    )


# ---------------------------------------------------------------------------
# AGT-2 / RAG-5
# ---------------------------------------------------------------------------


def test_assay_block_leads_with_count_and_aggregates():
    text = _render_structured_result(_assay(200))
    head = text[:400]
    assert "sample_count=200" in head
    assert "mean=0.4567" in head and "median=0.3333" in head
    assert "min=0" in head and "max=1.99" in head
    # A bounded, labelled row sample — the highest values, not the first.
    assert "highest 12 of 200" in text
    assert "AA-199" in text and "AA-000" not in text


def test_generic_block_puts_scalars_before_rows():
    result = SpatialQueryResult(
        collars=[_collar(i) for i in range(100)], count=100,
        data_source="PostGIS silver.collars",
    )
    text = _render_structured_result(result)
    first_line = text.splitlines()[0]
    assert "total_holes_matching=100" in first_line
    assert "collars: showing 12 of 100" in text
    assert len(text) <= 4000


def test_spatial_sample_is_labelled_with_the_true_total():
    """A LIMIT-capped alphabetical sample must not read as the project total
    (audit item 3): 50 collars retrieved from a 567-hole project."""
    result = SpatialQueryResult(
        collars=[_collar(i) for i in range(50)], count=50,
        data_source="PostGIS silver.collars", total_count=567,
    )
    text = _render_structured_result(result)
    assert "total_holes_matching=567" in text.splitlines()[0]
    assert "collars: showing 12 of 567 (alphabetical sample)" in text
    assert "showing 12 of 50" not in text
    assert "count=50" not in text


def test_long_scalar_fields_are_truncated_not_dumped():
    from dataclasses import dataclass

    @dataclass
    class _Card:
        count: int
        png_base64: str

    text = _render_structured_result(_Card(count=1, png_base64="A" * 5000))
    assert "count=1" in text
    assert len(text) < 600


# ---------------------------------------------------------------------------
# AGT-15 / RAG-6 — commodity -> element, summaries, holes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("What is the average gold grade?", ["gold"]),
        ("top Au assays in PLS-22-08", ["gold"]),
        ("compare copper and Au values", ["copper", "gold"]),
        ("U3O8 over 2 g/t", ["uranium"]),
        ("any rare earth elements?", ["rare earths"]),
        ("lithium brine results", ["lithium"]),
        ("what could lead to a better resource?", []),
        ("what is the deepest hole?", []),
    ],
)
def test_commodities_in_query(query, expected):
    assert commodities_in_query(query) == expected


def test_element_key_for_commodity_matches_this_projects_keys():
    available = ["Au_ppb", "Au_ppb_e", "U3O8_ppm", "U_ppm", "Li2O_pct", "cu_ppm"]
    assert element_key_for_commodity("gold", available) == "Au_ppb_e"
    assert element_key_for_commodity("uranium", available) == "U3O8_ppm"
    assert element_key_for_commodity("lithium", available) == "Li2O_pct"
    assert element_key_for_commodity("copper", available) == "cu_ppm"
    assert element_key_for_commodity("nickel", available) is None


def test_auto_selected_element_renders_every_elements_summary():
    result = _assay(
        5,
        element_auto_selected=True,
        element_summaries=[
            ElementSummary("Au_ppb", 40, 1.0, 900.0, 55.5, 12.0),
            ElementSummary("U3O8_pct_e", 5, 0.0, 0.04, 0.02, 0.02),
        ],
    )
    text = _render_structured_result(result)
    assert "no single commodity was named" in text
    assert "Au_ppb: n=40" in text and "mean=55.5" in text
    assert "U3O8_pct_e: n=5" in text


def test_unavailable_commodity_is_stated():
    result = _assay(
        3, requested_commodity="gold", requested_commodity_unavailable=True,
    )
    text = _render_structured_result(result)
    assert "asks about gold" in text
    assert "do not present them as gold grades" in text


class _Conn:
    """Answers query_assay_data's four statements by shape."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: Any):
        self.calls.append((sql, args))
        if "jsonb_object_keys" in sql:
            return [{"elem": "Au_ppb"}, {"elem": "U3O8_pct_e"}]
        if "jsonb_each_text" in sql:
            return [
                {"elem": "Au_ppb", "n": 4, "min_v": 1.0, "max_v": 9.0,
                 "mean_v": 5.0, "median_v": 5.0},
                {"elem": "U3O8_pct_e", "n": 2, "min_v": 0.1, "max_v": 0.3,
                 "mean_v": 0.2, "median_v": 0.2},
            ]
        return [{"hole_id": "PLS-22-08", "collar_id": "c1", "from_depth": 1.0,
                 "to_depth": 2.0, "sample_type": "core", "val": 9.0}]

    async def fetchrow(self, sql: str, *args: Any):
        self.calls.append((sql, args))
        return {"min_v": 1.0, "max_v": 9.0, "mean_v": 5.0, "median_v": 5.0,
                "total_n": 4}


class _Deps:
    workspace_id = "ws-1"
    project_id = "p-1"

    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire_scoped(self):
        yield self._conn


class _Ctx:
    def __init__(self, deps: _Deps) -> None:
        self.deps = deps


@pytest.mark.asyncio
async def test_query_assay_data_maps_gold_and_filters_named_holes():
    from app.agent.tools import query_assay_data

    conn = _Conn()
    result = await query_assay_data(
        _Ctx(_Deps(conn)), "p-1", commodity="gold", hole_ids=["PLS-22-08"],
    )
    assert result.element == "Au_ppb"
    assert result.element_auto_selected is False
    assert result.element_summaries == []
    assert result.hole_filter == ["PLS-22-08"]
    agg_sql, agg_args = next(c for c in conn.calls if "total_n" in c[0])
    assert "ANY($3::text[])" in agg_sql
    assert agg_args[1] == "Au_ppb"
    assert agg_args[2] == ["PLS-22-08"]
    assert agg_args[3] == "ws-1"


@pytest.mark.asyncio
async def test_query_assay_data_without_commodity_returns_summaries():
    from app.agent.tools import query_assay_data

    conn = _Conn()
    result = await query_assay_data(_Ctx(_Deps(conn)), "p-1")
    assert result.element_auto_selected is True
    assert [s.element for s in result.element_summaries] == ["Au_ppb", "U3O8_pct_e"]
    summary_sql, summary_args = next(c for c in conn.calls if "jsonb_each_text" in c[0])
    assert "s.workspace_id = $2" in summary_sql
    assert summary_args == ("p-1", "ws-1")


@pytest.mark.asyncio
async def test_query_assay_data_unassayed_commodity_falls_back_to_summaries():
    from app.agent.tools import query_assay_data

    conn = _Conn()
    result = await query_assay_data(_Ctx(_Deps(conn)), "p-1", commodity="nickel")
    assert result.requested_commodity == "nickel"
    assert result.requested_commodity_unavailable is True
    assert result.element_auto_selected is True
    assert len(result.element_summaries) == 2


@pytest.mark.asyncio
async def test_dispatcher_passes_commodity_and_holes(monkeypatch):
    import app.agent.tools as _tools_mod
    from app.agent.agentic_retrieval.nodes import _call_tool_safely

    seen: dict[str, Any] = {}

    async def fake_assay(ctx, project_id, *, commodity=None, hole_ids=None):
        seen.update(project_id=project_id, commodity=commodity, hole_ids=hole_ids)
        return None

    monkeypatch.setattr(_tools_mod, "query_assay_data", fake_assay)
    await _call_tool_safely(
        "query_assay_data", "top gold assays in hole PLS-22-08", _Deps(_Conn()),
    )
    assert seen == {"project_id": "p-1", "commodity": "gold",
                    "hole_ids": ["PLS-22-08"]}

    await _call_tool_safely(
        "query_assay_data", "compare gold and copper grades", _Deps(_Conn()),
    )
    assert seen["commodity"] is None  # two named -> summaries


# ---------------------------------------------------------------------------
# RAG-18 — budget priority and evidence pruning
# ---------------------------------------------------------------------------


def _over_budget_results():
    # 20 x 5,000-char chunks with scores 0.01..0.20, dispatched BEFORE the
    # assay result — the synthesis order that used to starve it.
    docs = DocumentSearchResult(
        chunks=[_chunk(i, score=(i + 1) / 100) for i in range(20)],
        count=20, data_source="Qdrant",
    )
    return [("search_documents", docs), ("query_assay_data", _assay(50))]


def test_structured_block_survives_a_document_heavy_budget():
    context, rendered = _render_context_and_evidence(_over_budget_results())
    assert "sample_count=50" in context
    assert "context budget reached" in context
    tool_names = [name for name, _ in rendered]
    assert "query_assay_data" in tool_names


def test_dropped_chunks_are_the_lowest_scoring_and_leave_the_evidence():
    original = _over_budget_results()
    context, rendered = _render_context_and_evidence(original)
    docs = next(r for name, r in rendered if name == "search_documents")
    kept_scores = sorted(c.relevance_score for c in docs.chunks)
    assert 0 < len(docs.chunks) < 20
    assert docs.count == len(docs.chunks)
    # Every kept chunk outranks every dropped one.
    dropped = [c for c in original[0][1].chunks if c not in docs.chunks]
    assert min(kept_scores) > max(c.relevance_score for c in dropped)
    # Markers in the prompt are exactly the ids the assembler will emit for
    # the pruned list, and no dropped chunk's text/title reaches the prompt.
    for bundle in assign_citation_ids(rendered):
        for cid in bundle:
            assert cid in context
    for c in dropped:
        assert f"{c.document_title}\n" not in context


def test_everything_fits_returns_the_same_list_object():
    small = [("query_assay_data", _assay(3))]
    context, rendered = _render_context_and_evidence(small)
    assert rendered is small
    assert "context budget reached" not in context


# ---------------------------------------------------------------------------
# AGT-5 — graph-level: assemble's writes are visible to the next node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assemble_writes_reach_the_next_node(monkeypatch):
    from langgraph.graph import END, START, StateGraph

    import app.agent.llm_calls as _llm_mod
    from app.agent.agentic_retrieval.nodes import assemble_node
    from app.agent.agentic_retrieval.retrieval_profile import profile_for_intent
    from app.agent.agentic_retrieval.state import AgenticRetrievalState

    async def fake_call_llm(*args, **kwargs):
        return "Mean U3O8 is 0.4567 [DATA-1]."

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)
    seen: dict[str, Any] = {}

    async def capture(state: AgenticRetrievalState) -> dict[str, Any]:
        seen["system_prompt_tokens_estimate"] = state.system_prompt_tokens_estimate
        seen["tool_results"] = state.tool_results
        return {}

    graph = StateGraph(AgenticRetrievalState)
    graph.add_node("assemble", assemble_node)
    graph.add_node("capture", capture)
    graph.add_edge(START, "assemble")
    graph.add_edge("assemble", "capture")
    graph.add_edge("capture", END)

    class _D:
        project_id = "p"
        workspace_id = "ws"
        pg_pool = None
        redis_client = None

    initial = AgenticRetrievalState(
        query="mean grade?", deps=_D(), intent="synthesis",
        effective_intent="synthesis",
        retrieval_profile=profile_for_intent("synthesis"),
        tool_results=_over_budget_results(),
    )
    await graph.compile().ainvoke(initial)
    assert seen["system_prompt_tokens_estimate"]
    docs = next(r for n, r in seen["tool_results"] if n == "search_documents")
    assert len(docs.chunks) < 20  # the pruned evidence, not the full retrieval


def test_hole_ids_from_query_drops_the_numeric_tail_of_a_named_hole():
    from app.agent.agentic_retrieval.nodes import _hole_ids_from_query

    assert _hole_ids_from_query("assays in hole PLS-22-08") == ["PLS-22-08"]
    ids = _hole_ids_from_query("compare holes BH-1 and BH-12")
    assert "BH-1" in ids and "BH-12" in ids
