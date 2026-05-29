"""silver.assays_v2 → gold.significant_intersections.

For each (workspace, hole, element, cutoff) combination, compute the
contiguous weighted-average intercepts at or above the cutoff. The
output rows are what geologists put in press-release tables and
NI 43-101 reports.

Implementation: a window-function walk over assays ordered by depth.
We tag a "run start" wherever the value crosses the cutoff after
being below (or at the start of the hole), then aggregate via the
running run-tag.

Default cutoff grades (Kyle, 2026-05-20: "defaults"):
  Au → 0.3 g/t (300 ppb), Cu → 1000 ppm (0.1%),
  U  → 100 ppm (0.01% U₃O₈), Ag → 10 ppm, Pb → 10000 ppm (1%),
  Zn → 10000 ppm (1%), Ni → 1000 ppm (0.1%), Mo → 100 ppm.
"""
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.resources import PostgresResource


# Default cutoff grades, expressed in the element's value_ppm
# (so the silver.assays_v2.value_ppm column compares directly).
DEFAULT_CUTOFFS_PPM = {
    "Au": 300.0,
    "Ag": 10.0,
    "Cu": 1000.0,
    "Pb": 10000.0,
    "Zn": 10000.0,
    "Ni": 1000.0,
    "Co": 500.0,
    "Mo": 100.0,
    "U":  100.0,
}


class SignificantIntersectionsConfig(Config):
    workspace_id: str
    # Optional override; otherwise DEFAULT_CUTOFFS_PPM applies.
    cutoffs_ppm: dict[str, float] | None = None


# Per-element compute: returns (collar_id, element, cutoff, from, to,
# weighted_avg, peak_value, peak_depth, sample_count).
_COMPUTE_SQL = """
WITH tagged AS (
  SELECT
    a.workspace_id, a.collar_id, a.element,
    a.from_depth, a.to_depth, a.value_ppm,
    a.interval_length,
    -- run_id increments each time we transition from "below cutoff"
    -- to "at-or-above cutoff" within a hole.
    SUM(
      CASE
        WHEN a.value_ppm >= %(cutoff)s AND COALESCE(
          LAG(a.value_ppm) OVER w, 0
        ) < %(cutoff)s THEN 1
        ELSE 0
      END
    ) OVER w AS run_id,
    (a.value_ppm >= %(cutoff)s) AS at_cutoff
  FROM silver.assays_v2 a
  WHERE a.workspace_id = %(workspace_id)s::uuid
    AND a.element = %(element)s
    AND a.value_ppm IS NOT NULL
  WINDOW w AS (PARTITION BY a.collar_id ORDER BY a.from_depth)
),
runs AS (
  SELECT
    workspace_id, collar_id, element, run_id,
    MIN(from_depth) AS from_depth,
    MAX(to_depth)   AS to_depth,
    -- length-weighted average
    SUM(value_ppm * interval_length) / NULLIF(SUM(interval_length), 0)
      AS weighted_avg,
    MAX(value_ppm) AS peak_value,
    -- depth of the peak: a join trick — pick the depth where value
    -- equals the run's MAX(value_ppm).
    NULL::numeric AS peak_depth,
    COUNT(*) AS sample_count
  FROM tagged
  WHERE at_cutoff
  GROUP BY workspace_id, collar_id, element, run_id
)
INSERT INTO gold.significant_intersections (
  workspace_id, collar_id, element, cutoff_grade,
  from_depth, to_depth, weighted_avg, unit,
  peak_value, peak_depth, sample_count, computed_at
)
SELECT
  workspace_id, collar_id, element, %(cutoff)s,
  from_depth, to_depth, weighted_avg, 'ppm',
  peak_value, peak_depth, sample_count, NOW()
FROM runs
WHERE weighted_avg IS NOT NULL
"""

_CLEAR_BEFORE_RECOMPUTE = """
DELETE FROM gold.significant_intersections
 WHERE workspace_id = %s::uuid
   AND element = %s
"""


@asset(
    group_name="drillhole_gold",
    description=(
        "Notable grade intercepts per (hole, element, cutoff). Window-"
        "function walk over silver.assays_v2 ordered by depth, "
        "aggregating contiguous at-or-above-cutoff runs into single "
        "rows. Defaults: 0.3 g/t Au, 0.1% Cu, 1% Pb/Zn, 100 ppm U."
    ),
)
def gold_significant_intersections(
    context: AssetExecutionContext,
    config: SignificantIntersectionsConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    cutoffs = config.cutoffs_ppm or DEFAULT_CUTOFFS_PPM
    pg = postgres.connect()
    intersections_per_element: dict[str, int] = {}

    try:
        with pg.cursor() as cur:
            for element, cutoff in cutoffs.items():
                cur.execute(
                    _CLEAR_BEFORE_RECOMPUTE,
                    (config.workspace_id, element),
                )
                cur.execute(
                    _COMPUTE_SQL,
                    {
                        "workspace_id": config.workspace_id,
                        "element": element,
                        "cutoff": cutoff,
                    },
                )
                intersections_per_element[element] = cur.rowcount or 0
        pg.commit()
    finally:
        pg.close()

    return MaterializeResult(
        metadata={
            "intersections_per_element": MetadataValue.json(
                intersections_per_element
            ),
            "total_intersections": MetadataValue.int(
                sum(intersections_per_element.values())
            ),
        },
    )
