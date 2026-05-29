"""extract_hole_ids — relaxed numeric-ID adjacency rule.

Regression test for Kyle's "this hole please tell me about it, 36-1085"
query (2026-05-25). The earlier inline lookbehind required a context
word immediately before the digit run, so phrasings that mentioned the
hole 30 characters later returned []. The fix loosens the rule to a
sentence-level context-word check while keeping bare digit pairs
(depth ranges, page numbers) from false-firing.
"""

from __future__ import annotations

import pytest

from app.agent.viz_builder import extract_hole_ids

# ---------------------------------------------------------------------------
# Cases that SHOULD match the numeric pattern (context word somewhere)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        # Kyle's failing query 2026-05-25 — context word 30 chars before id.
        ("this hole please tell me about it, 36-1085", ["36-1085"]),
        # Standard adjacency still works.
        ("tell me about hole 36-1085", ["36-1085"]),
        # Borehole context word.
        ("show me borehole 36-1085 details", ["36-1085"]),
        # DDH context.
        ("what is the depth of DDH 36-1085?", ["36-1085"]),
        # Drillhole as one word.
        ("drillhole 36-1085 please", ["36-1085"]),
        # Hole id phrasing.
        ("the hole id is 36-1085", ["36-1085"]),
        # 3-group numeric IDs (Wyoming historical).
        ("look up hole 3774-36-1458", ["3774-36-1458"]),
        # Multiple holes.
        ("compare hole 36-1085 with hole 36-1042", ["36-1085", "36-1042"]),
    ],
)
def test_numeric_hole_ids_match_with_context_word(
    query: str, expected: list[str]
) -> None:
    assert extract_hole_ids(query) == expected


# ---------------------------------------------------------------------------
# Cases that should NOT match (no context word → bare-digit guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        # The contractually-required no-context case.
        "show me data for 36-1085",
        # Depth range.
        "intervals from 20-30 metres",
        # Page numbers.
        "see pages 100-150 of the report",
        # Year ranges.
        "data collected from 2010-2020",
        # Bare digit pair with no hole word anywhere.
        "36-1085",
        # Empty query.
        "",
    ],
)
def test_numeric_hole_ids_skip_without_context_word(query: str) -> None:
    assert extract_hole_ids(query) == []


# ---------------------------------------------------------------------------
# Lettered IDs always match (the alpha-num shape is its own context)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        ("PLS-22-08 anything you have", ["PLS-22-08"]),
        ("data for DH-2547 please", ["DH-2547"]),
        ("lower-case pls-20-01 still matches", ["PLS-20-01"]),
    ],
)
def test_lettered_hole_ids_always_match(
    query: str, expected: list[str]
) -> None:
    assert extract_hole_ids(query) == expected


def test_dedup_and_order_preserved() -> None:
    """Duplicate mentions deduplicate; first-seen ordering preserved."""
    assert extract_hole_ids(
        "hole 36-1085 then hole 36-1042 then hole 36-1085 again"
    ) == ["36-1085", "36-1042"]
