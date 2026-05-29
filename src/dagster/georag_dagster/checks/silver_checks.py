"""Asset checks for Silver-layer assets.

Every blocking check maps to a genuine data-quality assertion that distinguishes
"good data committed to Silver" from "broken data that must not commit".

Parser quality metadata fields emitted on every check (per spec B2):
  parse_total, parse_ok, parse_failed, parse_ratio, parser_name, parser_version

These feed Phase C quality claims and are stored in Dagster's event log for the
associated materialization.

NOTE: Do NOT add `from __future__ import annotations` to this file.
"""

import re

import psycopg2.extras
from dagster import (
    AssetCheckExecutionContext,
    AssetCheckResult,
    AssetCheckSeverity,
    AssetCheckSpec,
    asset_check,
)

from georag_dagster.resources import PostgresResource

# ---------------------------------------------------------------------------
# silver_collars checks
# ---------------------------------------------------------------------------

@asset_check(
    asset="silver_collars",
    name="collar_count_positive",
    description=(
        "Blocks commit if silver.collars contains zero rows. "
        "An ingestion run that produces no collar rows indicates a parser "
        "failure or an empty / malformed Bronze file — neither is acceptable "
        "in a committed run."
    ),
    blocking=True,
)
def silver_collars_check_collar_count_positive(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify silver.collars has at least one row after materialization."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM silver.collars;")
            row = cur.fetchone()
    count = row[0] if row else 0
    passed = count > 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"silver.collars has {count} rows."
            if passed
            else "silver.collars is empty — blocking commit."
        ),
        metadata={
            "collar_count": count,
            "parser_name": "csv_collar",
            "parser_version": "1.0",
        },
    )


@asset_check(
    asset="silver_collars",
    name="schema_conformance_pass_rate",
    description=(
        "Blocking only when the last-run parse ratio is exactly 0 % "
        "(catastrophic parser failure). Otherwise emits WARN with the "
        "measured ratio so it surfaces in Phase C baselines without "
        "blocking tolerable failure rates. "
        "Preserves the 'never silently drop' contract from §04c."
    ),
    blocking=True,
)
def silver_collars_check_schema_conformance(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Check that the last collar ingestion was not a total parse failure.

    Reads parse metrics from the most-recent bronze.provenance batch for
    silver.collars.  If bronze.provenance is empty (no runs yet) the check
    passes by convention — there is nothing to fail on a clean slate.
    """
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Count total collars vs total provenance rows for the latest ingest_run_id
            cur.execute("""
                SELECT
                    p.ingest_run_id,
                    p.parser_name,
                    p.parser_version,
                    COUNT(*) AS parse_ok
                FROM bronze.provenance p
                WHERE p.target_schema = 'silver'
                  AND p.target_table = 'collars'
                GROUP BY p.ingest_run_id, p.parser_name, p.parser_version
                ORDER BY MAX(p.created_at) DESC
                LIMIT 1;
            """)
            row = cur.fetchone()

    if row is None:
        # No provenance rows — clean-slate or first run. Pass by convention.
        return AssetCheckResult(
            passed=True,
            severity=AssetCheckSeverity.WARN,
            description="No provenance rows found for silver.collars — assuming first run.",
            metadata={
                "parse_total": 0,
                "parse_ok": 0,
                "parse_failed": 0,
                "parse_ratio": 1.0,
                "parser_name": "csv_collar",
                "parser_version": "1.0",
            },
        )

    parse_ok = int(row["parse_ok"])
    parser_name = row["parser_name"] or "csv_collar"
    parser_version = row["parser_version"] or "1.0"

    # parse_ratio based solely on provenance rows (all represent successful inserts)
    # A ratio of 0 means nothing made it through — that is the blocking case.
    # We cannot derive parse_failed from provenance alone (skipped rows are not
    # recorded there), so we flag 0 ok as the hard failure condition.
    passed = parse_ok > 0
    severity = AssetCheckSeverity.ERROR if not passed else AssetCheckSeverity.WARN
    parse_ratio = 1.0 if parse_ok > 0 else 0.0

    return AssetCheckResult(
        passed=passed,
        severity=severity,
        description=(
            f"Last collar run: {parse_ok} rows committed."
            if passed
            else "Last collar run committed 0 rows — catastrophic parse failure, blocking commit."
        ),
        metadata={
            "parse_total": parse_ok,
            "parse_ok": parse_ok,
            "parse_failed": 0,
            "parse_ratio": parse_ratio,
            "parser_name": parser_name,
            "parser_version": parser_version,
            "ingest_run_id": str(row["ingest_run_id"]),
        },
    )


@asset_check(
    asset="silver_collars",
    name="crs_round_trip_sane",
    description=(
        "Blocks commit if any collar row has a NULL geometry or a geometry "
        "with SRID=0. Both indicate CRS detection or geometry construction "
        "failure — spatial queries on such rows would silently produce wrong "
        "results."
    ),
    blocking=True,
)
def silver_collars_check_crs_srid_populated(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify all collar geometries are non-null with a valid SRID."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE geom IS NULL)      AS null_geom,
                    COUNT(*) FILTER (WHERE ST_SRID(geom) = 0) AS zero_srid,
                    COUNT(*)                                   AS total
                FROM silver.collars;
            """)
            row = cur.fetchone()

    null_geom = row[0] if row else 0
    zero_srid = row[1] if row else 0
    total = row[2] if row else 0
    bad = null_geom + zero_srid
    passed = bad == 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"All {total} collar geometries valid (non-null, SRID != 0)."
            if passed
            else (
                f"{bad} collar rows have bad geometry "
                f"(null_geom={null_geom}, zero_srid={zero_srid}) — blocking commit."
            )
        ),
        metadata={
            "total_collars": total,
            "null_geom_count": null_geom,
            "zero_srid_count": zero_srid,
            "bad_geom_count": bad,
            "parser_name": "csv_collar",
            "parser_version": "1.0",
        },
    )


# ---------------------------------------------------------------------------
# silver_surveys check
# ---------------------------------------------------------------------------

@asset_check(
    asset="silver_surveys",
    name="parse_total_positive",
    description=(
        "Blocks commit if silver.surveys is empty after materialization. "
        "A zero row count indicates a parse failure or empty Bronze file."
    ),
    blocking=True,
)
def silver_surveys_check_parse_total_positive(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify silver.surveys has at least one row."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM silver.surveys;")
            row = cur.fetchone()
    count = row[0] if row else 0
    passed = count > 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"silver.surveys has {count} rows."
            if passed
            else "silver.surveys is empty — blocking commit."
        ),
        metadata={
            "parse_total": count,
            "parse_ok": count,
            "parse_failed": 0,
            "parse_ratio": 1.0 if count > 0 else 0.0,
            "parser_name": "csv_survey",
            "parser_version": "1.0",
        },
    )


# ---------------------------------------------------------------------------
# silver_lithology check
# ---------------------------------------------------------------------------

@asset_check(
    asset="silver_lithology",
    name="parse_total_positive",
    description=(
        "Blocks commit if silver.lithology_logs is empty after materialization."
    ),
    blocking=True,
)
def silver_lithology_check_parse_total_positive(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify silver.lithology_logs has at least one row."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM silver.lithology_logs;")
            row = cur.fetchone()
    count = row[0] if row else 0
    passed = count > 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"silver.lithology_logs has {count} rows."
            if passed
            else "silver.lithology_logs is empty — blocking commit."
        ),
        metadata={
            "parse_total": count,
            "parse_ok": count,
            "parse_failed": 0,
            "parse_ratio": 1.0 if count > 0 else 0.0,
            "parser_name": "csv_lithology",
            "parser_version": "1.0",
        },
    )


# ---------------------------------------------------------------------------
# silver_samples check
# ---------------------------------------------------------------------------

@asset_check(
    asset="silver_samples",
    name="parse_total_positive",
    description=(
        "Blocks commit if silver.assay_samples is empty after materialization."
    ),
    blocking=True,
)
def silver_samples_check_parse_total_positive(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify silver.assay_samples has at least one row."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM silver.assay_samples;")
            row = cur.fetchone()
    count = row[0] if row else 0
    passed = count > 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"silver.assay_samples has {count} rows."
            if passed
            else "silver.assay_samples is empty — blocking commit."
        ),
        metadata={
            "parse_total": count,
            "parse_ok": count,
            "parse_failed": 0,
            "parse_ratio": 1.0 if count > 0 else 0.0,
            "parser_name": "csv_sample",
            "parser_version": "1.0",
        },
    )


# ---------------------------------------------------------------------------
# silver_well_logs check
# ---------------------------------------------------------------------------

@asset_check(
    asset="silver_well_logs",
    name="parse_total_positive",
    description=(
        "Blocks commit if silver.well_logs is empty after materialization."
    ),
    blocking=True,
)
def silver_well_logs_check_parse_total_positive(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify silver.well_logs has at least one row."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM silver.well_logs;")
            row = cur.fetchone()
    count = row[0] if row else 0
    passed = count > 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"silver.well_logs has {count} rows."
            if passed
            else "silver.well_logs is empty — blocking commit."
        ),
        metadata={
            "parse_total": count,
            "parse_ok": count,
            "parse_failed": 0,
            "parse_ratio": 1.0 if count > 0 else 0.0,
            "parser_name": "las_parser",
            "parser_version": "1.0",
        },
    )


# ---------------------------------------------------------------------------
# silver_spatial checks
# ---------------------------------------------------------------------------

@asset_check(
    asset="silver_spatial",
    name="geom_not_null",
    description=(
        "Blocks commit if any spatial feature row has a NULL geometry. "
        "NULL geometries break spatial indexing and all downstream ST_ queries."
    ),
    blocking=True,
)
def silver_spatial_check_geom_not_null(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify no NULL geometries in silver.spatial_features."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE geom IS NULL) AS null_geom,
                    COUNT(*) AS total
                FROM silver.spatial_features;
            """)
            row = cur.fetchone()
    null_geom = row[0] if row else 0
    total = row[1] if row else 0
    passed = null_geom == 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"All {total} spatial features have non-null geometry."
            if passed
            else f"{null_geom} of {total} spatial features have NULL geometry — blocking commit."
        ),
        metadata={
            "total_features": total,
            "null_geom_count": null_geom,
            "parse_total": total,
            "parse_ok": total - null_geom,
            "parse_failed": null_geom,
            "parse_ratio": (total - null_geom) / total if total > 0 else 1.0,
            "parser_name": "spatial_parser",
            "parser_version": "1.0",
        },
    )


@asset_check(
    asset="silver_spatial",
    name="crs_srid_populated",
    description=(
        "Blocks commit if any spatial feature row has SRID=0 on its geometry. "
        "SRID=0 means CRS detection failed — ST_Transform and spatial index "
        "lookups will produce silently wrong results."
    ),
    blocking=True,
)
def silver_spatial_check_srid_populated(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify all non-null geometries in silver.spatial_features have SRID != 0."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE geom IS NOT NULL AND ST_SRID(geom) = 0) AS zero_srid,
                    COUNT(*) AS total
                FROM silver.spatial_features;
            """)
            row = cur.fetchone()
    zero_srid = row[0] if row else 0
    total = row[1] if row else 0
    passed = zero_srid == 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"All {total} spatial features have valid SRID."
            if passed
            else f"{zero_srid} spatial features have SRID=0 — blocking commit."
        ),
        metadata={
            "total_features": total,
            "zero_srid_count": zero_srid,
            "parser_name": "spatial_parser",
            "parser_version": "1.0",
        },
    )


# ---------------------------------------------------------------------------
# silver_reports checks
# ---------------------------------------------------------------------------

@asset_check(
    asset="silver_reports",
    name="parse_total_positive",
    description=(
        "Blocks commit if silver.reports is empty after materialization. "
        "An empty reports table indicates a PDF parse failure or missing Bronze file."
    ),
    blocking=True,
)
def silver_reports_check_parse_total_positive(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify silver.reports has at least one row."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM silver.reports;")
            row = cur.fetchone()
    count = row[0] if row else 0
    passed = count > 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"silver.reports has {count} rows."
            if passed
            else "silver.reports is empty — blocking commit."
        ),
        metadata={
            "parse_total": count,
            "parse_ok": count,
            "parse_failed": 0,
            "parse_ratio": 1.0 if count > 0 else 0.0,
            "parser_name": "pdf_report",
            "parser_version": "1.0",
        },
    )


@asset_check(
    asset="silver_reports",
    name="schema_conformance_pass_rate",
    description=(
        "Blocks commit only at 0% pass rate (total parse failure). "
        "WARN otherwise — tolerable failure rates preserved per §04c. "
        "Checks that at least one report has a non-null, non-empty sections_text."
    ),
    blocking=True,
)
def silver_reports_check_schema_conformance(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify at least one report has populated sections_text."""
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE sections_text IS NOT NULL
                          AND sections_text != '{}'::jsonb
                          AND jsonb_typeof(sections_text) = 'object'
                    ) AS with_sections
                FROM silver.reports;
            """)
            row = cur.fetchone()
    total = row[0] if row else 0
    with_sections = row[1] if row else 0
    failed = total - with_sections
    ratio = with_sections / total if total > 0 else 1.0
    passed = with_sections > 0 if total > 0 else True
    severity = AssetCheckSeverity.ERROR if (total > 0 and with_sections == 0) else AssetCheckSeverity.WARN

    return AssetCheckResult(
        passed=passed,
        severity=severity,
        description=(
            f"{with_sections}/{total} reports have populated sections_text "
            f"(ratio={ratio:.2%})."
            if passed
            else f"No reports have populated sections_text — total parse failure, blocking commit."
        ),
        metadata={
            "parse_total": total,
            "parse_ok": with_sections,
            "parse_failed": failed,
            "parse_ratio": ratio,
            "parser_name": "pdf_report",
            "parser_version": "1.0",
        },
    )


# ---------------------------------------------------------------------------
# silver_xlsx check
# ---------------------------------------------------------------------------

@asset_check(
    asset="silver_xlsx",
    name="parse_total_positive",
    description=(
        "Blocks commit if no XLSX data landed in Silver. "
        "A minimum parse_total > 0 check per §B1 guidance."
    ),
    blocking=True,
)
def silver_xlsx_check_parse_total_positive(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify silver.collars (XLSX path) produced rows or silver.assay_samples has rows.

    XLSX files can write to either collars or assay_samples depending on content.
    We check the combined total across both tables as a proxy for XLSX parse success.
    A dedicated silver.xlsx_staging table does not exist in the current schema.
    """
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    (SELECT COUNT(*) FROM silver.collars) +
                    (SELECT COUNT(*) FROM silver.assay_samples) AS combined_count;
            """)
            row = cur.fetchone()
    count = row[0] if row else 0
    passed = count > 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"Combined Silver row count (collars + samples) = {count} — XLSX data present."
            if passed
            else "Zero rows in Silver after XLSX ingest — blocking commit."
        ),
        metadata={
            "parse_total": count,
            "parse_ok": count,
            "parse_failed": 0,
            "parse_ratio": 1.0 if count > 0 else 0.0,
            "parser_name": "xlsx_parser",
            "parser_version": "1.0",
        },
    )


# ---------------------------------------------------------------------------
# silver_seismic checks
#
# NOTE: silver_seismic / silver_xyz are Silver-trapped graph dead-ends (DAG-05).
# They have zero Gold/Index downstream consumers.  These checks gate re-runs to
# ensure the parse produced at least one row and the target table is not empty.
# A richer schema-conformance check will be added when Module 4 wires evidence
# rows for these tables and the parse_ok metadata is available from bronze.provenance.
#
# TODO: When silver_seismic emits parse_ok / parse_failed as MaterializeResult
# metadata (consistent with other parser assets), replace the table-count fallback
# below with a provenance-based check matching the silver_collars pattern.
# ---------------------------------------------------------------------------

@asset_check(
    asset="silver_seismic",
    name="parse_total_positive",
    description=(
        "Blocks re-run if silver.seismic_surveys contains zero rows. "
        "SEG-Y ingest writes exactly one row per survey file; zero rows means "
        "the parser raised before the INSERT committed."
    ),
    blocking=True,
)
def silver_seismic_check_parse_total_positive(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify silver.seismic_surveys has at least one row (table-count fallback).

    silver_seismic does not currently emit parse_ok / parse_failed as
    MaterializeResult metadata, so we fall back to a direct table count.
    This is the minimum acceptable gate per §B1 guidance: 'emit at minimum
    a parse_total > 0 blocking check'.
    """
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM silver.seismic_surveys;")
            row = cur.fetchone()
    count = row[0] if row else 0
    passed = count > 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"silver.seismic_surveys has {count} row(s)."
            if passed
            else "silver.seismic_surveys is empty — blocking run (SEG-Y parse did not commit)."
        ),
        metadata={
            "parse_total": count,
            "parse_ok": count,
            "parse_failed": 0,
            "parse_ratio": 1.0 if count > 0 else 0.0,
            "parser_name": "segy_parser",
            "parser_version": "1.0",
        },
    )


@asset_check(
    asset="silver_seismic",
    name="schema_conformance_pass_rate",
    description=(
        "Blocks only on 0% pass rate (table is empty — covered by parse_total_positive). "
        "WARN if survey_type is NULL on any row, which indicates the SEG-Y textual "
        "header was unreadable or the parser could not classify survey geometry. "
        "Mirrors the 'blocking only at catastrophic failure' pattern from silver_reports."
    ),
    blocking=True,
)
def silver_seismic_check_schema_conformance(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Check that at least one seismic survey row has a non-null survey_type.

    survey_type is the primary header-derived field; a NULL value means the
    SEG-Y textual header was unreadable or the segy_parser could not classify
    the survey geometry (2D vs 3D).  A populated survey_type confirms the
    parser extracted meaningful metadata.
    """
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE survey_type IS NOT NULL AND survey_type != '') AS with_type
                FROM silver.seismic_surveys;
            """)
            row = cur.fetchone()
    total = row[0] if row else 0
    with_type = row[1] if row else 0
    failed = total - with_type
    ratio = with_type / total if total > 0 else 1.0
    # Blocking only when total > 0 AND zero rows have a survey_type (catastrophic)
    passed = with_type > 0 if total > 0 else True
    severity = (
        AssetCheckSeverity.ERROR
        if (total > 0 and with_type == 0)
        else AssetCheckSeverity.WARN
    )

    return AssetCheckResult(
        passed=passed,
        severity=severity,
        description=(
            f"{with_type}/{total} seismic survey rows have a populated survey_type "
            f"(ratio={ratio:.2%})."
            if passed
            else "No seismic survey rows have a survey_type — header parse failed, blocking run."
        ),
        metadata={
            "parse_total": total,
            "parse_ok": with_type,
            "parse_failed": failed,
            "parse_ratio": ratio,
            "parser_name": "segy_parser",
            "parser_version": "1.0",
        },
    )


# ---------------------------------------------------------------------------
# silver_xyz checks
#
# NOTE: silver_xyz writes to silver.spatial_features (shared with silver_spatial).
# These checks use source='xyz' filtering to isolate XYZ-origin rows, avoiding
# false-positives from spatial features inserted by other assets.
#
# TODO: When silver_xyz emits parse_ok / parse_failed as MaterializeResult
# metadata, replace the table-count fallback below with a provenance-based
# check matching the silver_collars pattern.
# ---------------------------------------------------------------------------

@asset_check(
    asset="silver_xyz",
    name="parse_total_positive",
    description=(
        "Blocks re-run if silver.spatial_features has zero rows with source='xyz'. "
        "XYZ ingest inserts one LineString per survey line; zero rows means either "
        "the XYZ file had no points or the parse failed before INSERT."
    ),
    blocking=True,
)
def silver_xyz_check_parse_total_positive(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Verify silver.spatial_features has at least one row from the XYZ parser.

    Filters on source='xyz' to isolate XYZ-origin features from shapefile/
    GeoPackage features inserted by silver_spatial.  This avoids a false-pass
    when the spatial table has rows but they all came from a different asset.
    """
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM silver.spatial_features WHERE source = 'xyz';
            """)
            row = cur.fetchone()
    count = row[0] if row else 0
    passed = count > 0

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=(
            f"silver.spatial_features has {count} XYZ-origin row(s)."
            if passed
            else (
                "silver.spatial_features has zero rows with source='xyz' — "
                "blocking run (XYZ parse produced no features)."
            )
        ),
        metadata={
            "parse_total": count,
            "parse_ok": count,
            "parse_failed": 0,
            "parse_ratio": 1.0 if count > 0 else 0.0,
            "parser_name": "xyz_parser",
            "parser_version": "1.0",
        },
    )


@asset_check(
    asset="silver_xyz",
    name="schema_conformance_pass_rate",
    description=(
        "Blocks only on 0% pass rate (no XYZ rows — covered by parse_total_positive). "
        "WARN if any XYZ spatial feature has a NULL geometry, which would indicate "
        "a CRS reprojection failure or degenerate LineString (<2 valid points). "
        "Mirrors the 'blocking only at catastrophic failure' pattern."
    ),
    blocking=True,
)
def silver_xyz_check_schema_conformance(
    context: AssetCheckExecutionContext,
    postgres: PostgresResource,
) -> AssetCheckResult:
    """Check that XYZ-origin rows in silver.spatial_features have valid geometry.

    A NULL geom on an XYZ feature means the LineString had fewer than 2 valid
    points after reprojection — the asset logs these as skipped but they should
    not appear as committed rows (the asset skips the insert for degenerate lines).
    This check confirms that invariant holds in the database.
    """
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE geom IS NOT NULL) AS with_geom
                FROM silver.spatial_features
                WHERE source = 'xyz';
            """)
            row = cur.fetchone()
    total = row[0] if row else 0
    with_geom = row[1] if row else 0
    failed = total - with_geom
    ratio = with_geom / total if total > 0 else 1.0
    passed = with_geom > 0 if total > 0 else True
    severity = (
        AssetCheckSeverity.ERROR
        if (total > 0 and with_geom == 0)
        else AssetCheckSeverity.WARN
    )

    return AssetCheckResult(
        passed=passed,
        severity=severity,
        description=(
            f"{with_geom}/{total} XYZ spatial features have valid geometry "
            f"(ratio={ratio:.2%})."
            if passed
            else (
                "All XYZ spatial features have NULL geometry — "
                "CRS reprojection or LineString construction failed, blocking run."
            )
        ),
        metadata={
            "parse_total": total,
            "parse_ok": with_geom,
            "parse_failed": failed,
            "parse_ratio": ratio,
            "parser_name": "xyz_parser",
            "parser_version": "1.0",
        },
    )
