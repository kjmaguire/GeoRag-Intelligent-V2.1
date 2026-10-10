"""Audit 2026-10 finding 23: the NUMERIC prompt's summaries block exists.

The NUMERIC system prompt tells the model to "quote the HIGH-CONFIDENCE SUMMARIES
block verbatim" and "do NOT do arithmetic yourself", and the shared preamble says
the same of a "PRE-COMPUTED SUMMARY". The only code that produced either was
``context_builder._build_context``, which has no production caller; the live
structured renderer showed twelve alphabetical rows. So a superlative or an
average ("what is the deepest hole") was computed by the model, from a dozen rows
of a longer list.

``_render_spatial_result`` now emits the summaries, computed in Python over every
matching hole, when the retrieved rows ARE every matching hole, and says plainly
that no such figure exists when they are a LIMIT-capped sample (a "deepest" taken
from a sample describes the sample, and Layer 3 would ground it all the same).
"""

from __future__ import annotations

import pytest

from app.agent import tool_result_helpers
from app.agent.agentic_retrieval.nodes import _render_structured_result
from app.agent.hallucination.orchestrator_validators import verify_numbers
from app.agent.orchestrator import _SYSTEM_PROMPT_NUMERIC, _SYSTEM_PROMPT_NUMERIC_COLON
from app.agent.tools import CollarRecord, SpatialQueryResult


def _collar(i: int, *, depth: float | None = None, hole_type: str = "DD") -> CollarRecord:
    return CollarRecord(
        hole_id=f"H-{i:03d}",
        collar_id=f"c{i}",
        easting=500000.0 + i,
        northing=6000000.0 + i,
        elevation=400.0,
        total_depth=300.0 + i if depth is None else depth,
        hole_type=hole_type,
        azimuth=0.0,
        dip=-90.0,
        status="done",
        drill_date=f"2021-0{1 + i % 9}-15",
        longitude=-108.0 + i / 1000,
        latitude=57.0 + i / 1000,
    )


def _result(n: int, *, total: int | None, **kw: object) -> SpatialQueryResult:
    return SpatialQueryResult(
        collars=[_collar(i) for i in range(n)],
        count=n,
        data_source="PostGIS silver.collars",
        total_count=total,
        **kw,
    )


# ---------------------------------------------------------------------------
# The retrieved rows are every matching hole
# ---------------------------------------------------------------------------


def test_a_complete_set_gets_summaries_computed_over_every_hole() -> None:
    # 30 holes, deepest last alphabetically: it is NOT among the twelve listed.
    text = _render_structured_result(_result(30, total=30))

    assert "HIGH-CONFIDENCE SUMMARIES" in text
    assert "all 30 matching holes" in text
    assert "Total drill holes: 30" in text
    assert "Deepest hole: H-029 at 329.0 m" in text
    assert "Shallowest hole: H-000 at 300.0 m" in text
    assert "Average total_depth: 314.5 metres" in text
    assert "H-029" not in text.split("collars: showing")[1]  # only the summary names it


def test_the_summaries_come_before_the_listing_and_say_what_the_listing_is() -> None:
    text = _render_structured_result(_result(30, total=30))

    assert text.index("HIGH-CONFIDENCE SUMMARIES") < text.index("collars: showing")
    assert text.splitlines()[0].endswith("total_holes_matching=30")
    listing = [line for line in text.splitlines() if line.startswith("collars: showing")][0]
    assert "alphabetical listing; the summaries above cover all 30" in listing


def test_summaries_never_cut_a_listed_row_in_half() -> None:
    text = _render_structured_result(_result(50, total=50))

    assert len(text) <= 4000
    rows = [line for line in text.splitlines() if line.startswith("  CollarRecord(")]
    assert rows, "expected the listing to keep some rows"
    assert all(row.endswith(")") for row in rows), "a row was truncated mid-value"
    shown = int(text.split("collars: showing ")[1].split(" ")[0])
    assert shown == len(rows) <= 12


def test_a_complete_set_with_no_depths_still_renders() -> None:
    result = SpatialQueryResult(
        collars=[_collar(i, depth=None) for i in range(3)],
        count=3,
        data_source="PostGIS silver.collars",
        total_count=3,
    )
    for collar in result.collars:
        collar.total_depth = None

    text = _render_structured_result(result)

    assert "Average total_depth: unknown (no depth values recorded)" in text
    assert "Deepest hole" not in text


# ---------------------------------------------------------------------------
# The retrieved rows are a sample
# ---------------------------------------------------------------------------


def test_a_sample_gets_no_superlatives_and_is_told_so() -> None:
    text = _render_structured_result(_result(50, total=567))

    assert "total_holes_matching=567" in text.splitlines()[0]
    assert "HIGH-CONFIDENCE SUMMARIES" not in text
    for figure in ("Deepest hole", "Shallowest hole", "Average total_depth", "Easternmost"):
        assert figure not in text
    assert "State the total as 567, never as 50" in text
    assert "cannot be determined from the retrieved sample" in text
    assert "collars: showing" in text and "of 567 (alphabetical sample)" in text


def test_completeness_unknown_means_no_summaries() -> None:
    # A result built without the query carries no total_count: nothing says the
    # rows are every hole, so nothing is summarised as if they were.
    text = _render_structured_result(_result(20, total=None))

    assert "HIGH-CONFIDENCE SUMMARIES" not in text
    assert "Deepest hole" not in text
    assert "cannot be determined" not in text  # and no claim that they are a sample
    assert "collars: showing 12 of 20" in text


def test_an_empty_result_has_nothing_to_summarise() -> None:
    text = _render_structured_result(_result(0, total=0))

    assert "HIGH-CONFIDENCE SUMMARIES" not in text
    assert "total_holes_matching=0" in text


# ---------------------------------------------------------------------------
# A summary that cannot be built must not take the answer down
# ---------------------------------------------------------------------------


def test_a_failing_aggregate_leaves_the_block_out_and_the_rows_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_collars: list) -> list[str]:
        raise TypeError("'<' not supported between instances of 'NoneType' and 'str'")

    monkeypatch.setattr(tool_result_helpers, "_build_collar_aggregates", _boom)

    text = _render_structured_result(_result(5, total=5))

    assert "HIGH-CONFIDENCE SUMMARIES" not in text
    assert "collars: showing 5 of 5" in text
    assert "H-004" in text


# ---------------------------------------------------------------------------
# The prompt's promise and the renderer agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt", [_SYSTEM_PROMPT_NUMERIC, _SYSTEM_PROMPT_NUMERIC_COLON], ids=["dash", "colon"]
)
def test_the_block_the_numeric_prompt_names_is_the_block_the_renderer_emits(prompt: str) -> None:
    assert "HIGH-CONFIDENCE SUMMARIES" in prompt
    assert "HIGH-CONFIDENCE SUMMARIES" in _render_structured_result(_result(8, total=8))


# ---------------------------------------------------------------------------
# Layer 3 is untouched, and grounds what the summaries say
# ---------------------------------------------------------------------------


def test_layer_3_grounds_the_values_quoted_from_the_summaries() -> None:
    result = _result(30, total=30)
    tool_results = [("query_spatial_collars", result)]

    quoted = (
        "The deepest hole is H-029 at 329 metres [DATA-1]. "
        "The average total depth is 314.5 metres across 30 holes [DATA-1]."
    )
    invented = "The deepest hole is H-029 at 3290 metres [DATA-1]."

    assert verify_numbers(quoted, tool_results) == []
    assert verify_numbers(invented, tool_results), "the guard must still reject a wrong figure"
