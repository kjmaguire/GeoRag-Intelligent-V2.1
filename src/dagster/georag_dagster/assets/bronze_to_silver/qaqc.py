"""bronze.raw_qaqc_submissions → silver.qaqc_results.

Straight pass-through transform — the pass/fail logic is enforced
by the GENERATED column on the silver.qaqc_results table itself
(blanks > 3× expected, CRMs > tolerance_pct), so the asset only
needs to copy + normalise fields.
"""
import psycopg2.extras
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.resources import PostgresResource


class SilverQaqcConfig(Config):
    workspace_id: str
    default_tolerance_pct: float = 10.0


_UPSERT_SQL = """
INSERT INTO silver.qaqc_results (
    workspace_id, sample_id, qaqc_type, standard_ref, element,
    expected_value, reported_value, unit, tolerance_pct,
    bronze_source_id
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id) DO NOTHING
"""


@asset(
    group_name="drillhole_silver",
    description=(
        "Validated QA/QC samples. The pass/fail decision is computed "
        "on the silver column via STORED GENERATED; this asset only "
        "copies + normalises units from the bronze submission."
    ),
)
def silver_qaqc_results(
    context: AssetExecutionContext,
    config: SilverQaqcConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    pg = postgres.connect()
    rows_in = 0
    rows_written = 0

    try:
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, sample_id, qaqc_type, standard_ref, element,
                       expected_value, reported_value, reported_unit
                  FROM bronze.raw_qaqc_submissions
                 WHERE workspace_id = %s::uuid
                """,
                (config.workspace_id,),
            )
            bronze_rows = cur.fetchall()

        rows_in = len(bronze_rows)
        with pg.cursor() as upsert_cur:
            for row in bronze_rows:
                upsert_cur.execute(
                    _UPSERT_SQL,
                    (
                        config.workspace_id, row["sample_id"],
                        row["qaqc_type"], row["standard_ref"], row["element"],
                        row["expected_value"], row["reported_value"],
                        row["reported_unit"] or "ppm",
                        config.default_tolerance_pct,
                        row["id"],
                    ),
                )
                rows_written += 1

        pg.commit()
    finally:
        pg.close()

    return MaterializeResult(
        metadata={
            "rows_in": MetadataValue.int(rows_in),
            "rows_written": MetadataValue.int(rows_written),
        },
    )
