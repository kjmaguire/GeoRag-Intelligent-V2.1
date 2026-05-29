"""Gold layer asset — drillhole intervals enriched for visual rendering.

§5 Phase H4. Materialises `gold.drillhole_intervals_visual` (schema in
`database/raw/phase5/10-drillhole-intervals-visual.sql`). Each row is
a pre-joined per-interval record carrying everything the §5 strip-log
renderer needs:

    silver.collars                  → identity + spatial context
    silver.lithology_intervals      → from/to depth, lithology code
    silver.assays                   → max assay value per element per
                                       interval (for the colour-by-grade
                                       overlay)
    SME lithology palette           → display_color fallback when not
                                       set per-interval

The asset is idempotent via the unique constraint
(collar_id, from_depth_m, to_depth_m). Re-materialising after a silver
refresh upserts (DELETE + INSERT scoped to the workspace) — avoids
the cost of a per-row MERGE on PostgreSQL.

Mineralisation threshold (`is_mineralised`):
    True when assay_value_max > project's `mineralisation_threshold`
    OR (when no threshold set) when the assay element is one of the
    target commodities AND the value exceeds the SME-curated default
    (1000 ppm U3O8, 0.5 g/t Au, 0.5% Cu, etc.). The defaults match
    the operator-curated thresholds in §20.2 deposit-model templates.

NOTE: Do NOT add `from __future__ import annotations` — Dagster 1.13's
Config/ConfigurableResource classes use Pydantic for type introspection.
"""

import logging
import math
from typing import Optional

import psycopg2.extras
from dagster import (
    AssetExecutionContext,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.assets.silver import silver_collars
from georag_dagster.resources import PostgresResource


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SME default lithology palette (matches app/services/visualizations/strip_log.py)
# ---------------------------------------------------------------------------

_DEFAULT_LITHOLOGY_PALETTE: dict[str, str] = {
    "SST":  "#f4d35e",
    "CGL":  "#c89f60",
    "PGN":  "#bc4749",
    "GPT":  "#8b8b8b",
    "MUD":  "#6b705c",
    "SLT":  "#a4ac86",
    "SHL":  "#5e6068",
    "LMS":  "#cad2c5",
    "DOL":  "#dadec7",
    "VEI":  "#e8c2ca",
    "FLT":  "#000000",
    "OVB":  "#a0522d",
}


# Per-commodity mineralisation thresholds (SME defaults). Per-project
# overrides live in silver.projects.mineralisation_threshold once that
# column lands; for now this dict is the fallback.
_DEFAULT_MINERALISATION_THRESHOLDS: dict[str, float] = {
    "U3O8_ppm":  1000.0,   # 0.1% U3O8
    "U3O8_pct":  0.10,
    "U_ppm":     800.0,
    "Au_ppm":    0.5,
    "Au_ppb":    500.0,
    "Au_gpt":    0.5,
    "Ag_ppm":    50.0,
    "Cu_pct":    0.5,
    "Cu_ppm":    5000.0,
    "Zn_pct":    2.0,
    "Pb_pct":    1.0,
    "Ni_pct":    0.5,
    "Co_ppm":    500.0,
}


def _color_for(lithology_code: Optional[str]) -> Optional[str]:
    """Resolve the default palette colour from a lithology code prefix."""
    if not lithology_code:
        return None
    code = lithology_code.upper()
    for prefix, colour in _DEFAULT_LITHOLOGY_PALETTE.items():
        if code.startswith(prefix):
            return colour
    return None


def _is_mineralised(element: Optional[str], value: Optional[float]) -> bool:
    """SME-default mineralisation flag."""
    if element is None or value is None or math.isnan(value):
        return False
    threshold = _DEFAULT_MINERALISATION_THRESHOLDS.get(element)
    if threshold is None:
        return False
    return float(value) >= threshold


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

# silver_lithology_intervals + silver_samples assets may not exist as
# Python modules yet (some silver assets land via raw SQL); we depend
# only on silver_collars at the Python level and pull the rest via SQL
# joins to the underlying tables. The asset still gets re-materialised
# whenever silver_collars bumps a workspace's data_version.

@asset(
    group_name="gold",
    deps=[silver_collars],
    description=(
        "Pre-joined visual-ready strip-log rows. One row per "
        "(collar, from_depth, to_depth) interval enriched with the max "
        "assay value across all elements + SME palette colour + "
        "mineralisation flag. Source of truth for the §5 strip-log "
        "renderer at /v1/viz/strip_log."
    ),
)
def gold_drillhole_intervals_visual(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> MaterializeResult:
    """Materialise gold.drillhole_intervals_visual from silver inputs."""

    counters = {
        "intervals_read":            0,
        "intervals_written":         0,
        "intervals_mineralised":     0,
        "intervals_with_assays":     0,
        "intervals_skipped":         0,
        "workspaces_processed":      0,
        "errors":                    0,
    }

    with postgres.get_connection() as conn:
        conn.autocommit = False

        # 1. Discover which workspaces actually have collar data.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT workspace_id, data_version
                  FROM silver.workspaces
                  WHERE workspace_id IN (
                        SELECT DISTINCT workspace_id FROM silver.collars
                  )
                """
            )
            workspaces = cur.fetchall()

        for ws_row in workspaces:
            workspace_id = ws_row[0]
            ws_data_version = int(ws_row[1] or 0)
            counters["workspaces_processed"] += 1
            context.log.info(
                "gold_drillhole_intervals_visual: workspace=%s "
                "data_version=%d",
                workspace_id, ws_data_version,
            )

            try:
                # 2. Set RLS GUC + delete old rows for this workspace
                #    (full upsert; intervals counts can shrink between
                #     materialisations as silver evolves).
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('app.workspace_id', %s, false)",
                        (str(workspace_id),),
                    )
                    cur.execute(
                        "DELETE FROM gold.drillhole_intervals_visual "
                        "WHERE workspace_id = %s::uuid",
                        (str(workspace_id),),
                    )

                # 3. Pull the source rows. Left join on assays so
                #    intervals without samples still come through.
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute(
                        """
                        WITH max_assay_per_interval AS (
                            SELECT
                                li.lithology_interval_id,
                                a.element            AS assay_element_max,
                                a.value::float       AS assay_value_max,
                                a.unit               AS assay_unit_max,
                                ROW_NUMBER() OVER (
                                    PARTITION BY li.lithology_interval_id
                                    ORDER BY a.value DESC NULLS LAST
                                ) AS rnk
                              FROM silver.lithology_intervals li
                              LEFT JOIN silver.assays a
                                ON a.collar_id   = li.collar_id
                               AND a.from_depth >= li.from_depth_m
                               AND a.to_depth   <= li.to_depth_m
                             WHERE li.workspace_id = %(ws)s::uuid
                        )
                        SELECT
                            li.lithology_interval_id::text AS interval_id,
                            li.workspace_id::text          AS workspace_id,
                            li.project_id::text            AS project_id,
                            li.collar_id::text             AS collar_id,
                            c.hole_id                      AS hole_id,
                            li.from_depth_m::float         AS from_depth_m,
                            li.to_depth_m::float           AS to_depth_m,
                            li.lithology_code              AS lithology_code,
                            li.lithology_label             AS lithology_label,
                            ma.assay_element_max,
                            ma.assay_value_max,
                            ma.assay_unit_max,
                            c.easting::float               AS easting,
                            c.northing::float              AS northing,
                            c.elevation_m::float           AS elevation_m,
                            c.crs_epsg                     AS crs_epsg
                          FROM silver.lithology_intervals li
                          JOIN silver.collars c
                            ON c.collar_id = li.collar_id
                          LEFT JOIN max_assay_per_interval ma
                            ON ma.lithology_interval_id = li.lithology_interval_id
                           AND ma.rnk = 1
                         WHERE li.workspace_id = %(ws)s::uuid
                         ORDER BY c.hole_id, li.from_depth_m
                        """,
                        {"ws": str(workspace_id)},
                    )
                    source_rows = cur.fetchall()

                if not source_rows:
                    context.log.info(
                        "gold_drillhole_intervals_visual: workspace=%s "
                        "has no lithology intervals; skipping",
                        workspace_id,
                    )
                    continue

                counters["intervals_read"] += len(source_rows)

                # 4. Compute derived fields (color, mineralised) then
                #    bulk-insert.
                insert_rows = []
                for r in source_rows:
                    code = r["lithology_code"]
                    color = _color_for(code)
                    display_label = r["lithology_label"] or code or None
                    mineralised = _is_mineralised(
                        r["assay_element_max"], r["assay_value_max"],
                    )
                    if r["assay_value_max"] is not None:
                        counters["intervals_with_assays"] += 1
                    if mineralised:
                        counters["intervals_mineralised"] += 1
                    insert_rows.append((
                        r["interval_id"],
                        r["workspace_id"],
                        r["project_id"],
                        r["collar_id"],
                        r["hole_id"],
                        r["from_depth_m"],
                        r["to_depth_m"],
                        code,
                        r["lithology_label"],
                        display_label,
                        color,
                        r["assay_element_max"],
                        r["assay_value_max"],
                        r["assay_unit_max"],
                        mineralised,
                        r["easting"],
                        r["northing"],
                        r["elevation_m"],
                        r["crs_epsg"],
                        ws_data_version,
                    ))

                with conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur,
                        """
                        INSERT INTO gold.drillhole_intervals_visual (
                            interval_id, workspace_id, project_id,
                            collar_id, hole_id,
                            from_depth_m, to_depth_m,
                            lithology_code, lithology_label,
                            display_label, display_color,
                            assay_element_max, assay_value_max, assay_unit_max,
                            is_mineralised,
                            easting, northing, elevation_m, crs_epsg,
                            silver_data_version_at_materialisation
                        ) VALUES %s
                        ON CONFLICT (collar_id, from_depth_m, to_depth_m)
                            DO UPDATE SET
                                lithology_code = EXCLUDED.lithology_code,
                                lithology_label = EXCLUDED.lithology_label,
                                display_label = EXCLUDED.display_label,
                                display_color = EXCLUDED.display_color,
                                assay_element_max = EXCLUDED.assay_element_max,
                                assay_value_max = EXCLUDED.assay_value_max,
                                assay_unit_max = EXCLUDED.assay_unit_max,
                                is_mineralised = EXCLUDED.is_mineralised,
                                materialised_at = NOW(),
                                silver_data_version_at_materialisation =
                                    EXCLUDED.silver_data_version_at_materialisation
                        """,
                        insert_rows,
                        template=(
                            "(%s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, "
                            "%s::numeric, %s::numeric, "
                            "%s, %s, %s, %s, "
                            "%s, %s::numeric, %s, %s, "
                            "%s::numeric, %s::numeric, %s::numeric, %s, "
                            "%s)"
                        ),
                        page_size=500,
                    )
                    counters["intervals_written"] += len(insert_rows)

                conn.commit()

            except Exception as exc:
                counters["errors"] += 1
                conn.rollback()
                context.log.exception(
                    "gold_drillhole_intervals_visual: workspace=%s failed: %s",
                    workspace_id, exc,
                )

    context.log.info(
        "gold_drillhole_intervals_visual: complete. counters=%s",
        counters,
    )

    return MaterializeResult(
        metadata={
            "workspaces_processed":  MetadataValue.int(counters["workspaces_processed"]),
            "intervals_read":        MetadataValue.int(counters["intervals_read"]),
            "intervals_written":     MetadataValue.int(counters["intervals_written"]),
            "intervals_mineralised": MetadataValue.int(counters["intervals_mineralised"]),
            "intervals_with_assays": MetadataValue.int(counters["intervals_with_assays"]),
            "errors":                MetadataValue.int(counters["errors"]),
        },
    )
