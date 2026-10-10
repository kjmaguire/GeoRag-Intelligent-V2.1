"""Drill-hole-ID extraction for the query path.

Home to extract_hole_ids(), the drill-hole-ID regex extractor used by the
agentic-retrieval intent classifier, the query classifier and the multi-turn
resolver. The MapPayload is built in
``app.agent.agentic_retrieval.nodes._build_chat_card_payloads``.

This module used to also build MapPayload (``build_map_payload``) and
VizPayload chart hints (``build_viz_payload``, including a graph_viz branch
keyed on a Neo4j GraphTraversalResult). Neither had a caller anywhere in the
live pipeline, so both were removed (build_viz_payload 2026-07-31,
build_map_payload in the 2026 full code review).
"""

from __future__ import annotations

from app.agent.hole_id_patterns import (
    DESIGNATION_RE,
    find_numeric_hole_ids,
    hole_id_key,
    iter_compact_hole_id_matches,
    iter_hole_id_matches,
)

# Drill-hole ID patterns seen so far in the GeoRAG corpus:
#   PLS-20-01, PLS-22-08       (Patterson Lake South — letters + 2-group digits)
#   DH-2547, IC-11             (generic diamond / IC — 2-letter prefix)
#   XLS-24-01                  (Excel import prefix)
#   GH08-212, SB12-001         (Wyoming historical — letters + embedded year digits, then dash + sequence)
#   SRE09-12                   (WSGS SRE — letters + embedded year digits, then dash + sequence)
#   36-1085, 36-1042           (Cameco Shirley Basin — section-sequence, no letter prefix)
#   3774-36-1458               (Wyoming historical — three numeric groups)
#   0070-4850, 370-4850        (Gas Hills — two numeric groups, no letter prefix)
#   BH21, DDH0023, SRE0912     (compact — letters and digits, no separator)
# Lettered patterns are matched anywhere in the query, bar the words that have
# their shape ("Pre-2010", "Zone-3", "Pb-206"); compact ones need a hole word in
# front or a drill-type prefix; numeric-only patterns REQUIRE a context word
# (hole/drillhole/etc) and are not a designation, an interval, a page reference
# or a year range, so depth ranges ("20-30 m") and counts ("36 holes") do not
# false-positive.

# Defined in app.agent.hole_id_patterns so Layer 4 and Layer 6 read the same
# shapes without importing this module (and with it the whole tool layer).


def extract_hole_ids(query: str) -> list[str]:
    """Return every drill-hole ID mentioned in the query (upper-cased, de-duped).

    Combines three patterns, each with the same exclusions the answer-side
    check (Layer 4) applies -- the two used to disagree, and a bogus ID here
    filters an assay query to zero rows and forces the factual_lookup intent:
      1. Lettered (PLS-20-01, DH-2547, XLS-24-09) — matched anywhere, except
         the shapes that are words ("pre-2010", "Zone-3", "Oct-2011") or
         isotopes ("Pb-206"); see ``hole_id_patterns.iter_hole_id_matches``.
      2. Compact (BH21, DDH0023, SRE0912) — only with a drill-type prefix or a
         hole word shortly in front, so "NI43" and "WGS84" are not holes.
      3. Numeric-only (36-1085, 99-001) — only when a drill-hole context word
         ("hole", "drillhole", "DDH", "borehole", "hole id") appears somewhere
         in the same query (a deliberate loosening of the earlier inline
         adjacency lookbehind, so "this hole please tell me about it,
         36-1085" still matches while bare digit pairs ("show me data for
         36-1085") still skip), and never a standard ("NI 43-101"), an
         interval ("120-126 m", "between 20-30"), a page / figure reference or
         a year range ("2011-2014") -- ``hole_id_patterns.find_numeric_hole_ids``.
    """
    if not query:
        return []
    # "NI 43-101" is a name, not a hole: mask it before anything reads it.
    masked = DESIGNATION_RE.sub(" ", query)

    seen: set[str] = set()
    keys: set[str] = set()
    ordered: list[str] = []

    def _add(raw: str) -> None:
        normalised = raw.upper()
        if normalised not in seen:
            seen.add(normalised)
            ordered.append(normalised)
            keys.add(hole_id_key(normalised))

    for match in iter_hole_id_matches(masked):
        _add(match.group(1))
    for match in iter_compact_hole_id_matches(masked):
        # "BH21" next to "BH-21" is one hole, said twice.
        if hole_id_key(match.group(1)) not in keys:
            _add(match.group(1))
    for candidate in find_numeric_hole_ids(masked):
        _add(candidate.value)

    return ordered
