"""Asset checks for silver_drill_traces.

Single blocking check: desurvey_trace_count_matches_collar_count_with_surveys.

ADR-0007 PR-4 changed the contract: the asset now also writes a
straight-line trace for collars that have NO surveys but DO have a usable
collar orientation (azimuth + dip + total_depth). The check therefore
verifies::

    trace_count == collars_with_surveys + collars_with_straight_line_orientation

Collars that have neither surveys nor a usable collar orientation are
counted under `unusable_collars_skipped` and are not expected to appear
in silver.drill_traces.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that
import breaks runtime annotation evaluation.
"""

from dagster import (
    AssetCheckExecutionContext,
    AssetCheckResult,
    AssetCheckSeverity,
    MetadataValue,
    asset_check,
)

from georag_dagster.resources import PostgresResource


@asset_check(
    asset="silver_drill_traces",
    name="desurvey_trace_count_matches_collar_count_with_surveys",
    description=(
        "Blocking: verifies that silver.drill_traces row count equals "
        "(distinct collars with surveys) + (collars with no surveys but a "
        "usable straight-line orientation). Any mismatch indicates the "
        "asset silently dropped a collar that should have produced a trace."
    ),
    blocking=True,
)
def desurvey_trace_count_matches_collar_count_with_surveys(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify trace count = surveyed + straight-line eligible collars."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            # Distinct collars that have at least one survey row.
            cur.execute(
                "SELECT COUNT(DISTINCT collar_id) FROM silver.surveys;"
            )
            row = cur.fetchone()
            collars_with_surveys = int(row[0]) if row else 0

            # Traces written.
            cur.execute("SELECT COUNT(*) FROM silver.drill_traces;")
            row = cur.fetchone()
            trace_count = int(row[0]) if row else 0

            # ADR-0007 PR-4 — collars with no surveys but a positive
            # total_depth produce a straight-line trace. Missing
            # azimuth/dip default to vertical (az=0, dip=-90) per the
            # asset's industry-standard fallback. The only collars we
            # skip are the ones with no usable total_depth.
            cur.execute(
                """
                SELECT COUNT(*)
                FROM silver.collars c
                WHERE NOT EXISTS (
                    SELECT 1 FROM silver.surveys s
                    WHERE s.collar_id = c.collar_id
                )
                  AND c.total_depth IS NOT NULL
                  AND c.total_depth > 0;
                """
            )
            row = cur.fetchone()
            straight_line_eligible = int(row[0]) if row else 0

            # Collars with no surveys AND no usable total_depth —
            # legitimately skipped. Reported for diagnostics.
            cur.execute(
                """
                SELECT COUNT(*)
                FROM silver.collars c
                WHERE NOT EXISTS (
                    SELECT 1 FROM silver.surveys s
                    WHERE s.collar_id = c.collar_id
                )
                  AND (c.total_depth IS NULL OR c.total_depth <= 0);
                """
            )
            row = cur.fetchone()
            unusable_collars = int(row[0]) if row else 0

    expected = collars_with_surveys + straight_line_eligible
    passed = trace_count == expected

    if passed:
        description = (
            f"trace_count={trace_count} == surveyed({collars_with_surveys}) + "
            f"straight_line_eligible({straight_line_eligible}). "
            f"unusable_collars_skipped={unusable_collars}."
        )
    else:
        description = (
            f"MISMATCH: trace_count={trace_count} != "
            f"surveyed({collars_with_surveys}) + "
            f"straight_line_eligible({straight_line_eligible}) = {expected}. "
            f"unusable_collars_skipped={unusable_collars}. "
            "Check silver_drill_traces logs for error_count or invalid-az/dip drops."
        )

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=description,
        metadata={
            "trace_count":              MetadataValue.int(trace_count),
            "collars_with_surveys":     MetadataValue.int(collars_with_surveys),
            "straight_line_eligible":   MetadataValue.int(straight_line_eligible),
            "unusable_collars_skipped": MetadataValue.int(unusable_collars),
            "expected_trace_count":     MetadataValue.int(expected),
        },
    )
