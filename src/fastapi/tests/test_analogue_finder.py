"""§9.8 Analogue Finder Agent tests (Phase H4)."""
from __future__ import annotations

import asyncio

from app.agents.phase9.analogue_finder import (
    _embedding_similarity,
    _graph_path_similarity,
    analogue_finder,
)


def _run(project_attributes, top_k=10):
    inner = getattr(analogue_finder, "__wrapped__", analogue_finder)
    return asyncio.run(inner(
        ctx=None,
        workspace_id="ws-1",
        target_model_id="tm-1",
        project_attributes=project_attributes,
        top_k=top_k,
    ))


def test_athabasca_attributes_match_athabasca_deposits() -> None:
    result = _run({
        "deposit_model": "unconformity_uranium",
        "host_rocks":    ["unconformity", "basement_graphitic"],
        "commodities":   ["uranium"],
        "tectonic_setting": "athabasca",
    })
    names = [a["deposit_name"] for a in result["analogues"]]
    # McArthur River + Cigar Lake should be at the top
    assert "McArthur River" in names[:3]
    assert "Cigar Lake" in names[:3]


def test_graph_path_similarity_with_matches() -> None:
    sim, matched = _graph_path_similarity(
        {"deposit_model": "porphyry", "commodity": "copper"},
        {"attributes": {"porphyry", "copper", "molybdenum"}},
    )
    assert sim > 0
    assert set(matched) == {"porphyry", "copper"}


def test_graph_path_similarity_no_overlap_returns_zero() -> None:
    sim, matched = _graph_path_similarity(
        {"x": "foo"}, {"attributes": {"bar"}},
    )
    assert sim == 0
    assert matched == []


def test_embedding_similarity_lexical_overlap() -> None:
    sim = _embedding_similarity(
        "unconformity uranium basement",
        {"description": "unconformity-style uranium basement contact"},
    )
    assert 0 < sim < 1


def test_embedding_similarity_empty_returns_zero() -> None:
    assert _embedding_similarity("", {"description": "anything"}) == 0


def test_top_k_respected() -> None:
    result = _run({
        "host_rocks":  ["gold", "copper", "uranium"],
        "commodities": ["copper", "gold", "nickel"],
    }, top_k=3)
    assert len(result["analogues"]) <= 3


def test_analogues_sorted_by_combined_score_desc() -> None:
    result = _run({
        "host_rocks":  ["porphyry", "copper", "molybdenum"],
        "commodities": ["copper", "gold"],
    })
    scores = [a["combined_score"] for a in result["analogues"]]
    assert scores == sorted(scores, reverse=True)


def test_each_result_has_required_fields() -> None:
    result = _run({
        "host_rocks":  ["unconformity", "basement_graphitic"],
        "commodities": ["uranium"],
    })
    for a in result["analogues"]:
        for key in (
            "deposit_name", "location", "commodity", "deposit_model",
            "embedding_similarity", "graph_path_similarity",
            "combined_score", "matched_attributes", "evidence_chunk_ids",
        ):
            assert key in a


def test_no_attributes_returns_empty() -> None:
    result = _run({})
    assert result["analogues"] == []


def test_summary_carries_channel_tag() -> None:
    result = _run({"commodities": ["uranium"]})
    assert "channel=in_memory" in result["summary"]
