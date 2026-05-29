"""Visualizations router (§5 — drillhole strip log, cross-section, stereonet).

Endpoints
---------
GET /v1/viz/strip_log?collar_id=<uuid>&format=<json|png>
    Returns either an interactive Plotly figure dict (JSON) or a static
    PNG render of the strip log for one drillhole.

GET /v1/viz/cross_section?project_id=<uuid>&section_line_id=<uuid>&format=<json|png>
    Returns a vertical cross-section panel pre-projected onto the
    requested section line.

GET /v1/viz/stereonet?project_id=<uuid>&format=<json|png>
    Returns a stereonet projection of structural measurements (foliations,
    bedding, faults, joints).

doc-phase 186 — Phase H4 §5 strip-log API wire-up.

Auth
----
Same pattern as ``evidence.py``: service-key + workspace-id JWT context.
Workspace scope is enforced via RLS GUC at the asyncpg connection level —
queries for collars / sections / structures outside the caller's workspace
return empty result sets, which the renderer turns into a graceful
"no data" figure.

Performance target: ≤500 ms p95 (renderer is pure-function; only one
PG round-trip per request).
"""
from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app.services.auth import verify_service_key
from app.services.visualizations import (
    CrossSectionPanel,
    StereonetPoint,
    StripLogInterval,
    render_cross_section_matplotlib_png,
    render_cross_section_plotly_figure,
    render_stereonet_matplotlib_png,
    render_stereonet_plotly_figure,
    render_strip_log_matplotlib_png,
    render_strip_log_plotly_figure,
)
from app.services.workspace_resolution import resolve_workspace_id

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/v1/viz",
    tags=["visualizations"],
    dependencies=[Depends(verify_service_key)],
)


# ----------------------------------------------------------------------------
# §17.4 chart-export contract envelope
# ----------------------------------------------------------------------------
#
# Each chart-producing endpoint can opt into the §17.4 export-contract envelope
# via the `with_export_metadata=true` query param. The envelope shape is fixed
# per docs/chart_export_contract_spec.md:
#
#   {
#     "chart": {"type": ..., "format": ..., "content": ...},
#     "export_metadata": {
#       "source_data": {"gold_tables": [...], "row_count": N, "row_ids": [...]},
#       "method": "...",
#       "filters": {...},
#       "crs": "EPSG:4326",
#       "citations": [...],
#       "confidence_warnings": [...]
#     }
#   }
#
# Default (without the param) returns the raw figure dict, preserving back-compat
# with the existing /v1/viz/strip_log + /cross_section + /stereonet callers.


def _build_export_envelope(
    *,
    chart_type: str,
    figure: dict[str, Any],
    gold_tables: list[str],
    row_ids: list[str],
    method: str,
    filters: dict[str, Any],
    crs: str = "EPSG:4326",
    citations: list[dict[str, Any]] | None = None,
    confidence_warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Wrap a Plotly figure in the §17.4 chart-export contract envelope.

    Citations + confidence_warnings default to empty lists until the
    provenance / QA propagation lands in §10p-i / §5.10 follow-ups. The
    contract requires the keys be present, not populated.
    """
    return {
        "chart": {
            "type":    chart_type,
            "format":  "plotly_json",
            "content": figure,
        },
        "export_metadata": {
            "source_data": {
                "gold_tables":      gold_tables,
                "row_count":        len(row_ids),
                "row_ids":          row_ids,
                "external_sources": [],
            },
            "method":              method,
            "filters":             filters,
            "crs":                 crs,
            "citations":           citations or [],
            "confidence_warnings": confidence_warnings or [],
        },
    }


# ----------------------------------------------------------------------------
# Strip log
# ----------------------------------------------------------------------------


async def _fetch_strip_log_intervals(
    *,
    pg_pool,
    workspace_id: str,
    collar_id: UUID,
) -> list[StripLogInterval]:
    """Pull pre-joined rows from gold.drillhole_intervals_visual.

    Returns the rows ordered by from_depth_m ascending so the renderer
    doesn't need to re-sort. Workspace scope is enforced via the
    `app.workspace_id` GUC that the orchestrator sets on every
    connection — RLS denies cross-tenant access by default.
    """
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        rows = await conn.fetch(
            """
            SELECT
                interval_id::text             AS interval_id,
                collar_id::text               AS collar_id,
                hole_id,
                from_depth_m::float           AS from_depth_m,
                to_depth_m::float             AS to_depth_m,
                lithology_code,
                lithology_label,
                display_label,
                display_color,
                assay_element_max,
                assay_value_max::float        AS assay_value_max,
                assay_unit_max,
                is_mineralised
            FROM gold.drillhole_intervals_visual
            WHERE collar_id = $1::uuid
            ORDER BY from_depth_m
            """,
            str(collar_id),
        )

    return [
        StripLogInterval(
            interval_id=r["interval_id"],
            collar_id=r["collar_id"],
            hole_id=r["hole_id"],
            from_depth_m=r["from_depth_m"],
            to_depth_m=r["to_depth_m"],
            lithology_code=r["lithology_code"],
            lithology_label=r["lithology_label"],
            display_label=r["display_label"],
            display_color=r["display_color"],
            assay_element_max=r["assay_element_max"],
            assay_value_max=r["assay_value_max"],
            assay_unit_max=r["assay_unit_max"],
            is_mineralised=bool(r["is_mineralised"]),
        )
        for r in rows
    ]


@router.get(
    "/strip_log",
    summary="Strip log for one drillhole (Plotly JSON or PNG)",
)
async def get_strip_log(
    collar_id: UUID = Query(..., description="silver.collars.collar_id"),
    fmt: Literal["json", "png"] = Query(
        "json",
        alias="format",
        description="json = Plotly figure dict; png = static raster",
    ),
    title: str | None = Query(
        None,
        description="Optional custom title; defaults to 'Strip log — <hole_id>'",
    ),
    with_export_metadata: bool = Query(
        False,
        description="Wrap the JSON response in the §17.4 chart-export contract envelope.",
    ),
    workspace_id: str = Depends(resolve_workspace_id),
):
    """Render a strip log for one drillhole."""
    # Late-imported here so the router module loads cleanly even if the
    # app.state pool isn't yet bound (e.g., during tests that don't
    # spin up the full lifespan).
    from app.main import app as _app  # noqa: PLC0415

    pg_pool = getattr(_app.state, "pg_pool", None)
    if pg_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pg_pool not bound on app.state",
        )

    try:
        intervals = await _fetch_strip_log_intervals(
            pg_pool=pg_pool,
            workspace_id=workspace_id,
            collar_id=collar_id,
        )
    except Exception:
        logger.exception(
            "strip_log_fetch_failed: collar_id=%s workspace_id=%s",
            collar_id, workspace_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="strip_log_fetch_failed",
        )

    if fmt == "json":
        figure = render_strip_log_plotly_figure(intervals, title=title)
        if with_export_metadata:
            envelope = _build_export_envelope(
                chart_type="strip_log",
                figure=figure,
                gold_tables=["gold.drillhole_intervals_visual"],
                row_ids=[i.interval_id for i in intervals],
                method="depth-ordered intervals from gold.drillhole_intervals_visual",
                filters={"collar_id": str(collar_id)},
            )
            return JSONResponse(content=envelope)
        return JSONResponse(content=figure)

    # fmt == "png"
    png_bytes = render_strip_log_matplotlib_png(intervals, title=title)
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=60",
        },
    )


# ----------------------------------------------------------------------------
# Cross-section (stub — full implementation lands with the cross-section
# renderer module)
# ----------------------------------------------------------------------------


async def _fetch_cross_section_panels(
    *,
    pg_pool,
    workspace_id: str,
    section_line_id: UUID,
) -> list[CrossSectionPanel]:
    """Pull pre-projected panels from gold.cross_section_panels."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        rows = await conn.fetch(
            """
            SELECT
                panel_id::text            AS panel_id,
                section_line_id::text     AS section_line_id,
                interval_id::text         AS interval_id,
                collar_id::text           AS collar_id,
                hole_id,
                distance_along_m::float   AS distance_along_m,
                top_elevation_m::float    AS top_elevation_m,
                bottom_elevation_m::float AS bottom_elevation_m,
                panel_width_m::float      AS panel_width_m,
                lithology_code,
                display_label,
                display_color,
                is_mineralised,
                perpendicular_offset_m::float AS perpendicular_offset_m
              FROM gold.cross_section_panels
             WHERE section_line_id = $1::uuid
             ORDER BY distance_along_m, top_elevation_m DESC
            """,
            str(section_line_id),
        )
    return [
        CrossSectionPanel(
            panel_id=r["panel_id"],
            section_line_id=r["section_line_id"],
            interval_id=r["interval_id"],
            collar_id=r["collar_id"],
            hole_id=r["hole_id"],
            distance_along_m=r["distance_along_m"],
            top_elevation_m=r["top_elevation_m"],
            bottom_elevation_m=r["bottom_elevation_m"],
            panel_width_m=r["panel_width_m"],
            lithology_code=r["lithology_code"],
            display_label=r["display_label"],
            display_color=r["display_color"],
            is_mineralised=bool(r["is_mineralised"]),
            perpendicular_offset_m=r["perpendicular_offset_m"] or 0.0,
        )
        for r in rows
    ]


@router.get(
    "/cross_section",
    summary="Vertical cross-section panel (Plotly JSON or PNG)",
)
async def get_cross_section(
    section_line_id: UUID = Query(..., description="silver.section_lines FK"),
    fmt: Literal["json", "png"] = Query("json", alias="format"),
    title: str | None = Query(None),
    with_export_metadata: bool = Query(
        False,
        description="Wrap the JSON response in the §17.4 chart-export contract envelope.",
    ),
    workspace_id: str = Depends(resolve_workspace_id),
):
    """Render a cross-section from pre-projected panels."""
    from app.main import app as _app  # noqa: PLC0415

    pg_pool = getattr(_app.state, "pg_pool", None)
    if pg_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pg_pool not bound on app.state",
        )

    try:
        panels = await _fetch_cross_section_panels(
            pg_pool=pg_pool,
            workspace_id=workspace_id,
            section_line_id=section_line_id,
        )
    except Exception:
        logger.exception(
            "cross_section_fetch_failed: section_line_id=%s",
            section_line_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="cross_section_fetch_failed",
        )

    if fmt == "json":
        figure = render_cross_section_plotly_figure(panels, title=title)
        if with_export_metadata:
            envelope = _build_export_envelope(
                chart_type="cross_section",
                figure=figure,
                gold_tables=["gold.cross_section_panels", "gold.drillhole_intervals_visual"],
                row_ids=[p.panel_id for p in panels],
                method="orthogonal projection of collars + drill_traces onto A→B section line; intervals joined from gold.drillhole_intervals_visual",
                filters={"section_line_id": str(section_line_id)},
            )
            return JSONResponse(content=envelope)
        return JSONResponse(content=figure)
    png_bytes = render_cross_section_matplotlib_png(panels, title=title)
    return Response(content=png_bytes, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=60"})


# ----------------------------------------------------------------------------
# Stereonet
# ----------------------------------------------------------------------------


async def _fetch_stereonet_points(
    *,
    pg_pool,
    workspace_id: str,
    project_id: UUID,
    measurement_kind: str | None = None,
) -> list[StereonetPoint]:
    """Pull pre-aggregated structural measurements from
    gold.structure_measurements_visual."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        if measurement_kind:
            rows = await conn.fetch(
                """
                SELECT
                    measurement_id::text       AS measurement_id,
                    measurement_kind,
                    pole_trend_deg::float      AS pole_trend_deg,
                    pole_plunge_deg::float     AS pole_plunge_deg,
                    strike_deg::float          AS strike_deg,
                    dip_deg::float             AS dip_deg,
                    depth_m::float             AS depth_m,
                    confidence,
                    display_color,
                    display_symbol
                  FROM gold.structure_measurements_visual
                 WHERE project_id = $1::uuid
                   AND measurement_kind = $2
                """,
                str(project_id), measurement_kind,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT
                    measurement_id::text       AS measurement_id,
                    measurement_kind,
                    pole_trend_deg::float      AS pole_trend_deg,
                    pole_plunge_deg::float     AS pole_plunge_deg,
                    strike_deg::float          AS strike_deg,
                    dip_deg::float             AS dip_deg,
                    depth_m::float             AS depth_m,
                    confidence,
                    display_color,
                    display_symbol
                  FROM gold.structure_measurements_visual
                 WHERE project_id = $1::uuid
                """,
                str(project_id),
            )
    return [
        StereonetPoint(
            measurement_id=r["measurement_id"],
            measurement_kind=r["measurement_kind"],
            pole_trend_deg=r["pole_trend_deg"],
            pole_plunge_deg=r["pole_plunge_deg"],
            strike_deg=r["strike_deg"],
            dip_deg=r["dip_deg"],
            depth_m=r["depth_m"],
            confidence=r["confidence"],
            display_color=r["display_color"],
            display_symbol=r["display_symbol"],
        )
        for r in rows
    ]


@router.get(
    "/stereonet",
    summary="Stereonet projection of structural measurements",
)
async def get_stereonet(
    project_id: UUID = Query(...),
    measurement_kind: str | None = Query(
        None,
        description="Filter to one of bedding/foliation/joint/fault/vein/other",
    ),
    fmt: Literal["json", "png"] = Query("png", alias="format"),
    title: str | None = Query(None),
    with_export_metadata: bool = Query(
        False,
        description="Wrap the JSON response in the §17.4 chart-export contract envelope.",
    ),
    workspace_id: str = Depends(resolve_workspace_id),
):
    """Render an equal-area lower-hemisphere stereonet for one project."""
    from app.main import app as _app  # noqa: PLC0415

    pg_pool = getattr(_app.state, "pg_pool", None)
    if pg_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pg_pool not bound on app.state",
        )

    try:
        points = await _fetch_stereonet_points(
            pg_pool=pg_pool,
            workspace_id=workspace_id,
            project_id=project_id,
            measurement_kind=measurement_kind,
        )
    except Exception:
        logger.exception(
            "stereonet_fetch_failed: project_id=%s kind=%s",
            project_id, measurement_kind,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="stereonet_fetch_failed",
        )

    if fmt == "json":
        figure = render_stereonet_plotly_figure(points, title=title)
        if with_export_metadata:
            filters: dict[str, Any] = {"project_id": str(project_id)}
            if measurement_kind:
                filters["measurement_kind"] = measurement_kind
            envelope = _build_export_envelope(
                chart_type="stereonet",
                figure=figure,
                gold_tables=["gold.structure_measurements_visual"],
                row_ids=[p.measurement_id for p in points],
                method="equal-area lower-hemisphere projection; pole-to-plane for planar measurements",
                filters=filters,
            )
            return JSONResponse(content=envelope)
        return JSONResponse(content=figure)
    png_bytes = render_stereonet_matplotlib_png(points, title=title)
    return Response(content=png_bytes, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=60"})


# ============================================================================
# §17.3 — 8 additional chart types (long-section, Harker, spider, REE,
# ternary, grade-tonnage, anomaly map, target heatmap)
# ============================================================================
from pydantic import BaseModel, Field
from app.services.visualizations.additional_charts import (
    KNOWN_CHARTS, render_chart,
)


# ─── Real-data fetchers ──────────────────────────────────────────────
async def _fetch_long_section_collars(
    *, pg_pool, workspace_id: str, project_id: UUID,
    reference_azimuth_deg: float | None = None,
) -> dict[str, Any]:
    """Pull silver.collars for one project shaped for long_section_figure."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        rows = await conn.fetch(
            """
            SELECT hole_id,
                   easting, northing, COALESCE(elevation, 0) AS elevation,
                   total_depth,
                   COALESCE(azimuth, 0) AS azimuth,
                   COALESCE(dip, -90) AS inclination
              FROM silver.collars
             WHERE project_id = $1::uuid
               AND total_depth > 0
             ORDER BY hole_id
             LIMIT 100
            """,
            project_id,
        )
    return {
        "collars": [dict(r) for r in rows],
        "reference_azimuth_deg": reference_azimuth_deg or 90.0,
    }


async def _fetch_target_heatmap_cells(
    *, pg_pool, workspace_id: str, commodity: str | None,
) -> dict[str, Any]:
    """Pull gold.h3_density_mineral cells, convert h3 → lng/lat for plot."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        rows = await conn.fetch(
            """
            SELECT silver.h3_cell_to_latlng(h3_index) AS center,
                   (occurrence_count + drillhole_count)::float AS score
              FROM gold.h3_density_mineral
             WHERE resolution = 7
               AND ($1::text IS NULL OR commodity_code = $1)
             ORDER BY score DESC
             LIMIT 500
            """,
            commodity,
        )
    cells = []
    for r in rows:
        center = r["center"]
        if center is None:
            continue
        # h3_cell_to_latlng returns POINT(lat, lng) in some bindings or
        # POINT(lng, lat) in others — normalise via PostGIS string.
        # We re-fetch as ST_AsText if needed; cheaper to handle here.
        if isinstance(center, str) and center.startswith("("):
            # asyncpg gives "(lat,lng)" tuple representation
            parts = center.strip("()").split(",")
            if len(parts) == 2:
                try:
                    lat, lng = float(parts[0]), float(parts[1])
                    cells.append({"lng": lng, "lat": lat, "score": float(r["score"])})
                except ValueError:
                    continue
    return {"cells": cells}


async def _fetch_harker_samples(
    *, pg_pool, workspace_id: str, project_id: UUID,
    y_oxide: str = "Al2O3",
) -> dict[str, Any]:
    """Pull SiO2 + y_oxide per sample for Harker diagram."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        rows = await conn.fetch(
            f"""
            SELECT s.rock_type,
                   max(CASE WHEN a.assay_element = 'SiO2' THEN a.assay_value END) AS sio2,
                   max(CASE WHEN a.assay_element = $2 THEN a.assay_value END) AS y_val
              FROM silver.assay_samples s
              JOIN silver.assays a ON a.sample_id = s.sample_id
             WHERE s.project_id = $1::uuid
               AND a.assay_element IN ('SiO2', $2)
             GROUP BY s.sample_id, s.rock_type
            HAVING max(CASE WHEN a.assay_element = 'SiO2' THEN a.assay_value END) IS NOT NULL
               AND max(CASE WHEN a.assay_element = $2 THEN a.assay_value END) IS NOT NULL
             LIMIT 200
            """,
            project_id, y_oxide,
        )
    return {
        "samples": [
            {"SiO2": float(r["sio2"]), y_oxide: float(r["y_val"]),
             "rock_type": r["rock_type"] or "unknown"}
            for r in rows
        ],
        "y_oxide": y_oxide,
    }


async def _fetch_geochem_samples_by_element(
    *, pg_pool, workspace_id: str, project_id: UUID,
    elements: list[str], limit_samples: int = 5,
) -> list[dict[str, Any]]:
    """Pull samples × element matrix (for spider / REE)."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        rows = await conn.fetch(
            """
            SELECT s.sample_code, a.assay_element, a.assay_value
              FROM silver.assay_samples s
              JOIN silver.assays a ON a.sample_id = s.sample_id
             WHERE s.project_id = $1::uuid
               AND a.assay_element = ANY($2::text[])
               AND a.assay_value IS NOT NULL
               AND s.sample_id IN (
                   SELECT sample_id FROM silver.assay_samples
                    WHERE project_id = $1::uuid
                    ORDER BY sample_code LIMIT $3
               )
             ORDER BY s.sample_code, a.assay_element
            """,
            project_id, elements, limit_samples,
        )
    by_sample: dict[str, dict[str, Any]] = {}
    for r in rows:
        sid = r["sample_code"]
        if sid not in by_sample:
            by_sample[sid] = {"sample_id": sid}
        by_sample[sid][r["assay_element"]] = float(r["assay_value"])
    return list(by_sample.values())


async def _fetch_grade_tonnage_samples(
    *, pg_pool, workspace_id: str, project_id: UUID,
    element: str = "Au",
    tonnes_per_sample: float = 1000.0,
) -> dict[str, Any]:
    """Pull (grade, tonnes) tuples for grade-tonnage curve."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        rows = await conn.fetch(
            """
            SELECT a.assay_value AS grade
              FROM silver.assays a
              JOIN silver.assay_samples s ON s.sample_id = a.sample_id
             WHERE s.project_id = $1::uuid
               AND a.assay_element = $2
               AND a.assay_value IS NOT NULL
             ORDER BY grade DESC
             LIMIT 500
            """,
            project_id, element,
        )
    return {
        "samples": [
            {"grade": float(r["grade"]), "tonnes": tonnes_per_sample}
            for r in rows
        ],
        "grade_unit": "g/t" if element == "Au" else "ppm",
    }


async def _fetch_anomaly_map_samples(
    *, pg_pool, workspace_id: str, project_id: UUID,
) -> dict[str, Any]:
    """Use silver.collars total_depth as the "anomaly value" for demo —
    a real wiring would use silver.assays once that table lands. This
    is honest fallback behaviour the chart can demonstrate against
    actual project data."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        rows = await conn.fetch(
            """
            SELECT ST_X(geom_4326) AS lng, ST_Y(geom_4326) AS lat,
                   total_depth AS value
              FROM silver.collars
             WHERE project_id = $1::uuid
               AND geom_4326 IS NOT NULL
               AND total_depth IS NOT NULL
             LIMIT 200
            """,
            project_id,
        )
    return {
        "samples": [dict(r) for r in rows],
        "element_label": "total_depth (m) — anomaly proxy",
    }


class ChartRequest(BaseModel):
    chart_kind: str = Field(..., description="One of the 8 known chart kinds.")
    params: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Per-chart inputs (collars/samples/cells etc). "
            "Pass null/empty to render the synthetic demo dataset."
        ),
    )
    project_id: UUID | None = Field(
        default=None,
        description=(
            "When set + chart_kind supports real-data binding, pull the "
            "inputs from this project's silver/gold tables instead of using "
            "synthetic demo data. Currently supported: long_section, "
            "target_heatmap (workspace-scoped, no project_id needed), anomaly_map."
        ),
    )
    commodity: str | None = Field(
        default=None,
        description="Filter for target_heatmap (e.g. 'au', 'u', 'cu').",
    )
    reference_azimuth_deg: float | None = Field(
        default=None,
        description="Override projection azimuth for long_section (default 90°).",
    )


@router.get("/chart-kinds", summary="List the 8 supported §17.3 chart kinds")
async def list_chart_kinds() -> dict[str, list[str]]:
    return {"chart_kinds": KNOWN_CHARTS}


@router.post(
    "/chart",
    summary="Render a §17.3 chart (Plotly figure spec)",
    description=(
        "POST `{chart_kind, params, project_id?}` to render any of the 8 "
        "chart kinds. With project_id set + a supported chart kind, the "
        "endpoint pulls real workspace data from silver/gold tables. "
        "Without project_id (or for chart kinds not yet wired to real "
        "data), the synthetic demo dataset is used."
    ),
)
async def render_chart_endpoint(
    request: Request,
    body: ChartRequest,
) -> dict[str, Any]:
    if body.chart_kind not in KNOWN_CHARTS:
        raise HTTPException(
            400,
            f"chart_kind must be one of {KNOWN_CHARTS}",
        )

    # Real-data binding for 3 chart kinds when project_id (or commodity
    # for target_heatmap) is provided. Falls back to params/demo otherwise.
    params = body.params
    pg_pool = getattr(request.app.state, "pg_pool", None)
    workspace_id_str = "a0000000-0000-0000-0000-000000000001"  # default; resolved properly when JWT carries it
    if hasattr(request.state, "workspace_id"):
        workspace_id_str = str(request.state.workspace_id)

    try:
        if body.chart_kind == "long_section" and body.project_id and pg_pool:
            params = await _fetch_long_section_collars(
                pg_pool=pg_pool, workspace_id=workspace_id_str,
                project_id=body.project_id,
                reference_azimuth_deg=body.reference_azimuth_deg,
            )
        elif body.chart_kind == "target_heatmap" and pg_pool:
            real = await _fetch_target_heatmap_cells(
                pg_pool=pg_pool, workspace_id=workspace_id_str,
                commodity=body.commodity,
            )
            if real["cells"]:
                params = real
        elif body.chart_kind == "anomaly_map" and body.project_id and pg_pool:
            real = await _fetch_anomaly_map_samples(
                pg_pool=pg_pool, workspace_id=workspace_id_str,
                project_id=body.project_id,
            )
            if real["samples"]:
                params = real
        elif body.chart_kind == "harker_diagram" and body.project_id and pg_pool:
            real = await _fetch_harker_samples(
                pg_pool=pg_pool, workspace_id=workspace_id_str,
                project_id=body.project_id,
                y_oxide=(body.params or {}).get("y_oxide", "Al2O3"),
            )
            if real["samples"]:
                params = real
        elif body.chart_kind == "spider_diagram" and body.project_id and pg_pool:
            elements = ["Rb","Ba","Th","U","Nb","La","Ce","Nd","Sr","Zr","Sm","Eu","Ti","Y","Yb","Lu"]
            samples = await _fetch_geochem_samples_by_element(
                pg_pool=pg_pool, workspace_id=workspace_id_str,
                project_id=body.project_id, elements=elements, limit_samples=3,
            )
            if samples:
                params = {"samples": samples, "normalization": "primitive_mantle"}
        elif body.chart_kind == "ree_pattern" and body.project_id and pg_pool:
            elements = ["La","Ce","Pr","Nd","Sm","Eu","Gd","Tb","Dy","Ho","Er","Tm","Yb","Lu"]
            samples = await _fetch_geochem_samples_by_element(
                pg_pool=pg_pool, workspace_id=workspace_id_str,
                project_id=body.project_id, elements=elements, limit_samples=3,
            )
            if samples:
                params = {"samples": samples}
        elif body.chart_kind == "grade_tonnage" and body.project_id and pg_pool:
            real = await _fetch_grade_tonnage_samples(
                pg_pool=pg_pool, workspace_id=workspace_id_str,
                project_id=body.project_id,
                element=(body.params or {}).get("element", "Au"),
            )
            if real["samples"]:
                params = real
    except Exception:  # noqa: BLE001
        logger.warning(
            "chart real-data fetch failed, falling back to demo: %s",
            body.chart_kind, exc_info=True,
        )
        params = body.params

    try:
        return render_chart(body.chart_kind, params)
    except Exception as exc:  # noqa: BLE001
        logger.exception("chart render failed: %s / %s", body.chart_kind, exc)
        raise HTTPException(500, f"chart render failed: {exc}")


# ============================================================================
# §5.10 + §5.11 — Visual QA + Visual Readiness agent endpoints (B8 + B9)
# ============================================================================
#
# The agents themselves live at app/agents/phase5/{drillhole_visual_qa,
# visual_readiness}.py — pure-function classifiers that take a pre-built
# inventory dict. These endpoints fetch the inventory from PG, then run the
# agent. Output is the operator-readable envelope the §5.12 Drillhole Detail
# page (and any future chat-router viz pre-check) calls before rendering.

from app.agents import AgentContext  # noqa: E402
from app.agents.phase5.drillhole_visual_qa import drillhole_visual_qa  # noqa: E402
from app.agents.phase5.visual_readiness import visual_readiness  # noqa: E402
from app.services.auth import UserContext, extract_user_context  # noqa: E402


async def _fetch_visual_qa_inventory(
    *, pg_pool, workspace_id: str, collar_id: UUID,
) -> dict[str, Any]:
    """Build the §5.10 visual-QA inventory dict for one collar.

    All queries scoped via RLS (`app.workspace_id` GUC); cross-tenant
    collars return as has_collar=False because RLS denies the row.
    """
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        collar_row = await conn.fetchrow(
            """
            SELECT total_depth, azimuth, dip
              FROM silver.collars
             WHERE collar_id = $1::uuid
            """,
            str(collar_id),
        )
        interval_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                          AS n,
                COUNT(*) FILTER (WHERE lithology_code IS NOT NULL) AS n_litho
              FROM gold.drillhole_intervals_visual
             WHERE collar_id = $1::uuid
            """,
            str(collar_id),
        )
        # drill_traces is one row per collar (UNIQUE on collar_id) carrying
        # a LineStringZ geometry. Presence = 1 trace; absence = 0.
        trace_count = await conn.fetchval(
            "SELECT COUNT(*) FROM silver.drill_traces WHERE collar_id = $1::uuid",
            str(collar_id),
        )

    has_collar = collar_row is not None
    total_depth = collar_row["total_depth"] if has_collar else None
    azimuth = collar_row["azimuth"] if has_collar else None
    dip = collar_row["dip"] if has_collar else None
    return {
        "has_collar":          has_collar,
        "has_total_depth":     has_collar and total_depth is not None and total_depth > 0,
        "has_azimuth_dip":     has_collar and azimuth is not None and dip is not None,
        "interval_count":      int(interval_row["n"] or 0) if interval_row else 0,
        "has_lithology_codes": bool(interval_row and (interval_row["n_litho"] or 0) > 0),
        "trace_point_count":   int(trace_count or 0),
    }


async def _fetch_readiness_inventory(
    *, pg_pool, workspace_id: str, viz_kind: str,
    collar_id: UUID | None, project_id: UUID | None,
) -> dict[str, Any]:
    """Build the §5.11 readiness inventory dict per viz_kind."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )

        if viz_kind == "strip_log":
            row = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM gold.drillhole_intervals_visual WHERE collar_id = $1::uuid) AS interval_count,
                    (SELECT (total_depth IS NOT NULL AND total_depth > 0)::int
                       FROM silver.collars WHERE collar_id = $1::uuid)                                AS has_total_depth
                """,
                str(collar_id),
            )
            return {
                "interval_count":  int(row["interval_count"] or 0),
                "has_total_depth": int(row["has_total_depth"] or 0),
            }

        if viz_kind == "stereonet":
            n = await conn.fetchval(
                """
                SELECT COUNT(*) FROM gold.structure_measurements_visual
                 WHERE collar_id = $1::uuid
                """,
                str(collar_id),
            )
            return {"structure_count": int(n or 0)}

        if viz_kind == "cross_section":
            collar_count = await conn.fetchval(
                "SELECT COUNT(*) FROM silver.collars WHERE project_id = $1::uuid",
                str(project_id),
            )
            section_line_present = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM gold.cross_section_panels
                     WHERE project_id = $1::uuid
                )
                """,
                str(project_id),
            )
            interval_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM gold.drillhole_intervals_visual
                 WHERE project_id = $1::uuid
                """,
                str(project_id),
            )
            return {
                "collar_count":         int(collar_count or 0),
                "section_line_present": 1 if section_line_present else 0,
                "interval_count":       int(interval_count or 0),
            }

    return {}


class VizQaRequest(BaseModel):
    collar_id: UUID
    workspace_id: UUID | None = None


@router.post(
    "/qa",
    summary="§5.10 Drillhole Visual QA — audit visualization readiness for one collar",
)
async def viz_qa(
    body: VizQaRequest,
    request: Request,
    user: UserContext = Depends(extract_user_context),
) -> dict[str, Any]:
    pg_pool = getattr(request.app.state, "pg_pool", None)
    if pg_pool is None:
        raise HTTPException(503, "pg_pool not bound on app.state")

    if body.workspace_id is not None:
        ws_id = str(body.workspace_id)
    else:
        ws_uuid = await resolve_workspace_id(user, request, pg_pool, None)
        ws_id = str(ws_uuid)

    inventory = await _fetch_visual_qa_inventory(
        pg_pool=pg_pool, workspace_id=ws_id, collar_id=body.collar_id,
    )

    ctx = AgentContext(
        workspace_id=UUID(ws_id),
        actor_kind="user",
    )
    result = await drillhole_visual_qa(
        ctx=ctx, collar_id=body.collar_id, inventory=inventory,
    )
    payload = result.value if hasattr(result, "value") else result
    return {
        "outcome":        getattr(result, "outcome", "ok"),
        "duration_ms":    getattr(result, "duration_ms", 0),
        "invocation_id":  str(getattr(getattr(result, "ctx", None), "invocation_id", "")),
        "inventory":      inventory,
        "qa":             payload,
    }


class VizReadinessRequest(BaseModel):
    viz_kind: Literal["strip_log", "cross_section", "stereonet"]
    collar_id: UUID | None = None
    project_id: UUID | None = None
    workspace_id: UUID | None = None


@router.post(
    "/readiness",
    summary="§5.11 Visual Readiness — pre-check whether a visualization is feasible",
)
async def viz_readiness(
    body: VizReadinessRequest,
    request: Request,
    user: UserContext = Depends(extract_user_context),
) -> dict[str, Any]:
    import time as _time
    _readiness_t0 = _time.monotonic()

    if body.viz_kind in ("strip_log", "stereonet") and body.collar_id is None:
        raise HTTPException(400, f"{body.viz_kind} requires collar_id")
    if body.viz_kind == "cross_section" and body.project_id is None:
        raise HTTPException(400, "cross_section requires project_id")

    pg_pool = getattr(request.app.state, "pg_pool", None)
    if pg_pool is None:
        raise HTTPException(503, "pg_pool not bound on app.state")

    if body.workspace_id is not None:
        ws_id = str(body.workspace_id)
    else:
        ws_uuid = await resolve_workspace_id(user, request, pg_pool, None)
        ws_id = str(ws_uuid)
    inventory = await _fetch_readiness_inventory(
        pg_pool=pg_pool, workspace_id=ws_id, viz_kind=body.viz_kind,
        collar_id=body.collar_id, project_id=body.project_id,
    )

    ctx = AgentContext(workspace_id=UUID(ws_id), actor_kind="user")
    result = await visual_readiness(
        ctx=ctx, viz_kind=body.viz_kind,
        collar_id=body.collar_id, project_id=body.project_id,
        inventory=inventory,
    )
    payload = result.value if hasattr(result, "value") else result

    # Phase 6 — readiness-probe latency histogram. Labelled by whether
    # the call carried an explicit workspace_id (real backing data) or
    # fell through to resolution (typically the empty-state probe path).
    try:
        from app.metrics import READINESS_PROBE_DURATION
        READINESS_PROBE_DURATION.labels(
            workspace_scoped="true" if body.workspace_id is not None else "false",
        ).observe(_time.monotonic() - _readiness_t0)
    except Exception:
        pass

    return {
        "outcome":        getattr(result, "outcome", "ok"),
        "duration_ms":    getattr(result, "duration_ms", 0),
        "invocation_id":  str(getattr(getattr(result, "ctx", None), "invocation_id", "")),
        "inventory":      inventory,
        "readiness":      payload,
    }
