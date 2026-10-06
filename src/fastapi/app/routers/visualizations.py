"""Visualizations router (§17.3 chart cards).

Endpoints
---------
GET  /v1/viz/chart-kinds — the supported §17.3 chart kinds.
POST /v1/viz/chart       — render one §17.3 chart (Plotly figure spec).

The §5 per-drillhole endpoints (GET /v1/viz/strip_log, /cross_section,
/stereonet) were unmounted on 2026-09-29 (database audit PG-13) — see the
note below the router definition.

Auth
----
Service-key + workspace-id context. Workspace scope is enforced via the
RLS GUC at the asyncpg connection level — queries outside the caller's
workspace return empty result sets, which the renderer turns into a
graceful "no data" figure.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.agent.workspace_context import LEGACY_DEFAULT_TENANT_UUID, WorkspaceContext
from app.agent.workspace_dependency import OptionalWorkspace
from app.db.scoped_pool import scoped_connection
from app.metrics import WORKSPACE_RESOLUTION_FAILURES
from app.services.auth import verify_service_key
from app.services.collar_depth import EFFECTIVE_TOTAL_DEPTH_SQL

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/v1/viz",
    tags=["visualizations"],
    dependencies=[Depends(verify_service_key)],
)


# ----------------------------------------------------------------------------
# Removed 2026-09-29: GET /strip_log, /cross_section, /stereonet
# ----------------------------------------------------------------------------
#
# Database audit PG-13. All three read gold columns that do not exist
# (strip_log: interval_id / from_depth_m / display_color, where
# gold.drillhole_intervals_visual has visual_id / depth_from / color_hint;
# cross_section: section_line_id and more; stereonet: measurement_id and
# more), so every call was a 500. Nothing in Laravel or the React app
# called them — the Workspace, DrillholeDetail and chat cards read those gold
# tables through Laravel directly — and the only other reference was the
# tests/load_k6/viz_strip_log.k6.js load script. Unmounted rather than
# repaired; the renderers in app/services/visualizations/ are untouched and
# git history has the handlers if a caller ever needs them back.

# ============================================================================
# §17.3 — 8 additional chart types (long-section, Harker, spider, REE,
# ternary, grade-tonnage, anomaly map, target heatmap)
# ============================================================================
import contextlib  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402

from app.services.visualizations.additional_charts import (  # noqa: E402
    KNOWN_CHARTS,
    render_chart,
)


# ─── Real-data fetchers ──────────────────────────────────────────────
async def _fetch_long_section_collars(
    *, pg_pool, workspace_id: str, project_id: UUID,
    reference_azimuth_deg: float | None = None,
) -> dict[str, Any]:
    """Pull silver.collars for one project shaped for long_section_figure.

    GIS-6 / GIS-7 (2026-09-29): easting/northing are computed from geom_4326
    in ONE metric frame — the UTM zone of the project's collar centroid —
    not read from the easting/northing columns, which hold whatever each
    source gave (UTM metres of any zone, lon/lat degrees, US survey feet)
    and so cannot be plotted against each other. azimuth/dip are passed
    through as NULL when unrecorded; the figure labels those holes instead
    of inventing a vertical one.
    """
    async with scoped_connection(
        pg_pool, workspace_id=workspace_id, site="viz._fetch_long_section_collars"
    ) as conn:
        # total_depth is optional since 2026-09-29 (§04e): a collar without one
        # is drawn to its deepest survey/interval instead of being dropped.
        rows = await conn.fetch(
            f"""
            WITH c AS (
                SELECT c.hole_id, c.geom_4326, c.elevation,
                       {EFFECTIVE_TOTAL_DEPTH_SQL} AS total_depth,
                       c.azimuth, c.dip
                  FROM silver.collars c
                 WHERE c.project_id = $1::uuid
                   AND {EFFECTIVE_TOTAL_DEPTH_SQL} > 0
                   AND c.geom_4326 IS NOT NULL
                 ORDER BY c.hole_id
                 LIMIT 100
            ), frame AS (
                SELECT CASE WHEN ST_Y(ST_Centroid(ST_Collect(geom_4326))) >= 0
                            THEN 32600 ELSE 32700 END
                       + LEAST(60, GREATEST(1,
                           floor((ST_X(ST_Centroid(ST_Collect(geom_4326))) + 180.0) / 6.0)::int + 1
                         )) AS srid
                  FROM c
            )
            SELECT c.hole_id,
                   ST_X(ST_Transform(c.geom_4326, frame.srid)) AS easting,
                   ST_Y(ST_Transform(c.geom_4326, frame.srid)) AS northing,
                   COALESCE(c.elevation, 0) AS elevation,
                   c.total_depth,
                   c.azimuth,
                   c.dip AS inclination
              FROM c CROSS JOIN frame
             ORDER BY c.hole_id
            """,
            project_id,
        )
    return {
        "collars": [dict(r) for r in rows],
        "reference_azimuth_deg": reference_azimuth_deg or 90.0,
    }


class HeatmapCapabilityMissing(RuntimeError):
    """The H3 stack this chart needs is not installed on this server.

    Raised instead of returning empty cells so ``render`` can answer 503
    rather than dropping into its generic "fall back to demo params" path —
    see the comment on that handler for why silence is the wrong answer here.
    """


async def _h3_capability_missing(conn) -> str | None:
    """Return a human-readable reason the H3 heatmap cannot be served, or None.

    The target heatmap needs three things that arrive together or not at all:
    the `h3` extension (for silver.h3_cell_to_latlng), the `h3_postgis`
    extension, and gold.h3_density_mineral. All three are declared only in
    `database/raw/`, which CD never applies, so on Azure none of them exist.

    This is not a fixable-by-migration gap. Azure Database for PostgreSQL
    Flexible Server does not offer `h3` at all — it is absent from the
    server's `azure.extensions` allowedValues, so `CREATE EXTENSION h3` can
    never succeed there regardless of privileges. The chart is therefore
    permanently unavailable on Azure until it is rewritten without H3, and the
    honest thing is to say so.
    """
    has_extension = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'h3')"
    )
    if not has_extension:
        return "the h3 extension is not installed on this server"

    has_table = await conn.fetchval(
        "SELECT to_regclass('gold.h3_density_mineral') IS NOT NULL"
    )
    if not has_table:
        return "gold.h3_density_mineral does not exist on this server"

    return None


async def _fetch_target_heatmap_cells(
    *, pg_pool, workspace_id: str, commodity: str | None,
) -> dict[str, Any]:
    """Pull gold.h3_density_mineral cells, convert h3 → lng/lat for plot.

    Raises HeatmapCapabilityMissing when the H3 stack is absent. Without that
    check the query raises UndefinedFunction/UndefinedTable, `render` catches
    it with the rest of the real-data fetches, and the caller is served
    `body.params` — demo placeholder data — with only a WARNING in the log.
    A fabricated heatmap that looks real is a worse failure than an error.
    """
    async with scoped_connection(
        pg_pool, workspace_id=workspace_id, site="viz._fetch_target_heatmap_cells"
    ) as conn:
        reason = await _h3_capability_missing(conn)
        if reason is not None:
            raise HeatmapCapabilityMissing(reason)

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
    """Pull SiO2 + y_oxide per sample for Harker diagram.

    Reads ``silver.assays_v2``, the canonical assay table. This used to query
    ``silver.assay_samples`` JOIN ``silver.assays``, a pair declared only in
    ``database/raw/phase0/109`` that CD never applied and that the
    architecture doc does not name at all — so on Azure the query raised
    ``42P01`` and the caller's blanket ``except Exception`` quietly swapped in
    demo data. A Harker diagram of fabricated geochemistry is exactly what the
    target_heatmap capability gate refuses to ship, so the fix is to read the
    table that is actually deployed.

    Two shape differences from the retired pair:

    * ``assays_v2`` has no ``project_id`` — it is scoped by ``collar_id``, so
      the project filter goes through ``silver.collars``.
    * ``assays_v2`` has no ``rock_type``. The nearest deployed source is the
      logged lithology at the sample's depth, so ``silver.lithology`` is
      joined on the interval containing ``from_depth``. That containing-interval
      rule is the conventional reading and is only ever a display grouping
      here — the LEFT JOIN keeps unmatched samples, which fall through to
      "unknown" exactly as a NULL ``rock_type`` did before. Worth an SME
      confirmation if Harker grouping ever becomes load-bearing.
    """
    async with scoped_connection(
        pg_pool, workspace_id=workspace_id, site="viz._fetch_harker_samples"
    ) as conn:
        rows = await conn.fetch(
            """
            SELECT max(l.rock_name) AS rock_type,
                   max(CASE WHEN a.element = 'SiO2' THEN a.value END) AS sio2,
                   max(CASE WHEN a.element = $2 THEN a.value END) AS y_val
              FROM silver.assays_v2 a
              JOIN silver.collars c ON c.collar_id = a.collar_id
              LEFT JOIN silver.lithology l
                     ON l.collar_id = a.collar_id
                    AND a.from_depth >= l.from_depth
                    AND a.from_depth <  l.to_depth
             WHERE c.project_id = $1::uuid
               AND a.element IN ('SiO2', $2)
             GROUP BY a.collar_id, a.sample_id
            HAVING max(CASE WHEN a.element = 'SiO2' THEN a.value END) IS NOT NULL
               AND max(CASE WHEN a.element = $2 THEN a.value END) IS NOT NULL
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
    """Pull samples × element matrix (for spider / REE).

    Reads ``silver.assays_v2`` — see ``_fetch_harker_samples`` for why the
    retired ``silver.assays`` / ``silver.assay_samples`` pair is gone. Here
    ``assays_v2.sample_id`` (text) plays the part the old ``sample_code``
    did, and the project filter goes through ``silver.collars``.
    """
    async with scoped_connection(
        pg_pool, workspace_id=workspace_id, site="viz._fetch_geochem_samples_by_element"
    ) as conn:
        rows = await conn.fetch(
            """
            SELECT a.sample_id, a.element, a.value
              FROM silver.assays_v2 a
              JOIN silver.collars c ON c.collar_id = a.collar_id
             WHERE c.project_id = $1::uuid
               AND a.element = ANY($2::text[])
               AND a.value IS NOT NULL
               AND a.sample_id IN (
                   SELECT v.sample_id
                     FROM silver.assays_v2 v
                     JOIN silver.collars vc ON vc.collar_id = v.collar_id
                    WHERE vc.project_id = $1::uuid
                    GROUP BY v.sample_id
                    ORDER BY v.sample_id LIMIT $3
               )
             ORDER BY a.sample_id, a.element
            """,
            project_id, elements, limit_samples,
        )
    by_sample: dict[str, dict[str, Any]] = {}
    for r in rows:
        sid = r["sample_id"]
        if sid not in by_sample:
            by_sample[sid] = {"sample_id": sid}
        by_sample[sid][r["element"]] = float(r["value"])
    return list(by_sample.values())


async def _fetch_grade_tonnage_samples(
    *, pg_pool, workspace_id: str, project_id: UUID,
    element: str = "Au",
    tonnes_per_sample: float = 1000.0,
) -> dict[str, Any]:
    """Pull (grade, tonnes) tuples for grade-tonnage curve.

    Reads ``silver.assays_v2`` — see ``_fetch_harker_samples`` for why the
    retired ``silver.assays`` / ``silver.assay_samples`` pair is gone.
    """
    async with scoped_connection(
        pg_pool, workspace_id=workspace_id, site="viz._fetch_grade_tonnage_samples"
    ) as conn:
        rows = await conn.fetch(
            """
            SELECT a.value AS grade
              FROM silver.assays_v2 a
              JOIN silver.collars c ON c.collar_id = a.collar_id
             WHERE c.project_id = $1::uuid
               AND a.element = $2
               AND a.value IS NOT NULL
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
    """Use silver.collars total_depth as the "anomaly value" for demo.

    This is honest fallback behaviour the chart can demonstrate against
    actual project data. The previous note here said a real wiring would use
    ``silver.assays`` "once that table lands" — it never will; that table was
    raw-only, is absent from the architecture doc, and was archived on
    2026-08-28. The canonical source for a real wiring is
    ``silver.assays_v2``, which IS deployed; doing that rewiring is a chart
    behaviour change and deliberately not bundled with the table repointing.
    """
    async with scoped_connection(
        pg_pool, workspace_id=workspace_id, site="viz._fetch_anomaly_map_samples"
    ) as conn:
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
    ws: WorkspaceContext | None = OptionalWorkspace,
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
    # REC#1 (2026-06-03) — typed Depends. `ws` is None ONLY when the
    # request had no workspace claim on auth context; the synthetic
    # demo path is the right fallback for chart rendering specifically
    # (this endpoint genuinely serves anonymous gallery previews, so
    # OptionalWorkspace is correct here vs RequiredWorkspace). The B4
    # metric still fires so ops can see anonymous render rate.
    if ws is not None:
        workspace_id_str = ws.workspace_id
    else:
        workspace_id_str = LEGACY_DEFAULT_TENANT_UUID
        with contextlib.suppress(Exception):
            WORKSPACE_RESOLUTION_FAILURES.labels(
                site="visualizations.render"
            ).inc()

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
    except HeatmapCapabilityMissing as exc:
        # Deliberately NOT the demo fallback below. Every other chart kind
        # degrades to placeholder data on a fetch failure, which is fine when
        # the cause is transient. This one is a permanent server capability
        # gap (h3 is not available on Azure Flexible Server at all), so demo
        # data would mean shipping a fabricated exploration-target heatmap
        # that a geologist cannot distinguish from a real one.
        logger.warning(
            "target_heatmap unavailable on this server: %s", exc,
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"target_heatmap is not available on this server: {exc}",
        ) from exc
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
        raise HTTPException(500, "chart render failed") from exc


# ============================================================================
# §5.10 + §5.11 — Visual QA + Visual Readiness agent endpoints — REMOVED
# 2026-07-28 (task #31).
#
# These backed app/agents/phase5/{drillhole_visual_qa,visual_readiness}.py,
# gated on "the §5.12 Drillhole Detail page" per this module's own prior
# docstring — but no page by that name (or any variant) exists anywhere in
# resources/js/, and nothing in app/ (Laravel) or resources/js/ calls
# /v1/viz/qa or /v1/viz/readiness. Confirmed dead on both sides before
# removal; phase5 (the whole directory) had no other caller and was deleted
# alongside this.
# ============================================================================
