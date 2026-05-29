"""Viz payload builders — turn tool results into MapPayload / VizPayload.

M2 Phase 5 visualization contract:

  MapPayload  — always populated when a spatial tool call returned collars.
                Carries a WGS84 GeoJSON FeatureCollection (Point per collar)
                + bounding box for auto-zoom. The frontend CollarMap reads
                this directly without issuing any extra API calls.

  VizPayload  — chart *hints* that tell the frontend which specialised
                component to render:

                  downhole_strip  → <StripLogViewer holeId=...> for a single
                                    hole the user asked about by name.
                  assay_histogram → (reserved, not yet implemented)

                The hint travels as plotly_data/plotly_layout so the existing
                Pydantic schema does not need to change. Frontend inspects
                plotly_layout.meta.hole_id / plotly_layout.meta.kind and
                dispatches to the right React component.

Why hints rather than inline data for strip logs:
  The lithology + survey + assay rollup for a single hole is ~1–5 kB; the
  frontend already has a well-tested StripLogViewer that fetches from
  /api/v1/projects/{id}/collars/{collar_id}. Emitting a hint keeps the
  response small and lets the viewer own its loading/error states.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.agent.tools import (
    AssayDataResult,
    DocumentSearchResult,
    GraphTraversalResult,
    SpatialQueryResult,
)
from app.models.rag import MapPayload, VizPayload

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

# Keywords that strengthen our confidence that a single-hole strip log is the
# right visualization. If the query names a hole AND mentions any of these,
# emit a strip_log hint.
_STRIP_LOG_KEYWORDS = {
    "lithology",
    "litho",
    "log",
    "strip",
    "interval",
    "intersection",
    "intercept",
    "downhole",
    "depth",
    "formation",
    "unit",
    "rock type",
    "core",
    "rqd",
    "recovery",
    "alteration",
    "assay",
}


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


def _query_wants_strip_log(query: str) -> bool:
    lower = query.lower()
    return any(kw in lower for kw in _STRIP_LOG_KEYWORDS)


def build_viz_payload(
    query: str,
    spatial_result: SpatialQueryResult | None,
    document_result: DocumentSearchResult | None = None,
    assay_result: AssayDataResult | None = None,
    graph_result: GraphTraversalResult | None = None,
) -> VizPayload | None:
    """Decide which specialised chart (if any) should render inline with the
    assistant message.

    Current rules:

    1. If the query names exactly one drill hole AND the hole is present in
       the spatial result set AND the query has strip-log intent → emit a
       ``downhole_strip`` VizPayload whose layout carries the hole_id so the
       React StripLogViewer can fetch lithology directly.

    2. Otherwise return None. Histogram / cross-section / 3-D trace are
       reserved for subsequent wedges.

    The ``plotly_data`` list is left empty by design: the frontend maps the
    ``chart_type`` field to a dedicated React component (StripLogViewer) that
    owns its own rendering. ``plotly_layout.meta`` is our extension point for
    passing React props through the Pydantic VizPayload shape without
    breaking the existing contract.
    """
    # --- Rule 1: strip log hint ---
    hole_ids = extract_hole_ids(query)
    if (
        spatial_result is not None
        and hole_ids
        and _query_wants_strip_log(query)
    ):
        collar_ids_by_hole = {
            collar.hole_id.upper(): collar.collar_id
            for collar in spatial_result.collars
        }
        for hole_id in hole_ids:
            collar_id = collar_ids_by_hole.get(hole_id)
            if collar_id is None:
                continue
            logger.info(
                "build_viz_payload: emitting downhole_strip hint hole_id=%s",
                hole_id,
            )
            return VizPayload(
                chart_type="downhole_strip",
                plotly_data=[],
                plotly_layout={
                    "meta": {
                        "kind": "strip_log",
                        "hole_id": hole_id,
                        "collar_id": collar_id,
                    },
                },
                title=f"Strip log — {hole_id}",
            )

    # --- Rule 2: assay histogram with inline Plotly traces ---
    if assay_result is not None and assay_result.count > 0:
        # Build a histogram trace grouped by hole_id for colour coding.
        by_hole: dict[str, list[float]] = {}
        for s in assay_result.samples:
            by_hole.setdefault(s.hole_id, []).append(s.value)

        # Colour palette for holes (amber-toned for dark theme).
        palette = [
            "#f59e0b", "#ef4444", "#22c55e", "#3b82f6", "#a855f7",
            "#ec4899", "#14b8a6", "#f97316", "#6366f1", "#84cc16",
        ]

        traces = []
        for idx, (hid, vals) in enumerate(sorted(by_hole.items())):
            traces.append({
                "x": vals,
                "type": "histogram",
                "name": hid,
                "marker": {"color": palette[idx % len(palette)]},
                "opacity": 0.75,
            })

        # Human-readable element label
        elem_label = assay_result.element.replace("_", " ")

        layout = {
            "barmode": "overlay",
            "title": {"text": f"{elem_label} Grade Distribution", "font": {"color": "#f3f4f6", "size": 14}},
            "xaxis": {"title": {"text": elem_label, "font": {"color": "#9ca3af"}}, "color": "#9ca3af", "gridcolor": "#1f2937"},
            "yaxis": {"title": {"text": "Count", "font": {"color": "#9ca3af"}}, "color": "#9ca3af", "gridcolor": "#1f2937"},
            "plot_bgcolor": "#030712",
            "paper_bgcolor": "#111827",
            "legend": {"font": {"color": "#d1d5db"}, "bgcolor": "rgba(0,0,0,0)"},
            "margin": {"t": 40, "r": 20, "b": 50, "l": 50},
            "meta": {
                "kind": "assay_histogram",
                "element": assay_result.element,
                "sample_count": assay_result.count,
            },
        }

        logger.info(
            "build_viz_payload: emitting assay_histogram element=%s samples=%d",
            assay_result.element,
            assay_result.count,
        )

        return VizPayload(
            chart_type="assay_histogram",
            plotly_data=traces,
            plotly_layout=layout,
            title=f"{elem_label} — {assay_result.count} samples",
        )

    # --- Rule 3: knowledge graph viz (React Flow nodes + edges) ---
    if graph_result is not None and graph_result.count > 0:
        # Build React Flow-compatible nodes and edges from the graph traversal.
        # The start entity is inferred from the relationship directions.
        node_map: dict[str, dict] = {}
        edges: list[dict] = []

        # Entity type → colour mapping
        # Neo4j review — canonical drillhole label is `Drillhole` (lowercase
        # h) per the V1.2 schema migration in `index_neo4j.py`. We keep the
        # legacy `DrillHole` key in here as a fallback so any in-flight
        # responses from a pre-migration cache still render with the right
        # colour, but new graph results land under `Drillhole`.
        type_colors = {
            "Project": "#f59e0b",
            "Drillhole": "#22c55e",
            "DrillHole": "#22c55e",  # legacy fallback — see comment above
            "Formation": "#3b82f6",
            "Report": "#a855f7",
            "QualifiedPerson": "#ec4899",
            "Deposit": "#ef4444",
            "MineralOccurrence": "#14b8a6",
        }

        for ent in graph_result.entities:
            eid = ent.entity_id
            if eid not in node_map:
                etype = ent.entity_type
                node_map[eid] = {
                    "id": eid,
                    "type": "default",
                    "data": {
                        "label": ent.name or etype,
                        "entityType": etype,
                        "color": type_colors.get(etype, "#6b7280"),
                    },
                    "position": {"x": 0, "y": 0},  # frontend auto-layouts
                }

            if ent.relationship_type:
                # Create edge from relationship
                edge_id = f"e-{len(edges)}"
                if ent.relationship_direction == "OUTBOUND":
                    edges.append({
                        "id": edge_id,
                        "source": "center",
                        "target": eid,
                        "label": ent.relationship_type,
                        "animated": True,
                    })
                else:
                    edges.append({
                        "id": edge_id,
                        "source": eid,
                        "target": "center",
                        "label": ent.relationship_type,
                        "animated": True,
                    })

        # Add center node (the queried entity — not in the result set)
        node_map["center"] = {
            "id": "center",
            "type": "default",
            "data": {
                "label": "Query Focus",
                "entityType": "Query",
                "color": "#f59e0b",
            },
            "position": {"x": 0, "y": 0},
        }

        nodes = list(node_map.values())

        logger.info(
            "build_viz_payload: emitting graph_viz nodes=%d edges=%d",
            len(nodes),
            len(edges),
        )

        return VizPayload(
            chart_type="graph_viz",
            plotly_data=[],  # not used — React Flow handles rendering
            plotly_layout={
                "meta": {
                    "kind": "graph_viz",
                    "nodes": nodes,
                    "edges": edges,
                },
            },
            title=f"Knowledge Graph — {len(nodes)} entities",
        )

    # --- Rule 4: cross-section (Plotly 2D) ---
    if spatial_result is not None and spatial_result.count >= 2:
        _xsec_keywords = {"cross-section", "cross section", "section", "fence", "profile"}
        if any(kw in query.lower() for kw in _xsec_keywords):
            # Project all holes onto a W-E line (sort by easting).
            sorted_collars = sorted(
                [c for c in spatial_result.collars if c.longitude is not None],
                key=lambda c: c.easting,
            )
            if len(sorted_collars) >= 2:
                # Compute distance along section from westernmost hole
                origin_e = sorted_collars[0].easting
                distances = [c.easting - origin_e for c in sorted_collars]

                traces = []
                # Collar markers at surface elevation
                traces.append({
                    "type": "scatter",
                    "mode": "markers+text",
                    "name": "Collars",
                    "x": distances,
                    "y": [c.elevation for c in sorted_collars],
                    "text": [c.hole_id for c in sorted_collars],
                    "textposition": "top center",
                    "textfont": {"size": 9, "color": "#d1d5db", "family": "monospace"},
                    "marker": {"size": 8, "color": "#f59e0b", "symbol": "diamond"},
                    "hovertemplate": "<b>%{text}</b><br>Distance: %{x:.0f} m<br>Elev: %{y:.0f} m<extra></extra>",
                })

                # Vertical lines from surface to TD
                for i, c in enumerate(sorted_collars):
                    bottom = c.elevation - c.total_depth
                    traces.append({
                        "type": "scatter",
                        "mode": "lines",
                        "showlegend": False,
                        "x": [distances[i], distances[i]],
                        "y": [c.elevation, bottom],
                        "line": {"color": "#22c55e", "width": 2},
                        "hoverinfo": "skip",
                    })
                    # TD marker
                    traces.append({
                        "type": "scatter",
                        "mode": "markers",
                        "showlegend": False,
                        "x": [distances[i]],
                        "y": [bottom],
                        "marker": {"size": 5, "color": "#ef4444", "symbol": "x"},
                        "hovertemplate": f"{c.hole_id} TD<br>{c.total_depth:.0f} m<extra></extra>",
                    })

                # Surface topography line
                traces.append({
                    "type": "scatter",
                    "mode": "lines",
                    "name": "Surface",
                    "x": distances,
                    "y": [c.elevation for c in sorted_collars],
                    "line": {"color": "#6b7280", "width": 1, "dash": "dot"},
                })

                xsec_layout = {
                    "title": {"text": "W–E Cross Section", "font": {"color": "#f3f4f6", "size": 14}},
                    "xaxis": {"title": {"text": "Distance along section (m)", "font": {"color": "#9ca3af"}}, "color": "#9ca3af", "gridcolor": "#1f2937"},
                    "yaxis": {"title": {"text": "Elevation (m)", "font": {"color": "#9ca3af"}}, "color": "#9ca3af", "gridcolor": "#1f2937"},
                    "plot_bgcolor": "#030712",
                    "paper_bgcolor": "#111827",
                    "legend": {"font": {"color": "#d1d5db"}, "bgcolor": "rgba(0,0,0,0)"},
                    "margin": {"t": 40, "r": 20, "b": 50, "l": 60},
                    "meta": {"kind": "cross_section"},
                }

                logger.info(
                    "build_viz_payload: emitting cross_section holes=%d",
                    len(sorted_collars),
                )
                return VizPayload(
                    chart_type="cross_section",
                    plotly_data=traces,
                    plotly_layout=xsec_layout,
                    title=f"W–E Cross Section — {len(sorted_collars)} holes",
                )

    # --- Rule 5: 3D drill trace ---
    if (
        spatial_result is not None
        and spatial_result.count >= 2
        and not hole_ids  # don't show 3D trace on single-hole queries
        and not _query_wants_strip_log(query)
    ):
        # Check if any holes have survey data by looking at the query keywords
        _3d_keywords = {"3d", "trace", "traces", "trajectory", "trajectories", "deviat"}
        lower_q = query.lower()
        wants_3d = any(kw in lower_q for kw in _3d_keywords)

        if wants_3d:
            # Build collar positions for deck.gl (lat/lon + total_depth)
            collar_data = []
            for c in spatial_result.collars:
                if c.longitude is not None and c.latitude is not None:
                    collar_data.append({
                        "hole_id": c.hole_id,
                        "longitude": c.longitude,
                        "latitude": c.latitude,
                        "elevation": c.elevation,
                        "total_depth": c.total_depth,
                        "hole_type": c.hole_type,
                        "status": c.status,
                        "azimuth": c.azimuth,
                        "dip": c.dip,
                    })

            if collar_data:
                logger.info(
                    "build_viz_payload: emitting drill_trace_3d collars=%d",
                    len(collar_data),
                )
                return VizPayload(
                    chart_type="drill_trace_3d",
                    plotly_data=[],
                    plotly_layout={
                        "meta": {
                            "kind": "drill_trace_3d",
                            "collars": collar_data,
                        },
                    },
                    title=f"3D Drill Traces — {len(collar_data)} holes",
                )

    return None
