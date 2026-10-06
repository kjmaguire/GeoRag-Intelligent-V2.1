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


def test_summaries_precede_records():
    """
    B4 invariant — the HIGH-CONFIDENCE SUMMARIES zone is emitted before the
    RAW RECORDS zone, even though the spatial tool's own record listing is
    appended first in dispatch order.
    """
    from app.agent.tools import CollarRecord, SpatialQueryResult

    collar = CollarRecord(
        hole_id="PLS-22-08",
        collar_id="c1",
        easting=1.0,
        northing=2.0,
        elevation=3.0,
        total_depth=510.0,
        hole_type="DD",
        azimuth=0.0,
        dip=-90.0,
        status="completed",
        drill_date=None,
    )
    spatial = SpatialQueryResult(collars=[collar, collar], count=2, data_source="test")

    packed = _build_context([("query_spatial_collars", spatial)])

    summary_pos = packed.find("=== HIGH-CONFIDENCE SUMMARIES")
    records_pos = packed.find("Spatial query returned")
    assert summary_pos >= 0, "summary zone missing"
    assert records_pos >= 0, "records zone missing"
    assert summary_pos < records_pos, "B4 invariant violated: records appeared before summaries"


def test_empty_tool_results_returns_noop_marker():
    assert _build_context([]) == "(no data retrieved)"
