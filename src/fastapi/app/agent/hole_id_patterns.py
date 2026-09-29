"""Drill-hole identifier patterns, shared by everything that must not read
the digits inside a hole name as measurements.

These lived in ``viz_builder`` (which needs them to route a query to a collar
lookup), but Layer 6 needs the same patterns to MASK hole IDs before it scans
an answer for numbers — and importing viz_builder would drag the whole tool
layer into a validator. They are leaf definitions with no dependencies, so
they belong in their own module and both sides import from here.

Formats seen in real projects:

  PLS-20-01, GH08-212, SRE09-12, IC-11, XLS-24-01, DH-2547
      letters, optional embedded digits, then dash-separated digit groups
  0070-4850, 370-4850, 36-1085
      Gas Hills / Cameco style — numeric only, no letter prefix
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Lettered IDs. Safe to match anywhere: the letter prefix makes a false
#: positive on ordinary prose unlikely.
#:
#: TWO letters minimum, deliberately. A single-letter prefix would pull
#: in "Figure A-1", "Table B-2" and "Appendix C-3", which are on nearly
#: every page of an NI 43-101. Layer 4 treats an unmatched hole ID as a
#: fabricated one — the single warning it grades critical on its own — so
#: a false positive there floors the answer's confidence and prints a
#: fabrication banner over a correct answer.
#:
#: Six digits per group rather than five: Layer 4's previous private
#: pattern allowed six, and narrowing a check while widening it
#: elsewhere is how coverage gets lost quietly.
HOLE_ID_RE = re.compile(
    r"\b([A-Z]{2,6}\d{0,4}-\d{1,6}(?:-\d{1,6})?)\b",
    re.IGNORECASE,
)

#: Numeric-only IDs. Bare digit ranges (depth intervals "20-30", page
#: numbers, hole counts) look identical, so callers must gate this on
#: HOLE_CONTEXT_RE matching somewhere in the same text rather than using it
#: unconditionally.
NUMERIC_HOLE_ID_RE = re.compile(
    r"\b(\d{1,4}-\d{1,5}(?:-\d{1,5})?)\b",
)

#: The gate for NUMERIC_HOLE_ID_RE. Deliberately not a tight lookbehind:
#: "this hole please tell me about it, 36-1085" puts the context word 30
#: characters away, and a strict adjacency rule drops the match.
HOLE_CONTEXT_RE = re.compile(
    r"\b(?:hole(?:\s*id)?s?|drill\s*holes?|drillholes?|ddh|borehole)\b",
    re.IGNORECASE,
)


#: Standard and regulatory designations whose digits are a NAME, not a
#: measurement and not a hole: "NI 43-101", "43-101F1", "National Instrument
#: 43-101", "S-K 1300", "JORC Code (2012)" is left alone (the year is a real
#: number a report may state). Every NI 43-101 answer names its standard, and
#: without this mask the digits leaked into three guards at once: Layer 3 read
#: 43 and 101 as numerical claims, Layer 6 tested them against whatever
#: constraint keyword sat nearby, and Layer 4 read "43-101" as a numeric
#: drill-hole ID whenever the answer also said "hole" — the one Layer 4
#: finding graded critical on its own (audit 2026-09-29, RAG-3).
DESIGNATION_RE = re.compile(
    r"\b(?:NI|National\s+Instrument|Form)\s*43-101(?:\s*F1|F1)?\b"
    r"|\b43-101\s*F1\b"
    r"|\bS-?K\s*1300\b",
    re.IGNORECASE,
)

# A numeric candidate immediately followed by one of these is a measured
# interval or quantity ("120-126 m", "50-60 %"), never a hole name.
_UNIT_AFTER_RE = re.compile(
    r"\s*(?:m|metres?|meters?|ft|feet|foot|km|cm|mm|%|g/t|ppm|ppb|°|deg(?:rees?)?)"
    r"(?![A-Za-z0-9/])",
    re.IGNORECASE,
)

# ...or immediately preceded by one of these words: page, figure, table,
# section and item references, and the interval prepositions ("from 120-126",
# "between 20-30"). Deliberately NOT "in" — "mineralisation in 36-1085" is the
# commonest way an answer names a Cameco hole.
_NON_HOLE_PRECEDER_RE = re.compile(
    r"\b(?:ni|instrument|form|pages?|pp|p|figs?|figures?|tables?|sections?|"
    r"items?|appendix|appendices|chapters?|plates?|from|to|between|intervals?|"
    r"depths?|years?)\s*\.?\s*$",
    re.IGNORECASE,
)

# 2011-2014 / 2011-14. Kept only when a hole-context word sits RIGHT before
# it ("hole 2011-14" is a legitimate year-sequence hole name); "hole 36-1085
# was drilled during 2011-2014" puts "hole" inside the ordinary window of a
# plain year range, so year ranges get a much tighter one.
_YEAR_RANGE_RE = re.compile(r"^(?:19|20)\d{2}-(?:(?:19|20)\d{2}|\d{2})$")
_YEAR_RANGE_CONTEXT_WINDOW = 3

#: How far before a numeric candidate a hole-context word may end and still
#: govern it. "holes 36-1085, 36-1042 and 36-1090" puts the third ID ~27
#: characters after "holes".
NUMERIC_HOLE_CONTEXT_WINDOW = 48


@dataclass(frozen=True)
class NumericHoleIdCandidate:
    """One bare-numeric token that may be a drill-hole ID.

    ``near_context`` is True when a hole-context word ("hole", "DDH",
    "borehole", ...) ends within :data:`NUMERIC_HOLE_CONTEXT_WINDOW`
    characters before it. Only near-context candidates are strong enough
    for Layer 4 to call a miss a fabricated hole; a far one is advisory.
    """

    value: str
    start: int
    end: int
    near_context: bool


def find_numeric_hole_ids(text: str) -> list[NumericHoleIdCandidate]:
    """Bare-numeric hole-ID candidates in ``text``, with the obvious
    non-hole shapes removed.

    Gated on HOLE_CONTEXT_RE matching somewhere in the text, exactly as the
    bare NUMERIC_HOLE_ID_RE always was. On top of that gate, a candidate is
    dropped when it is:

    * inside a standard designation ("NI 43-101") — :data:`DESIGNATION_RE`;
    * followed by a length / grade unit ("120-126 m");
    * preceded by a page / figure / table / section reference or an
      interval preposition ("from 120-126", "pages 12-14");
    * a year range with no hole word governing it ("2011-2014").

    Those were all reported by Layer 4 as critical fabricated drill holes on
    correct answers (RAG-3). A real numeric hole — "hole 36-1085" — passes
    every rule and is still checked.
    """
    if not HOLE_CONTEXT_RE.search(text):
        return []

    masked = [(m.start(), m.end()) for m in DESIGNATION_RE.finditer(text)]
    contexts = [m.end() for m in HOLE_CONTEXT_RE.finditer(text)]

    out: list[NumericHoleIdCandidate] = []
    for m in NUMERIC_HOLE_ID_RE.finditer(text):
        start, end = m.start(1), m.end(1)
        if any(ms < end and start < me for ms, me in masked):
            continue
        # Part of a decimal or a longer token ("145.2-148.0", "12-3a").
        if start > 0 and text[start - 1] in ".,":
            continue
        if end < len(text) and text[end] in ".," and text[end + 1: end + 2].isdigit():
            continue
        if _UNIT_AFTER_RE.match(text, end):
            continue
        if _NON_HOLE_PRECEDER_RE.search(text[max(0, start - 20):start]):
            continue
        value = m.group(1)
        near = any(0 <= start - c <= NUMERIC_HOLE_CONTEXT_WINDOW for c in contexts)
        if _YEAR_RANGE_RE.match(value) and not any(
            0 <= start - c <= _YEAR_RANGE_CONTEXT_WINDOW for c in contexts
        ):
            continue
        out.append(NumericHoleIdCandidate(value, start, end, near))
    return out


_CANONICAL_SEPARATORS_RE = re.compile(r"[\s\-_./]+")


def canonical_hole_id(hole_id: str) -> str:
    """Separator-free, upper-cased form of a hole ID ("BH-12" -> "BH12").

    The same rule the ingest side writes into
    ``silver.collars.hole_id_canonical`` (las_ingester._canonical_hole_id,
    csv_collar_ingester.canonicalize), so "BH-12", "bh12" and "BH 12" are one
    hole. Leading zeros are deliberately NOT stripped: "BH-1" and "BH-01"
    may be different holes, and a guard must not merge two real holes.
    """
    return _CANONICAL_SEPARATORS_RE.sub("", (hole_id or "").strip()).upper()
