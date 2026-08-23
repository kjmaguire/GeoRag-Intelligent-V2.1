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
