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
    HOLE_CONTEXT_RE,
    HOLE_ID_RE,
    NUMERIC_HOLE_ID_RE,
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
# Lettered patterns are matched anywhere in the query; numeric-only patterns
# REQUIRE a context word (hole/drillhole/etc) so depth ranges ("20-30 m")
# and counts ("36 holes") do not false-positive.

# Defined in app.agent.hole_id_patterns so Layer 6 can mask hole IDs before
# it scans an answer for numbers without importing this module (and with it
# the whole tool layer). Aliased to the historical private names so the call
# sites below — and any test that patches them — keep working.
_HOLE_ID_RE = HOLE_ID_RE
_NUMERIC_HOLE_ID_RE = NUMERIC_HOLE_ID_RE
_HOLE_CONTEXT_RE = HOLE_CONTEXT_RE


def extract_hole_ids(query: str) -> list[str]:
    """Return every drill-hole ID mentioned in the query (upper-cased, de-duped).

    Combines two patterns:
      1. Lettered (PLS-20-01, DH-2547, XLS-24-09) — matched anywhere; the
         alpha-num shape itself rejects depth-range / page-number false
         positives.
      2. Numeric-only (36-1085, 99-001) — matched anywhere in the query,
         but ONLY when a drill-hole context word ("hole", "drillhole",
         "DDH", "borehole", "hole id") appears somewhere in the same query.
         This is a deliberate loosening of the earlier inline-adjacency
         lookbehind ("hole 36-1085") so phrasings like "this hole please
         tell me about it, 36-1085" still match while bare digit pairs
         ("show me data for 36-1085") still skip.
    """
    seen: set[str] = set()
    ordered: list[str] = []

    # Lettered IDs always run — the pattern itself is specific enough.
    for raw in _HOLE_ID_RE.findall(query):
        normalised = raw.upper()
        if normalised not in seen:
            seen.add(normalised)
            ordered.append(normalised)

    # Numeric-only IDs only when a hole context word appears in the query.
    if query and _HOLE_CONTEXT_RE.search(query):
        for raw in _NUMERIC_HOLE_ID_RE.findall(query):
            normalised = raw.upper()
            if normalised not in seen:
                seen.add(normalised)
                ordered.append(normalised)

    return ordered
