"""silver.assays_v2 → gold.element_correlations.

Pearson r between every (element_a, element_b) pair within each
project, computed from co-located assay samples (same hole, same
from-to interval).

Useful for understanding mineralisation style:
  - Au-Ag correlation ≈ 1 → epithermal vein style
  - Cu-Mo correlation ≈ 1 → porphyry style
  - U-V correlation ≈ 1 → roll-front uranium

The query uses PostgreSQL's corr() aggregate which handles the
N>1 / variance-positive checks internally.
"""
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.resources import PostgresResource


class ElementCorrelationsConfig(Config):
    workspace_id: str
    min_paired_samples: int = 30


_UPSERT_SQL = """
WITH paired AS (
  SELECT
    a.workspace_id,
    c.project_id,
    a.element AS element_a,
    b.element AS element_b,
    a.value_ppm AS value_a,
    b.value_ppm AS value_b
  FROM silver.assays_v2 a
  JOIN silver.assays_v2 b
    ON a.collar_id = b.collar_id
   AND a.from_depth = b.from_depth
   AND a.to_depth   = b.to_depth
   AND a.element <  b.element  -- avoid (Au,Au) self-pairs + dedupe
  JOIN silver.collars c ON c.collar_id = a.collar_id
  WHERE a.workspace_id = %s::uuid
    AND a.value_ppm IS NOT NULL
    AND b.value_ppm IS NOT NULL
),
agg AS (
  SELECT
    workspace_id, project_id, element_a, element_b,
    corr(value_a, value_b) AS r,
    COUNT(*) AS n
  FROM paired
  GROUP BY workspace_id, project_id, element_a, element_b
  HAVING COUNT(*) >= %s
     AND corr(value_a, value_b) IS NOT NULL
)
INSERT INTO gold.element_correlations (
  workspace_id, project_id, element_a, element_b,
  correlation_r, sample_count, computed_at
)
SELECT workspace_id, project_id, element_a, element_b,
       round(r::numeric, 4), n, NOW()
  FROM agg
ON CONFLICT (workspace_id, project_id, element_a, element_b) DO UPDATE
   SET correlation_r = EXCLUDED.correlation_r,
       sample_count  = EXCLUDED.sample_count,
       computed_at   = NOW()
"""


@asset(
    group_name="drillhole_gold",
    description=(
        "Pearson r between element pairs at co-located depth intervals. "
        "Filters pairs below min_paired_samples to avoid spurious "
        "correlations from N<30 samples. UPSERTs per project."
    ),
)
def gold_element_correlations(
    context: AssetExecutionContext,
    config: ElementCorrelationsConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    pg = postgres.connect()
    rows = 0
    try:
        with pg.cursor() as cur:
            cur.execute(
                _UPSERT_SQL,
                (config.workspace_id, config.min_paired_samples),
            )
            rows = cur.rowcount or 0
        pg.commit()
    finally:
        pg.close()
    return MaterializeResult(
        metadata={
            "pairs_upserted": MetadataValue.int(rows),
            "min_paired_samples": MetadataValue.int(config.min_paired_samples),
        },
    )
