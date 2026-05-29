"""Asset checks for Index-layer assets.

Index-layer checks are the last gate before commit_ingestion_run runs the
data_version bump.  An embedding_id_present check on index_reports ensures
no passage was committed to Silver without a corresponding Qdrant point id.

NOTE: Do NOT add `from __future__ import annotations` to this file.
"""

import psycopg2.extras
from dagster import (
    AssetCheckExecutionContext,
    AssetCheckResult,
    AssetCheckSeverity,
    asset_check,
)

from georag_dagster.resources import PostgresResource

# Minimum acceptable parser quality ratio before raising an error.
# Below this threshold (0 = catastrophic) the check blocks; above it
# the check passes (WARN-severity is emitted for Phase C measurement).
_CATASTROPHIC_FAILURE_RATIO = 0.0


@asset_check(
    asset="index_reports",
    name="embedding_id_present",
    description=(
        "Blocks commit if any silver.reports row has an empty embedding_ids array. "
        "A report without embeddings cannot be retrieved by the RAG layer — it is "
        "effectively invisible to the system despite being in Silver."
    ),
    blocking=True,
)
def index_reports_check_embedding_id_present(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify all silver.reports rows have at least one embedding_id."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE embedding_ids IS NULL
                           OR cardinality(embedding_ids) = 0
                    ) AS missing_embeddings
                FROM silver.reports;
            """)
            row = cur.fetchone()

    total = row[0] if row else 0
    missing = row[1] if row else 0

    # Pass vacuously if the table is empty (first run before any reports are ingested).
    # The silver_reports parse_total_positive check handles the empty-table case.
    passed = missing == 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"All {total} reports have embedding_ids populated."
            if passed
            else f"{missing} of {total} reports have no embedding_ids — blocking commit."
        ),
        metadata={
            "total_reports": total,
            "missing_embedding_count": missing,
            "parse_total": total,
            "parse_ok": total - missing,
            "parse_failed": missing,
            "parse_ratio": (total - missing) / total if total > 0 else 1.0,
            "parser_name": "index_reports",
            "parser_version": "1.0",
        },
    )


@asset_check(
    asset="index_reports",
    name="parser_error_floor",
    description=(
        "Blocking only on a catastrophic failure rate (0% of reports indexed). "
        "WARN otherwise — partial embedding failures are tolerable (broken PDFs "
        "stay in Bronze, not lost). This check enforces the parser_error_floor "
        "specified in spec B1."
    ),
    blocking=True,
)
def index_reports_check_parser_quality_floor(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Block only when the embedding pass rate is exactly 0%."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE embedding_ids IS NOT NULL
                          AND cardinality(embedding_ids) > 0
                    ) AS embedded_count
                FROM silver.reports;
            """)
            row = cur.fetchone()

    total = row[0] if row else 0
    embedded = row[1] if row else 0
    failed = total - embedded
    ratio = embedded / total if total > 0 else 1.0

    # Catastrophic failure: non-empty table but zero embeddings
    catastrophic = total > 0 and embedded == 0
    passed = not catastrophic
    severity = AssetCheckSeverity.ERROR if catastrophic else AssetCheckSeverity.WARN

    return AssetCheckResult(
        passed=passed,
        severity=severity,
        description=(
            f"Index pass rate: {embedded}/{total} reports embedded (ratio={ratio:.2%})."
            if passed
            else f"Catastrophic: 0/{total} reports embedded — blocking commit."
        ),
        metadata={
            "parse_total": total,
            "parse_ok": embedded,
            "parse_failed": failed,
            "parse_ratio": ratio,
            "parser_name": "index_reports",
            "parser_version": "1.0",
        },
    )
