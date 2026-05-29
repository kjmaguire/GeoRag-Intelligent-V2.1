"""Gold layer asset — pre-computed cross-section panels.

§5 Phase H4 Step 5.7. Materialises ``gold.cross_section_panels`` per the
real schema in ``2026_05_13_080001_create_gold_cross_section_panels.php``:

    panel_id          uuid PK
    workspace_id      uuid NOT NULL
    project_id        uuid NOT NULL
    section_name      varchar(120) NOT NULL
    section_line_geom geometry(LineString, 4326) NOT NULL
    azimuth_deg       numeric(6,3)
    length_m          numeric(12,3)
    collars_projected jsonb NOT NULL DEFAULT '[]'
    x_extent_m        numeric(12,3)
    y_extent_m        numeric(12,3)
    buffer_m          numeric(10,3) NOT NULL DEFAULT 50
    computed_at       timestamptz
    created_at        timestamptz
    UNIQUE (project_id, section_name)

The asset projects collars in a corridor around the A→B section line onto a
2D panel coordinate system (axis distance + elevation) and serialises the
result into ``collars_projected``. Drill-trace LineStringZ is sampled at
regular depth intervals and each sample point is projected onto the
section axis. Strip-log intervals from ``gold.drillhole_intervals_visual``
ride along inside each hole's entry so the renderer needs one fetch.

Idempotent via ``(project_id, section_name)`` UPSERT. Buffers default to
50 m (matches schema default) and override via config.

NOTE: Do NOT add ``from __future__ import annotations`` — Dagster 1.13
Config classes need runtime-evaluable type hints.
"""

import json
import math
import uuid

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.resources import PostgresResource


COLLARS_IN_CORRIDOR_SQL = """
SELECT
    c.collar_id,
    c.hole_id,
    ST_X(c.geom_4326) AS lon,
    ST_Y(c.geom_4326) AS lat,
    c.easting,
    c.northing,
    c.elevation,
    c.total_depth,
    c.project_id
FROM silver.collars c
WHERE c.workspace_id = %(workspace_id)s::uuid
  AND (%(project_id)s::uuid IS NULL OR c.project_id = %(project_id)s::uuid)
  AND c.geom_4326 IS NOT NULL
  AND ST_DWithin(
        c.geom_4326::geography,
        ST_SetSRID(
            ST_MakeLine(
                ST_MakePoint(%(a_lon)s, %(a_lat)s),
                ST_MakePoint(%(b_lon)s, %(b_lat)s)
            ),
            4326
        )::geography,
        %(buffer_m)s
      )
ORDER BY hole_id;
"""

TRACE_BY_HOLE_SQL = """
SELECT ST_AsGeoJSON(geom) AS geojson
FROM silver.drill_traces
WHERE collar_id = %(collar_id)s::uuid;
"""

INTERVALS_BY_HOLE_SQL = """
SELECT depth_from, depth_to, lithology_code, lithology_label, color_hint, assay_payload
FROM gold.drillhole_intervals_visual
WHERE collar_id = %(collar_id)s::uuid
ORDER BY depth_from;
"""

UPSERT_PANEL_SQL = """
INSERT INTO gold.cross_section_panels (
    panel_id, workspace_id, project_id, section_name,
    section_line_geom, azimuth_deg, length_m,
    collars_projected, x_extent_m, y_extent_m, buffer_m
) VALUES (
    %(panel_id)s, %(workspace_id)s, %(project_id)s, %(section_name)s,
    ST_SetSRID(ST_MakeLine(
        ST_MakePoint(%(a_lon)s, %(a_lat)s),
        ST_MakePoint(%(b_lon)s, %(b_lat)s)
    ), 4326),
    %(azimuth_deg)s, %(length_m)s,
    %(collars_projected)s::jsonb, %(x_extent_m)s, %(y_extent_m)s, %(buffer_m)s
)
ON CONFLICT (project_id, section_name) DO UPDATE SET
    workspace_id      = EXCLUDED.workspace_id,
    section_line_geom = EXCLUDED.section_line_geom,
    azimuth_deg       = EXCLUDED.azimuth_deg,
    length_m          = EXCLUDED.length_m,
    collars_projected = EXCLUDED.collars_projected,
    x_extent_m        = EXCLUDED.x_extent_m,
    y_extent_m        = EXCLUDED.y_extent_m,
    buffer_m          = EXCLUDED.buffer_m,
    computed_at       = now();
"""


def _bearing_deg(a_lon: float, a_lat: float, b_lon: float, b_lat: float) -> float:
    """Forward azimuth A → B in degrees (0=N, clockwise)."""
    phi1 = math.radians(a_lat)
    phi2 = math.radians(b_lat)
    dlon = math.radians(b_lon - a_lon)
    x = math.sin(dlon) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _haversine_m(a_lon: float, a_lat: float, b_lon: float, b_lat: float) -> float:
    """Great-circle distance in metres."""
    r = 6_371_000.0
    phi1 = math.radians(a_lat)
    phi2 = math.radians(b_lat)
    dphi = math.radians(b_lat - a_lat)
    dlam = math.radians(b_lon - a_lon)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _project_lonlat_onto_axis(
    lon: float, lat: float,
    a_lon: float, a_lat: float,
    b_lon: float, b_lat: float,
) -> tuple[float, float]:
    """Approximate projection of (lon,lat) onto the A→B great-circle axis.

    Returns (axis_distance_m, perpendicular_offset_m). Approximation valid
    for short sections (a few km); for longer sections we should switch
    to a local UTM frame. v1 sections are typically ≤2 km — within the
    error envelope of the strip-log itself.
    """
    section_len = _haversine_m(a_lon, a_lat, b_lon, b_lat)
    if section_len == 0:
        return 0.0, _haversine_m(lon, lat, a_lon, a_lat)
    # Bearing from A to (lon,lat)
    theta_total = _bearing_deg(a_lon, a_lat, b_lon, b_lat)
    theta_point = _bearing_deg(a_lon, a_lat, lon, lat)
    delta = math.radians(theta_point - theta_total)
    dist_total = _haversine_m(a_lon, a_lat, lon, lat)
    axis = dist_total * math.cos(delta)
    perp = abs(dist_total * math.sin(delta))
    return axis, perp


class GoldCrossSectionConfig(Config):
    """Section definition + tenant scope."""

    workspace_id: str
    project_id: str
    section_name: str
    a_lon: float
    a_lat: float
    b_lon: float
    b_lat: float
    buffer_m: float = 50.0


@asset(
    group_name="gold",
    description=(
        "Project collars in a buffer around the A→B section line onto a 2D "
        "panel (axis distance + elevation), bundle strip-log intervals + "
        "projected drill traces, and UPSERT into gold.cross_section_panels."
    ),
)
def gold_cross_section_panels(
    context: AssetExecutionContext,
    config: GoldCrossSectionConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    """Compute one cross-section panel and upsert it into gold.cross_section_panels."""

    section_len = _haversine_m(config.a_lon, config.a_lat, config.b_lon, config.b_lat)
    azimuth = _bearing_deg(config.a_lon, config.a_lat, config.b_lon, config.b_lat)

    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                COLLARS_IN_CORRIDOR_SQL,
                {
                    "workspace_id": config.workspace_id,
                    "project_id":   config.project_id,
                    "a_lon":        config.a_lon,
                    "a_lat":        config.a_lat,
                    "b_lon":        config.b_lon,
                    "b_lat":        config.b_lat,
                    "buffer_m":     config.buffer_m,
                },
            )
            collars = cur.fetchall()

            collars_payload: list[dict] = []
            x_min = math.inf
            x_max = -math.inf
            y_min = math.inf
            y_max = -math.inf

            for collar in collars:
                lon = float(collar["lon"])
                lat = float(collar["lat"])
                axis_m, perp_m = _project_lonlat_onto_axis(
                    lon, lat, config.a_lon, config.a_lat, config.b_lon, config.b_lat,
                )

                cur.execute(TRACE_BY_HOLE_SQL, {"collar_id": collar["collar_id"]})
                trace_row = cur.fetchone()
                trace_points: list[dict] = []
                if trace_row and trace_row["geojson"]:
                    geojson = json.loads(trace_row["geojson"])
                    for x, y, z in geojson.get("coordinates", []):
                        tp_axis, tp_perp = _project_lonlat_onto_axis(
                            x, y, config.a_lon, config.a_lat, config.b_lon, config.b_lat,
                        )
                        trace_points.append({
                            "axis_m":      tp_axis,
                            "perp_m":      tp_perp,
                            "elevation_m": float(z),
                        })
                        x_min = min(x_min, tp_axis)
                        x_max = max(x_max, tp_axis)
                        y_min = min(y_min, float(z))
                        y_max = max(y_max, float(z))

                cur.execute(INTERVALS_BY_HOLE_SQL, {"collar_id": collar["collar_id"]})
                intervals = [
                    {
                        "from":            float(r["depth_from"]),
                        "to":              float(r["depth_to"]),
                        "lithology_code":  r["lithology_code"],
                        "lithology_label": r["lithology_label"],
                        "color_hint":      r["color_hint"],
                        "assays":          r["assay_payload"] or {},
                    }
                    for r in cur.fetchall()
                ]

                collar_elev = float(collar["elevation"]) if collar["elevation"] is not None else 0.0
                total_depth = float(collar["total_depth"]) if collar["total_depth"] is not None else 0.0
                x_min = min(x_min, axis_m)
                x_max = max(x_max, axis_m)
                y_min = min(y_min, collar_elev - total_depth)
                y_max = max(y_max, collar_elev)

                collars_payload.append({
                    "collar_id":           str(collar["collar_id"]),
                    "hole_id":             collar["hole_id"],
                    "axis_distance_m":     axis_m,
                    "perpendicular_offset_m": perp_m,
                    "collar_elevation_m":  collar_elev,
                    "total_depth_m":       total_depth,
                    "trace":               trace_points,
                    "intervals":           intervals,
                })

            x_extent = (x_max - x_min) if x_min != math.inf and x_max != -math.inf else None
            y_extent = (y_max - y_min) if y_min != math.inf and y_max != -math.inf else None

            cur.execute(
                UPSERT_PANEL_SQL,
                {
                    "panel_id":          str(uuid.uuid4()),
                    "workspace_id":      config.workspace_id,
                    "project_id":        config.project_id,
                    "section_name":      config.section_name,
                    "a_lon":             config.a_lon,
                    "a_lat":             config.a_lat,
                    "b_lon":             config.b_lon,
                    "b_lat":             config.b_lat,
                    "azimuth_deg":       azimuth,
                    "length_m":          section_len,
                    "collars_projected": json.dumps(collars_payload),
                    "x_extent_m":        x_extent,
                    "y_extent_m":        y_extent,
                    "buffer_m":          config.buffer_m,
                },
            )
        conn.commit()

    context.log.info(
        "gold_cross_section_panels: section='%s' project=%s collars=%d length_m=%.1f azimuth=%.1f°",
        config.section_name, config.project_id, len(collars_payload), section_len, azimuth,
    )

    return MaterializeResult(
        metadata={
            "section_name":     MetadataValue.text(config.section_name),
            "project_id":       MetadataValue.text(config.project_id),
            "workspace_id":     MetadataValue.text(config.workspace_id),
            "collar_count":     MetadataValue.int(len(collars_payload)),
            "section_length_m": MetadataValue.float(section_len),
            "azimuth_deg":      MetadataValue.float(azimuth),
            "x_extent_m":       MetadataValue.float(x_extent or 0.0),
            "y_extent_m":       MetadataValue.float(y_extent or 0.0),
        }
    )
