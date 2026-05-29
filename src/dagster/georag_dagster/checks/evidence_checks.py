"""Asset checks for evidence-model tables.

These checks validate data written to:
  - silver.document_passages  (B7 passage store)
  - silver.document_revisions (B8 evidence model)
  - silver.evidence_items     (B8 evidence model)

The DB-level CHECK constraints (text_hash format, revision_positive, exactly-one-ref)
are the authoritative safety net.  These Dagster checks are fast-fail feedback
that surfaces issues BEFORE the INSERT reaches the DB, providing actionable
metadata in the Dagster UI without waiting for a constraint violation stacktrace.

NOTE: Do NOT add `from __future__ import annotations` to this file.
"""

import re

import psycopg2.extras
from dagster import (
    AssetCheckExecutionContext,
    AssetCheckResult,
    AssetCheckSeverity,
    asset_check,
)

from georag_dagster.resources import PostgresResource

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# silver.document_passages checks
# ---------------------------------------------------------------------------

@asset_check(
    asset="silver_reports",
    name="no_duplicate_passage_ids",
    description=(
        "Blocks commit if silver.document_passages contains duplicate passage_id "
        "values. Duplicate passage_ids break the stable citation pointer contract "
        "in §10p-i — a passage_id must uniquely identify one chunk across all "
        "re-ingestion runs."
    ),
    blocking=True,
)
def document_passages_check_no_duplicate_passage_ids(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify no duplicate passage_id values in silver.document_passages."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS dup_count
                FROM (
                    SELECT passage_id, COUNT(*) AS n
                    FROM silver.document_passages
                    GROUP BY passage_id
                    HAVING COUNT(*) > 1
                ) AS dups;
            """)
            row = cur.fetchone()
    dup_count = row[0] if row else 0
    passed = dup_count == 0

    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM silver.document_passages;")
            total_row = cur.fetchone()
    total = total_row[0] if total_row else 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"No duplicate passage_ids in {total} passage rows."
            if passed
            else f"{dup_count} duplicate passage_id group(s) found — blocking commit."
        ),
        metadata={
            "total_passages": total,
            "duplicate_passage_id_groups": dup_count,
            "parser_name": "pdf_report",
            "parser_version": "1.0",
        },
    )


@asset_check(
    asset="silver_reports",
    name="text_hash_sha256_valid",
    description=(
        "Blocks commit if any passage has a text_hash that is not a 64-character "
        "lowercase hexadecimal string (SHA-256 format). Invalid hashes break "
        "passage_id stability across re-ingestion runs."
    ),
    blocking=True,
)
def document_passages_check_text_hash_sha256_valid(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify all document_passages.text_hash values match ^[0-9a-f]{64}$."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE text_hash IS NULL
                           OR text_hash !~ '^[0-9a-f]{64}$'
                    ) AS invalid_hash_count
                FROM silver.document_passages;
            """)
            row = cur.fetchone()

    total = row[0] if row else 0
    invalid = row[1] if row else 0
    passed = invalid == 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"All {total} passage text_hash values are valid SHA-256."
            if passed
            else f"{invalid} of {total} passages have invalid text_hash — blocking commit."
        ),
        metadata={
            "total_passages": total,
            "invalid_text_hash_count": invalid,
            "parser_name": "pdf_report",
            "parser_version": "1.0",
        },
    )


# ---------------------------------------------------------------------------
# silver.document_revisions checks
# ---------------------------------------------------------------------------

@asset_check(
    asset="silver_reports",
    name="document_revisions_document_id_not_null",
    description=(
        "Blocks commit if any document_revisions row has a NULL document_id. "
        "document_id is the FK to silver.reports and must be present for "
        "every revision — a NULL means a dangling revision with no parent document."
    ),
    blocking=True,
)
def document_revisions_check_document_id_not_null(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify silver.document_revisions has no NULL document_id values."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE document_id IS NULL) AS null_doc_id
                FROM silver.document_revisions;
            """)
            row = cur.fetchone()

    total = row[0] if row else 0
    null_count = row[1] if row else 0
    passed = null_count == 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"All {total} document_revisions rows have non-null document_id."
            if passed
            else f"{null_count} document_revisions rows have NULL document_id — blocking commit."
        ),
        metadata={
            "total_revisions": total,
            "null_document_id_count": null_count,
            "parser_name": "pdf_report",
            "parser_version": "1.0",
        },
    )


@asset_check(
    asset="silver_reports",
    name="document_revisions_sha256_format",
    description=(
        "Blocks commit if any document_revisions.source_sha256 does not match "
        "the ^[0-9a-f]{64}$ pattern. Invalid SHA-256 values break lineage tracing "
        "from revision back to the immutable Bronze object."
    ),
    blocking=True,
)
def document_revisions_check_sha256_format(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify all document_revisions.source_sha256 values are valid SHA-256."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE source_sha256 !~ '^[0-9a-f]{64}$'
                    ) AS invalid_sha256
                FROM silver.document_revisions;
            """)
            row = cur.fetchone()

    total = row[0] if row else 0
    invalid = row[1] if row else 0
    passed = invalid == 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"All {total} document_revisions rows have valid SHA-256."
            if passed
            else f"{invalid} document_revisions rows have invalid SHA-256 — blocking commit."
        ),
        metadata={
            "total_revisions": total,
            "invalid_sha256_count": invalid,
            "parser_name": "pdf_report",
            "parser_version": "1.0",
        },
    )


# ---------------------------------------------------------------------------
# silver.evidence_items checks
# ---------------------------------------------------------------------------

@asset_check(
    asset="silver_reports",
    name="evidence_items_exactly_one_ref",
    description=(
        "Blocks commit if any evidence_items row violates the exactly-one-of "
        "constraint (passage_id, structured_ref, graph_edge_ref, map_feature_ref). "
        "This mirrors the DB-level CHECK constraint but catches the violation "
        "pre-INSERT for faster feedback."
    ),
    blocking=True,
)
def evidence_items_check_exactly_one_ref(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify all evidence_items rows have exactly one non-null reference field."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE (
                            (passage_id IS NOT NULL)::int +
                            (structured_ref IS NOT NULL)::int +
                            (graph_edge_ref IS NOT NULL)::int +
                            (map_feature_ref IS NOT NULL)::int
                        ) != 1
                    ) AS bad_ref_count
                FROM silver.evidence_items;
            """)
            row = cur.fetchone()

    total = row[0] if row else 0
    bad = row[1] if row else 0
    passed = bad == 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"All {total} evidence_items rows have exactly one reference field."
            if passed
            else f"{bad} evidence_items rows violate the exactly-one-ref constraint — blocking commit."
        ),
        metadata={
            "total_evidence_items": total,
            "bad_ref_count": bad,
            "parser_name": "pdf_report",
            "parser_version": "1.0",
        },
    )
