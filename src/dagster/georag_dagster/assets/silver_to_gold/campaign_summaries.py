"""silver.* → gold.campaign_summaries.

One row per drilling campaign. Aggregates the campaign's hole list
+ best intercepts + QA/QC pass rate into a single dashboard row.
"""
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.resources import PostgresResource


class CampaignSummariesConfig(Config):
    workspace_id: str


_UPSERT_SQL = """
WITH holes AS (
  SELECT campaign_id, collar_id, total_depth
    FROM silver.collars
   WHERE workspace_id = %s::uuid AND campaign_id IS NOT NULL
),
best AS (
  SELECT
    c.campaign_id,
    si.element,
    si.weighted_avg,
    ROW_NUMBER() OVER (
      PARTITION BY c.campaign_id
      ORDER BY si.weighted_avg DESC NULLS LAST
    ) AS rn
  FROM gold.significant_intersections si
  JOIN silver.collars c ON c.collar_id = si.collar_id
  WHERE c.campaign_id IS NOT NULL
),
qaqc AS (
  -- Simple pass-rate per workspace; no campaign FK on qaqc_results
  -- so we approximate by workspace and let the dashboard scope it.
  SELECT workspace_id,
         AVG(CASE WHEN pass_fail = 'pass' THEN 100.0 ELSE 0 END) AS pass_rate
    FROM silver.qaqc_results
   GROUP BY workspace_id
)
INSERT INTO gold.campaign_summaries (
  workspace_id, campaign_id,
  holes_completed, total_metres, avg_hole_depth,
  elements_assayed,
  best_intersection_grade, best_intersection_element,
  qaqc_pass_rate, computed_at
)
SELECT
  cmp.workspace_id,
  cmp.id AS campaign_id,
  COUNT(DISTINCT h.collar_id) AS holes_completed,
  COALESCE(SUM(h.total_depth), 0) AS total_metres,
  AVG(h.total_depth) AS avg_hole_depth,
  ARRAY(SELECT DISTINCT a.element
          FROM silver.assays_v2 a
          JOIN silver.collars c2 ON c2.collar_id = a.collar_id
         WHERE c2.campaign_id = cmp.id
         ORDER BY 1) AS elements_assayed,
  MAX(CASE WHEN b.rn = 1 THEN b.weighted_avg END) AS best_intersection_grade,
  MAX(CASE WHEN b.rn = 1 THEN b.element END) AS best_intersection_element,
  q.pass_rate AS qaqc_pass_rate,
  NOW()
FROM silver.campaigns cmp
LEFT JOIN holes h ON h.campaign_id = cmp.id
LEFT JOIN best  b ON b.campaign_id = cmp.id
LEFT JOIN qaqc  q ON q.workspace_id = cmp.workspace_id
WHERE cmp.workspace_id = %s::uuid
GROUP BY cmp.workspace_id, cmp.id, q.pass_rate
ON CONFLICT (campaign_id) DO UPDATE SET
  holes_completed = EXCLUDED.holes_completed,
  total_metres    = EXCLUDED.total_metres,
  avg_hole_depth  = EXCLUDED.avg_hole_depth,
  elements_assayed = EXCLUDED.elements_assayed,
  best_intersection_grade = EXCLUDED.best_intersection_grade,
  best_intersection_element = EXCLUDED.best_intersection_element,
  qaqc_pass_rate  = EXCLUDED.qaqc_pass_rate,
  computed_at     = NOW()
"""


@asset(
    group_name="drillhole_gold",
    description="One row per drilling campaign — hole list + best intercepts + QA/QC pass rate.",
)
def gold_campaign_summaries(
    context: AssetExecutionContext,
    config: CampaignSummariesConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    pg = postgres.connect()
    rows = 0
    try:
        with pg.cursor() as cur:
            cur.execute(_UPSERT_SQL, (config.workspace_id, config.workspace_id))
            rows = cur.rowcount or 0
        pg.commit()
    finally:
        pg.close()
    return MaterializeResult(metadata={"campaigns_upserted": MetadataValue.int(rows)})
