"""silver.qaqc_results → gold.qaqc_statistics.

Per-(lab, element, qaqc_type) pass-rate rollup. The rolling window is
the current calendar quarter — narrower than monthly means we catch
problem labs faster, wider than weekly avoids quarter-end noise.
"""
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.resources import PostgresResource


class QaqcStatisticsConfig(Config):
    workspace_id: str


_UPSERT_SQL = """
WITH window AS (
  SELECT date_trunc('quarter', NOW()) AS period_start,
         date_trunc('quarter', NOW()) + interval '3 months' AS period_end
),
agg AS (
  SELECT
    qr.workspace_id,
    (SELECT period_start FROM window) AS period_start,
    (SELECT period_end   FROM window) AS period_end,
    sd.lab_name,
    qr.element,
    qr.qaqc_type,
    COUNT(*) AS samples_submitted,
    COUNT(*) FILTER (WHERE qr.pass_fail = 'pass') AS samples_passed,
    AVG(
      CASE WHEN qr.expected_value <> 0
           THEN abs(qr.reported_value - qr.expected_value)
                / qr.expected_value * 100
      END
    ) AS avg_error_pct
  FROM silver.qaqc_results qr
  LEFT JOIN silver.sample_dispatches sd ON sd.id = qr.dispatch_id
  WHERE qr.workspace_id = %s::uuid
    AND qr.created_at >= (SELECT period_start FROM window)
    AND qr.created_at <  (SELECT period_end   FROM window)
  GROUP BY qr.workspace_id, sd.lab_name, qr.element, qr.qaqc_type
)
INSERT INTO gold.qaqc_statistics (
  workspace_id, period_start, period_end,
  lab_name, element, qaqc_type,
  samples_submitted, samples_passed, avg_error_pct, computed_at
)
SELECT
  workspace_id, period_start, period_end,
  lab_name, element, qaqc_type,
  samples_submitted, samples_passed, avg_error_pct, NOW()
FROM agg
"""

_CLEAR_FOR_PERIOD = """
DELETE FROM gold.qaqc_statistics
 WHERE workspace_id = %s::uuid
   AND period_start = date_trunc('quarter', NOW())
"""


@asset(
    group_name="drillhole_gold",
    description=(
        "Per-quarter QA/QC pass-rate rollup by lab + element + "
        "qaqc_type. pass_rate_pct is a GENERATED column on the "
        "target table so the DB computes it from samples_passed / "
        "samples_submitted."
    ),
)
def gold_qaqc_statistics(
    context: AssetExecutionContext,
    config: QaqcStatisticsConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    pg = postgres.connect()
    rows_written = 0
    try:
        with pg.cursor() as cur:
            cur.execute(_CLEAR_FOR_PERIOD, (config.workspace_id,))
            cur.execute(_UPSERT_SQL, (config.workspace_id,))
            rows_written = cur.rowcount or 0
        pg.commit()
    finally:
        pg.close()
    return MaterializeResult(
        metadata={"rows_written": MetadataValue.int(rows_written)},
    )
