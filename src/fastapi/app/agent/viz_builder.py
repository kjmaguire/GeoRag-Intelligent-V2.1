"""Viz payload builders — turn tool results into MapPayload.

MapPayload is always populated when a spatial tool call returned collars.
Carries a WGS84 GeoJSON FeatureCollection (Point per collar) + bounding box
for auto-zoom. The frontend CollarMap reads this directly without issuing
any extra API calls.

Also home to extract_hole_ids(), the drill-hole-ID regex extractor used by
the agentic-retrieval intent classifier and multi-turn resolver.

This module used to also build VizPayload chart hints (build_viz_payload,
including a graph_viz branch keyed on a Neo4j GraphTraversalResult), but
that function had zero callers anywhere in the live pipeline — the real
chart types (coverage_table, assay_histogram, cross_section, etc.) are
built by app.services.visualizations instead. Removed 2026-07-31 rather
than left to bit-rot alongside the Neo4j-backed frontend KnowledgeGraph
component it was the only possible source for.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.agent.tools import SpatialQueryResult
from app.models.rag import MapPayload

logger = logging.getLogger(__name__)


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

# (1) Letters then optional embedded digits, then dash + digit groups.
#     Covers PLS-20-01, GH08-212, SRE09-12, IC-11, XLS-24-01, DH-2547.
_HOLE_ID_RE = re.compile(
    r"\b([A-Z]{2,6}\d{0,4}-\d{1,5}(?:-\d{1,5})?)\b",
    re.IGNORECASE,
)

# (2) Numeric-only IDs — 2 or 3 groups separated by dashes. Bare digit
#     ranges (depth intervals, page numbers, hole counts) would otherwise
#     false-positive, so we gate the entire pattern on the presence of a
#     drill-hole context word *anywhere* in the query (not as a tight
#     lookbehind). Kyle's "this hole please tell me about it, 36-1085"
#     places "hole" 30 chars before the digit run; a strict adjacency
#     lookbehind drops the match and the orchestrator can't route to a
#     collar lookup.
_NUMERIC_HOLE_ID_RE = re.compile(
    r"\b(\d{1,4}-\d{1,5}(?:-\d{1,5})?)\b",
)
_HOLE_CONTEXT_RE = re.compile(
    r"\b(?:hole(?:\s*id)?s?|drill\s*holes?|drillholes?|ddh|borehole)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# MapPayload builder
# ---------------------------------------------------------------------------


def build_map_payload(spatial_result: SpatialQueryResult | None) -> MapPayload | None:
    """Build a MapPayload from a SpatialQueryResult.

    Returns None when:
      - spatial_result is None (spatial tool was not called)
      - spatial_result has zero collars
      - No collars have valid longitude/latitude (fallback would crash the map)

    All valid rows are emitted as Point features. Collars without WGS84 coords
    are dropped from the feature collection — they cannot be rendered on
    MapLibre without a projection fallback and the frontend does not currently
    handle UTM. Future work: add an EPSG hint + client-side proj4 conversion.
    """
    if spatial_result is None or spatial_result.count == 0:
        return None

    features: list[dict[str, Any]] = []
    lons: list[float] = []
    lats: list[float] = []

    for collar in spatial_result.collars:
        if collar.longitude is None or collar.latitude is None:
            continue

        lons.append(collar.longitude)
        lats.append(collar.latitude)

        features.append(
            {
                "type": "Feature",
                "id": collar.collar_id,
                "geometry": {
                    "type": "Point",
                    "coordinates": [collar.longitude, collar.latitude],
                },
                "properties": {
                    "collar_id": collar.collar_id,
                    "hole_id": collar.hole_id,
                    "easting": collar.easting,
                    "northing": collar.northing,
                    "elevation": collar.elevation,
                    "total_depth": collar.total_depth,
                    "hole_type": collar.hole_type,
                    "azimuth": collar.azimuth,
                    "dip": collar.dip,
                    "status": collar.status,
                    "drill_date": collar.drill_date,
                },
            }
        )

    if not features:
        logger.info(
            "build_map_payload: %d collars had no WGS84 coords, returning None",
            spatial_result.count,
        )
        return None

    # Pad the bbox very slightly so points on the edge are not clipped.
    pad = 0.002
    bbox = (
        min(lons) - pad,
        min(lats) - pad,
        max(lons) + pad,
        max(lats) + pad,
    )

    return MapPayload(
        layer_id="spatial_collars",
        layer_type="collar",
        geojson={
            "type": "FeatureCollection",
            "features": features,
        },
        bbox=bbox,
        label=f"Drill collars ({len(features)})",
    )


# ---------------------------------------------------------------------------
# VizPayload builder
# ---------------------------------------------------------------------------


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

