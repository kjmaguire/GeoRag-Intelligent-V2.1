"""silver.* → gold.drill_summaries.

One row per drillhole. The summary geologists scan first:
   - total_depth, assay_coverage_pct, lithology_coverage_pct
   - recovery_avg_pct
   - best Au interval (grade, from, to)
   - which elements were assayed
   - qaqc_pass_rate per the hole's lab batches
   - has_geophysics flag

UPSERT keyed on (workspace_id, collar_id). Replaying the asset
recomputes from current silver state — safe to re-run anytime.
"""
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.resources import PostgresResource


class DrillSummariesConfig(Config):
    workspace_id: str


# The whole computation is one SQL statement so the row writes
# happen at PG's join speed, not Python's.
_UPSERT_SQL = """
WITH base AS (
  SELECT
    c.workspace_id,
    c.collar_id,
    c.hole_id,
    c.total_depth,
    -- assay coverage as % of total_depth
    COALESCE(SUM(DISTINCT a.to_depth - a.from_depth), 0)
      / NULLIF(c.total_depth, 0) * 100 AS assay_coverage_pct,
    -- elements assayed (sorted, deduped)
    ARRAY(SELECT DISTINCT a.element FROM silver.assays_v2 a
           WHERE a.collar_id = c.collar_id ORDER BY 1) AS elements_assayed
  FROM silver.collars c
  LEFT JOIN silver.assays_v2 a ON a.collar_id = c.collar_id
  WHERE c.workspace_id = %s::uuid
  GROUP BY c.workspace_id, c.collar_id, c.hole_id, c.total_depth
),
lith AS (
  SELECT collar_id,
         COALESCE(SUM(to_depth - from_depth), 0) AS l_covered
    FROM silver.lithology
   GROUP BY collar_id
),
rec AS (
  SELECT collar_id, AVG(core_recovery_pct) AS recovery_avg_pct
    FROM silver.recovery
   GROUP BY collar_id
),
best_au AS (
  SELECT DISTINCT ON (collar_id)
         collar_id, value_ppm AS grade, from_depth, to_depth
    FROM silver.assays_v2
   WHERE element = 'Au'
     AND value_ppm IS NOT NULL
   ORDER BY collar_id, value_ppm DESC
),
geophys AS (
  SELECT DISTINCT collar_id FROM silver.downhole_geophysics
)
INSERT INTO gold.drill_summaries (
  workspace_id, collar_id, hole_id, total_depth,
  assay_coverage_pct, lithology_coverage_pct,
  recovery_avg_pct, best_au_interval_grade,
  best_au_interval_from, best_au_interval_to,
  elements_assayed, has_geophysics, computed_at
)
SELECT
  b.workspace_id, b.collar_id, b.hole_id, b.total_depth,
  b.assay_coverage_pct,
  COALESCE(l.l_covered / NULLIF(b.total_depth, 0) * 100, 0) AS lithology_coverage_pct,
  r.recovery_avg_pct,
  ba.grade, ba.from_depth, ba.to_depth,
  b.elements_assayed,
  (g.collar_id IS NOT NULL) AS has_geophysics,
  NOW()
FROM base b
LEFT JOIN lith l   ON l.collar_id  = b.collar_id
LEFT JOIN rec r    ON r.collar_id  = b.collar_id
LEFT JOIN best_au ba ON ba.collar_id = b.collar_id
LEFT JOIN geophys g ON g.collar_id  = b.collar_id
ON CONFLICT (collar_id) DO UPDATE SET
  total_depth            = EXCLUDED.total_depth,
  assay_coverage_pct     = EXCLUDED.assay_coverage_pct,
  lithology_coverage_pct = EXCLUDED.lithology_coverage_pct,
  recovery_avg_pct       = EXCLUDED.recovery_avg_pct,
  best_au_interval_grade = EXCLUDED.best_au_interval_grade,
  best_au_interval_from  = EXCLUDED.best_au_interval_from,
  best_au_interval_to    = EXCLUDED.best_au_interval_to,
  elements_assayed       = EXCLUDED.elements_assayed,
  has_geophysics         = EXCLUDED.has_geophysics,
  computed_at            = NOW()
RETURNING collar_id
"""


@asset(
    group_name="drillhole_gold",
    description=(
        "One row per drillhole. Aggregates assay coverage, lithology "
        "coverage, average recovery, best Au intercept, and "
        "elements_assayed from silver. UPSERT keyed on collar_id."
    ),
)
def gold_drill_summaries(
    context: AssetExecutionContext,
    config: DrillSummariesConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    pg = postgres.connect()
    rows_upserted = 0
    try:
        with pg.cursor() as cur:
            cur.execute(_UPSERT_SQL, (config.workspace_id,))
            rows_upserted = cur.rowcount or 0
        pg.commit()
    finally:
        pg.close()

    return MaterializeResult(
        metadata={"rows_upserted": MetadataValue.int(rows_upserted)},
    )
