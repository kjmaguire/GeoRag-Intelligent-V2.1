"""silver.assays_v2 → gold.assay_composites.

Pre-computes fixed-length composites (default 1m, 2m, 5m) per
(hole, element). The composite_type column distinguishes between
'fixed_1m', 'fixed_2m', 'fixed_5m', and 'geological' (which is
populated by a separate path that respects lithology contacts).

Composite math: length-weighted average over a fixed-width window
along each hole, walking from collar to TD.
"""
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.resources import PostgresResource


class AssayCompositesConfig(Config):
    workspace_id: str
    composite_lengths_m: list[float] = [1.0, 2.0, 5.0]


# generate_series buckets each assay's depth range into fixed-width
# windows, then we length-weight-average inside each bucket.
_UPSERT_SQL = """
WITH windows AS (
  SELECT
    a.workspace_id, a.collar_id, a.element,
    %(length)s::numeric AS window_len,
    floor(a.from_depth / %(length)s::numeric) * %(length)s::numeric AS from_bucket,
    LEAST(
      floor(a.to_depth / %(length)s::numeric) * %(length)s::numeric
        + %(length)s::numeric,
      a.to_depth
    ) AS to_bucket,
    a.value_ppm, a.unit, a.interval_length
  FROM silver.assays_v2 a
  WHERE a.workspace_id = %(workspace_id)s::uuid
    AND a.value_ppm IS NOT NULL
),
agg AS (
  SELECT
    workspace_id, collar_id, element,
    from_bucket AS from_depth,
    from_bucket + window_len AS to_depth,
    SUM(value_ppm * interval_length) / NULLIF(SUM(interval_length), 0)
      AS weighted_avg,
    MIN(value_ppm) AS min_value,
    MAX(value_ppm) AS max_value,
    COUNT(*) AS sample_count,
    MAX(unit) AS unit
  FROM windows
  GROUP BY workspace_id, collar_id, element, from_bucket, window_len
)
INSERT INTO gold.assay_composites (
  workspace_id, collar_id, composite_type, element,
  from_depth, to_depth, weighted_avg, unit,
  sample_count, min_value, max_value, computed_at
)
SELECT
  workspace_id, collar_id,
  %(composite_type)s, element,
  from_depth, to_depth, weighted_avg, unit,
  sample_count, min_value, max_value, NOW()
FROM agg
WHERE weighted_avg IS NOT NULL
ON CONFLICT (workspace_id, collar_id, composite_type, element, from_depth, to_depth)
DO UPDATE SET
  weighted_avg = EXCLUDED.weighted_avg,
  sample_count = EXCLUDED.sample_count,
  min_value    = EXCLUDED.min_value,
  max_value    = EXCLUDED.max_value,
  computed_at  = NOW()
"""


@asset(
    group_name="drillhole_gold",
    description=(
        "Fixed-length composites (1m / 2m / 5m default) over "
        "silver.assays_v2. UPSERT keyed on the natural (hole, "
        "composite_type, element, from, to) tuple."
    ),
)
def gold_assay_composites(
    context: AssetExecutionContext,
    config: AssayCompositesConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    pg = postgres.connect()
    composites_per_length: dict[float, int] = {}

    try:
        with pg.cursor() as cur:
            for length in config.composite_lengths_m:
                cur.execute(
                    _UPSERT_SQL,
                    {
                        "workspace_id": config.workspace_id,
                        "length": length,
                        "composite_type": f"fixed_{length}m",
                    },
                )
                composites_per_length[length] = cur.rowcount or 0
        pg.commit()
    finally:
        pg.close()

    return MaterializeResult(
        metadata={
            "composites_per_length": MetadataValue.json(composites_per_length),
            "total_composites": MetadataValue.int(
                sum(composites_per_length.values())
            ),
        },
    )
