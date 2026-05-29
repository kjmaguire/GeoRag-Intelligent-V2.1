"""silver.* → gold.zone_statistics.

Per-(zone, element, cutoff) statistics. Zones come from
gold.significant_intersections.zone_name when set; rows without
a zone label aggregate under '__unzoned'.
"""
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.resources import PostgresResource


class ZoneStatisticsConfig(Config):
    workspace_id: str


_UPSERT_SQL = """
WITH zones AS (
  SELECT
    si.workspace_id,
    c.project_id,
    COALESCE(si.zone_name, '__unzoned') AS zone_name,
    si.element,
    si.cutoff_grade,
    COUNT(DISTINCT si.collar_id) AS holes_in_zone,
    SUM(si.downhole_length) AS total_length_m,
    AVG(si.weighted_avg) AS avg_grade,
    MAX(si.weighted_avg) AS max_grade,
    AVG(si.true_width_m) AS avg_true_width
  FROM gold.significant_intersections si
  JOIN silver.collars c ON c.collar_id = si.collar_id
  WHERE si.workspace_id = %s::uuid
  GROUP BY si.workspace_id, c.project_id, COALESCE(si.zone_name, '__unzoned'),
           si.element, si.cutoff_grade
)
INSERT INTO gold.zone_statistics (
  workspace_id, project_id, zone_name, element, cutoff_grade,
  holes_in_zone, total_length_m, avg_grade, unit,
  max_grade, avg_true_width, computed_at
)
SELECT
  workspace_id, project_id, zone_name, element, cutoff_grade,
  holes_in_zone, total_length_m, avg_grade, 'ppm',
  max_grade, avg_true_width, NOW()
FROM zones
ON CONFLICT (workspace_id, zone_name, element, cutoff_grade) DO UPDATE SET
  holes_in_zone  = EXCLUDED.holes_in_zone,
  total_length_m = EXCLUDED.total_length_m,
  avg_grade      = EXCLUDED.avg_grade,
  max_grade      = EXCLUDED.max_grade,
  avg_true_width = EXCLUDED.avg_true_width,
  computed_at    = NOW()
"""


@asset(
    group_name="drillhole_gold",
    description=(
        "Per-zone grade/thickness statistics aggregated from "
        "gold.significant_intersections. Rows without a zone_name "
        "label are bucketed under '__unzoned'."
    ),
)
def gold_zone_statistics(
    context: AssetExecutionContext,
    config: ZoneStatisticsConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    pg = postgres.connect()
    rows = 0
    try:
        with pg.cursor() as cur:
            cur.execute(_UPSERT_SQL, (config.workspace_id,))
            rows = cur.rowcount or 0
        pg.commit()
    finally:
        pg.close()
    return MaterializeResult(metadata={"zones_upserted": MetadataValue.int(rows)})
