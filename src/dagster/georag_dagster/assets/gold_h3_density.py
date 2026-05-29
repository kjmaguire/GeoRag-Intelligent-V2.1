"""§6.6 — h3 density aggregation of public-geoscience mineral data.

Refreshes ``gold.h3_density_mineral`` from:
  - ``public_geo.pg_mineral_occurrence`` → ``occurrence_count``
  - ``public_geo.pg_drillhole_collar`` → ``drillhole_count``

For each row, computes the h3 index at the appropriate resolution set
(see policy below) using the ``silver.h3_latlng_to_cell(geometry,
integer)`` function provided by h3_postgis. Aggregates per
(commodity, h3, resolution) and TRUNCATEs + re-INSERTs the gold table
— full refresh, idempotent across runs.

Cron schedule: ``0 5 * * *`` UTC (immediately after the §11 backup
window closes at 03:00 UTC and cold-tier archive at 04:00 UTC).
Scheduling lives in the Dagster definitions file, not here.

Resolution policy (locked in master_plan_section6_kickoff.md,
2026-05-16, Kyle deferred to geology call):

  - Default {5, 7, 9} for most commodities
      5: ~252 km hex side — continental zoom
      7: ~36 km  hex side — regional zoom
      9: ~5 km   hex side — project-scale zoom

  - {5, 7, 9, 10} for the 7 critical minerals in _CRITICAL_MINERAL_CODES
      10: ~1.8 km hex side — drill-grid spacing
      Athabasca uranium / pegmatite Li / magmatic Ni-Cu-PGE clusters
      need this; default res 9 aliases distinct deposits/pads.

The Martin function ``silver.density_choropleth_h3(z, x, y)`` picks
the zoom-appropriate resolution and returns MVT to the MapView.
"""

import psycopg2
from dagster import AssetExecutionContext, MaterializeResult, MetadataValue, asset

from georag_dagster.resources import PostgresResource


# Default resolution set, applied to every commodity_code that ISN'T
# in _CRITICAL_MINERAL_CODES below.
_DEFAULT_RESOLUTIONS: tuple[int, ...] = (5, 7, 9)

# Critical-mineral commodity codes (lowercased, matched against
# pg_mineral_occurrence.primary_commodities after unnest + LOWER).
# Aligned to Canada's Critical Minerals Strategy (2022) intersected
# with the exploration patterns most likely in the registered Canadian
# public-geoscience sources (BC MINFILE / SaskGeoAtlas / AGS / NRCan).
# Each of these commodities gets one extra resolution band (10,
# ~1.8 km hex side) for drill-grid alignment.
_CRITICAL_MINERAL_CODES: frozenset[str] = frozenset({
    "u",    # Uranium  — Athabasca unconformity
    "li",   # Lithium  — pegmatite + brine + clay
    "cu",   # Copper   — porphyry + sediment-hosted
    "co",   # Cobalt   — Ni/Cu sulfide systems
    "ni",   # Nickel   — magmatic sulfide camps
    "ree",  # Rare earths — carbonatite + alkaline complexes
    "pge",  # Platinum group — layered intrusions
})

_CRITICAL_RESOLUTIONS: tuple[int, ...] = (*_DEFAULT_RESOLUTIONS, 10)

# Every resolution that COULD appear in the gold table — used by the
# SQL UNION below and as a documentation contract for downstream
# consumers (Martin function, MapView toggle).
_ALL_RESOLUTIONS: tuple[int, ...] = tuple(sorted(
    set(_DEFAULT_RESOLUTIONS) | set(_CRITICAL_RESOLUTIONS)
))

# Comma-separated SQL literal for the IN-list used in the per-commodity
# resolution CASE — kept here so any add/remove from the critical-
# minerals frozenset above propagates to the SQL without manual edits.
def _sql_critical_in_list() -> str:
    return ", ".join(f"'{code}'" for code in sorted(_CRITICAL_MINERAL_CODES))


def _build_aggregate_sql() -> str:
    """Construct the aggregator. The per-commodity resolution CASE is
    interpolated from `_CRITICAL_MINERAL_CODES` so adds/removes don't
    require a hand-edit of the SQL string."""
    crit_in = _sql_critical_in_list()  # e.g. 'co', 'cu', 'li', 'ni', 'pge', 'ree', 'u'
    return f"""
TRUNCATE TABLE gold.h3_density_mineral;

INSERT INTO gold.h3_density_mineral
    (commodity_code, h3_index, resolution, occurrence_count, drillhole_count, computed_at)
SELECT
    src.commodity_code,
    silver.h3_latlng_to_cell(src.geom_centroid, res.resolution) AS h3_index,
    res.resolution,
    SUM(CASE WHEN src.kind = 'occurrence' THEN 1 ELSE 0 END) AS occurrence_count,
    SUM(CASE WHEN src.kind = 'drillhole'  THEN 1 ELSE 0 END) AS drillhole_count,
    now()
FROM (
    -- Mineral occurrences carry primary_commodities as a text[] array.
    -- Unnest so a multi-commodity occurrence contributes one row per
    -- commodity. Empty arrays → no rows (the occurrence has no known
    -- commodity and is dropped from the density layer).
    SELECT
        LOWER(commodity) AS commodity_code,
        ST_PointOnSurface(geom)::geometry AS geom_centroid,
        'occurrence' AS kind
    FROM public_geo.pg_mineral_occurrence
        CROSS JOIN LATERAL unnest(primary_commodities) AS commodity
    WHERE geom IS NOT NULL AND ST_IsValid(geom)
      AND commodity IS NOT NULL AND commodity <> ''
    UNION ALL
    -- Drillhole collars don't carry a single primary commodity in the
    -- canonical schema (they target rocks, not commodities). Use the
    -- 'drillhole' sentinel so the choropleth's drillhole layer is
    -- queryable without needing a commodity join.
    SELECT
        'drillhole' AS commodity_code,
        ST_PointOnSurface(geom)::geometry AS geom_centroid,
        'drillhole' AS kind
    FROM public_geo.pg_drillhole_collar
    WHERE geom IS NOT NULL AND ST_IsValid(geom)
) src
-- Per-commodity resolution set. Critical minerals get the extra
-- res-10 band (~1.8 km hex) for drill-grid alignment; everything
-- else stays on the default {5, 7, 9}.
CROSS JOIN LATERAL (
    SELECT unnest(
        CASE
            WHEN src.commodity_code IN ({crit_in})
                THEN ARRAY[5, 7, 9, 10]::int[]
            ELSE ARRAY[5, 7, 9]::int[]
        END
    ) AS resolution
) res
GROUP BY src.commodity_code, res.resolution,
         silver.h3_latlng_to_cell(src.geom_centroid, res.resolution);
"""


_AGGREGATE_SQL = _build_aggregate_sql()


_SUMMARY_SQL = """
SELECT
    resolution,
    count(*) AS cells,
    SUM(occurrence_count) AS total_occurrences,
    SUM(drillhole_count)  AS total_drillholes
FROM gold.h3_density_mineral
GROUP BY resolution
ORDER BY resolution;
"""


@asset(
    group_name="gold",
    description=(
        "h3 density aggregation of public-geoscience mineral occurrences "
        "and drillhole collars. Critical minerals (U/Li/Cu/Co/Ni/REE/PGE) "
        "materialise at resolutions {5, 7, 9, 10}; all other commodities "
        "and drillholes at {5, 7, 9}. Refreshed nightly @ 05:00 UTC; feeds "
        "the §6.13 density choropleth Martin function. Cross-tenant — "
        "gold.h3_density_mineral has no workspace_id."
    ),
)
def gold_h3_density_choropleth(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> MaterializeResult:
    context.log.info(
        "gold_h3_density_choropleth: aggregating defaults=%s critical=%s "
        "for codes=%s",
        _DEFAULT_RESOLUTIONS,
        _CRITICAL_RESOLUTIONS,
        sorted(_CRITICAL_MINERAL_CODES),
    )
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_AGGREGATE_SQL)
            conn.commit()

            cur.execute(_SUMMARY_SQL)
            summary_rows = cur.fetchall()

    summary = {
        f"resolution_{res}": {
            "cells":            cells,
            "total_occurrences": int(occ or 0),
            "total_drillholes":  int(dr or 0),
        }
        for res, cells, occ, dr in summary_rows
    }
    total_cells = sum(s["cells"] for s in summary.values())

    context.log.info(
        "gold_h3_density_choropleth: materialised %d cells across %d resolutions",
        total_cells, len(summary),
    )

    return MaterializeResult(
        metadata={
            "default_resolutions":    MetadataValue.json(list(_DEFAULT_RESOLUTIONS)),
            "critical_resolutions":   MetadataValue.json(list(_CRITICAL_RESOLUTIONS)),
            "critical_codes":         MetadataValue.json(sorted(_CRITICAL_MINERAL_CODES)),
            "total_cells":            MetadataValue.int(total_cells),
            "per_resolution":         MetadataValue.json(summary),
        },
    )


__all__ = ["gold_h3_density_choropleth"]
