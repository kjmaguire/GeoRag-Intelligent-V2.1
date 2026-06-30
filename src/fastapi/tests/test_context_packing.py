"""B4 — context packing order + MMR diversity."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.orchestrator import _build_context, _mmr_select_chunks

# ── Fakes matching the ToolResult shapes _build_context discriminates on ──


@dataclass
class _Chunk:
    document_title: str
    section_number: str | None = None
    section_title: str | None = None
    text: str = ""
    relevance_score: float = 0.0


@dataclass
class _FakeDocumentSearchResult:
    chunks: list[_Chunk] = field(default_factory=list)
    count: int = 0


# ── MMR ──────────────────────────────────────────────────────────────────


def test_mmr_removes_near_duplicates():
    """Two near-identical paragraphs should not both survive MMR."""
    chunks = [
        _Chunk("Report A", "13", "Resources", "Mineral resource estimate of 120 Mt at 0.18 percent uranium", 0.95),
        _Chunk("Report B", "13", "Resources", "Mineral resource estimate of 120 Mt at 0.18 percent uranium", 0.94),
        _Chunk("Report C", "7",  "Geology",   "The deposit sits within Athabasca basin sandstones overlying basement", 0.80),
        _Chunk("Report D", "11", "Drilling",  "Drill programme comprised 45 diamond holes totalling 18 kilometres", 0.75),
    ]
    selected = _mmr_select_chunks(chunks, lambda_weight=0.7, k=3)
    assert len(selected) == 3
    # The two near-identical report chunks should not both be present.
    titles = [c.document_title for c in selected]
    assert not (("Report A" in titles) and ("Report B" in titles))


def test_mmr_runs_on_small_result_sets():
    """P1 #17 — old behaviour skipped MMR for <3 chunks. New contract:
    MMR runs on any non-empty list; with 1 chunk it returns that chunk;
    with 2 it returns both (most-relevant first, then the other) so
    the diversity term is still applied even on thin retrieval.
    """
    # Empty input → empty output (no crash).
    assert _mmr_select_chunks([]) == []

    # Single chunk → single chunk back, unchanged.
    one = [_Chunk("A", text="foo", relevance_score=0.9)]
    assert _mmr_select_chunks(one) == one

    # Two distinct chunks → both returned; higher-relevance leads.
    two = [
        _Chunk("low",  text="alpha bravo charlie", relevance_score=0.4),
        _Chunk("high", text="delta echo foxtrot",  relevance_score=0.9),
    ]
    selected = _mmr_select_chunks(two)
    assert len(selected) == 2
    assert selected[0].document_title == "high"  # MMR seeds with top-relevance
    assert selected[1].document_title == "low"


# ── _build_context ordering ──────────────────────────────────────────────


def test_summaries_precede_records_which_precede_graph():
    """
    B4 invariant — SUMMARY zone first, RECORDS zone middle, GRAPH zone last.
    Even when tool dispatch order would naturally put graph ahead of docs,
    the final packed prompt keeps graph at the tail.
    """
    from app.agent.tools import (
        DocumentSearchResult,
        GraphEntity,
        GraphTraversalResult,
    )

    # Construct a minimal DocumentSearchResult with real chunk shape.
    doc_result = DocumentSearchResult(
        chunks=[],  # type: ignore[arg-type]
        count=0,
        data_source="test",
    )

    # A graph result dispatched FIRST in the tool order.
    graph_result = GraphTraversalResult(
        entities=[
            GraphEntity(
                entity_id="e1",
                entity_type="Formation",
                name="Athabasca SST",
                properties={"age": "Proterozoic"},
                relationship_type="HOSTS",
                relationship_direction="OUTBOUND",
            ),
        ],
        count=1,
        data_source="Neo4j",
    )

    tool_results = [
        ("traverse_knowledge_graph", graph_result),  # dispatched first
        ("search_documents", doc_result),
    ]

    packed = _build_context(tool_results)
    # Document-search record block must appear BEFORE the graph block,
    # regardless of tool_results order.
    doc_pos = packed.find("Document search returned")
    graph_pos = packed.find("Knowledge graph returned")
    assert doc_pos >= 0, "document section missing"
    assert graph_pos >= 0, "graph section missing"
    assert doc_pos < graph_pos, (
        "B4 invariant violated: graph section appeared before records"
    )


def test_empty_tool_results_returns_noop_marker():
    assert _build_context([]) == "(no data retrieved)"
