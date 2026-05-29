"""Map/Chart Planner Agent (§7.5 / §15.4).

Decides which maps + charts each section needs, then RECORDS the
plan for the §5 renderer endpoints (strip_log, cross_section,
stereonet, target_heatmap, etc.) to execute downstream. Enforces the
§17.4 chart export contract: every produced chart carries the 6
required metadata fields.

Phase H4 graduation — the planner now emits a structured plan
(kind, target endpoint, params, exhibit_id, chart_export_metadata)
that the §7.1 graph downstream can consume to either:

  a) call the §5 router directly via HTTPX
  b) enqueue an outbox row pointing at the §5 endpoint

The plan is RECORDED, not yet RENDERED. The actual rendering call
sits behind a feature flag so the planner is safe to run in
dry/test mode against synthetic state. When the orchestrator hooks
up the render call, the contract above is preserved.

Output contract — see module docstring.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from app.agents import AgentContext, georag_agent


logger = logging.getLogger(__name__)


# Map / chart kind → (endpoint, payload_kind). Used to point the
# renderer at the right §5 surface. Add a new kind here when the
# renderer ships a new surface.
_MAP_KIND_ROUTER: dict[str, tuple[str, str]] = {
    "project_aoi":        ("/v1/viz/project_aoi",       "geojson"),
    "collar_map":         ("/v1/viz/collar_map",        "geojson"),
    "target_overview":    ("/v1/viz/target_heatmap",    "png"),
    "target_heatmap":     ("/v1/viz/target_heatmap",    "png"),
    "bedrock_overlay":    ("/v1/viz/pg_overlay",        "png"),
    "pg_overlay":         ("/v1/viz/pg_overlay",        "png"),
    "activity_heatmap":   ("/v1/viz/activity_heatmap",  "png"),
}

_CHART_KIND_ROUTER: dict[str, tuple[str, str]] = {
    "stereonet":          ("/v1/viz/stereonet",          "png"),
    "strip_log":          ("/v1/viz/strip_log",          "png"),
    "cross_section":      ("/v1/viz/cross_section",      "png"),
    "score_ranking":      ("/v1/viz/score_ranking",      "plotly"),
    "grade_histogram":    ("/v1/viz/grade_histogram",    "plotly"),
    "parse_quality_bars": ("/v1/viz/parse_quality_bars", "plotly"),
    "pass_rate_trend":    ("/v1/viz/pass_rate_trend",    "plotly"),
    "throughput_timeline":("/v1/viz/throughput_timeline","plotly"),
    "uncertainty_bars":   ("/v1/viz/uncertainty_bars",   "plotly"),
    "new_doc_timeline":   ("/v1/viz/new_doc_timeline",   "plotly"),
    "score_delta":        ("/v1/viz/score_delta",        "plotly"),
}


def _chart_export_payload(
    *,
    kind: str,
    source_data: str,
    method: str,
    filters: dict[str, Any],
    crs: str,
    citations: list[str],
    confidence_warnings: list[str],
) -> dict[str, Any]:
    """The 6-field §17.4 chart export contract."""
    return {
        "source_data":         source_data,
        "method":              method,
        "filters":             filters,
        "crs":                 crs,
        "citations":           citations,
        "confidence_warnings": confidence_warnings,
    }


@georag_agent(
    name="Map/Chart Planner Agent",
    risk_tier="R2",  # Writes rendered artifacts to SeaweedFS
    version="1.0.0",  # graduated Phase H4
)
async def map_chart_planner(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    project_id: UUID | str,
    section_id: str,
    pending_map_kinds: list[str],
    pending_chart_kinds: list[str],
    citations_per_kind: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Plan the maps/charts for one section.

    Args:
        workspace_id / project_id: routed into chart filters so the
            renderer scopes the data.
        section_id: target section.
        pending_map_kinds: list of kinds the planner read off the
            section plan.
        pending_chart_kinds: list of kinds for charts.
        citations_per_kind: optional citation chunk IDs per kind so
            the §17.4 export payload's `citations` field is populated.

    Returns:
        The plan. Each entry has ``exhibit_id`` for downstream
        sign-off + §29 export reference. ``uri`` is empty until the
        downstream renderer call lands it; ``metadata`` is the
        complete §17.4 ChartExportPayload.
    """
    citations_per_kind = citations_per_kind or {}
    ws_str = str(workspace_id)
    proj_str = str(project_id)

    maps: list[dict[str, Any]] = []
    for kind in pending_map_kinds:
        if kind not in _MAP_KIND_ROUTER:
            logger.warning(
                "map_chart_planner: unknown map kind %r — skipping", kind,
            )
            continue
        endpoint, payload_kind = _MAP_KIND_ROUTER[kind]
        maps.append({
            "kind":        kind,
            "uri":         "",  # filled by the renderer call later
            "exhibit_id":  f"MAP-{section_id}-{kind}-{uuid4().hex[:6]}",
            "endpoint":    endpoint,
            "payload_kind": payload_kind,
            "params":      {
                "workspace_id": ws_str,
                "project_id":   proj_str,
                "format":       payload_kind,
            },
            "metadata":    _chart_export_payload(
                kind=kind,
                source_data=f"workspace={ws_str} project={proj_str}",
                method=f"§5 {endpoint} renderer ({payload_kind})",
                filters={"workspace_id": ws_str, "project_id": proj_str},
                crs="EPSG:4326",
                citations=citations_per_kind.get(kind, []),
                confidence_warnings=[],
            ),
        })

    charts: list[dict[str, Any]] = []
    for kind in pending_chart_kinds:
        if kind not in _CHART_KIND_ROUTER:
            logger.warning(
                "map_chart_planner: unknown chart kind %r — skipping", kind,
            )
            continue
        endpoint, payload_kind = _CHART_KIND_ROUTER[kind]
        charts.append({
            "kind":        kind,
            "uri":         "",
            "exhibit_id":  f"CHART-{section_id}-{kind}-{uuid4().hex[:6]}",
            "endpoint":    endpoint,
            "payload_kind": payload_kind,
            "params":      {
                "workspace_id": ws_str,
                "project_id":   proj_str,
                "format":       payload_kind,
            },
            "metadata":    _chart_export_payload(
                kind=kind,
                source_data=f"workspace={ws_str} project={proj_str}",
                method=f"§5 {endpoint} renderer ({payload_kind})",
                filters={"workspace_id": ws_str, "project_id": proj_str},
                crs="EPSG:4326",
                citations=citations_per_kind.get(kind, []),
                confidence_warnings=[],
            ),
        })

    summary = (
        f"section={section_id} maps_planned={len(maps)} "
        f"charts_planned={len(charts)} "
        f"unknown_kinds={(len(pending_map_kinds) - len(maps)) + (len(pending_chart_kinds) - len(charts))}"
    )
    logger.info("map_chart_planner: %s", summary)

    return {
        "section_id": section_id,
        "maps":       maps,
        "charts":     charts,
        "summary":    summary,
        "planned_at": datetime.now(timezone.utc).isoformat(),
    }


__all__ = ["map_chart_planner"]
