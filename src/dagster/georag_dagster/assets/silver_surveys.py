"""Silver layer asset — parse, validate, and insert survey data into PostGIS.

Reads the raw CSV from the MinIO Bronze bucket, runs it through the CSV survey
parser, resolves hole_id → collar_id FK via silver.collars lookup, and bulk-
inserts valid records into silver.surveys. Invalid rows and FK misses are logged
and counted but never silently dropped — the final MaterializeResult carries
skip counts.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import uuid
from io import StringIO

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets.bronze_surveys import BRONZE_BUCKET, SURVEYS_PREFIX
from georag_dagster.parsers.csv_survey import parse_csv_surveys
from georag_dagster.resources import S3Resource, PostgresResource

PROVENANCE_INSERT_SQL = """
INSERT INTO bronze.provenance (
    target_schema, target_table, target_id,
    source_file, source_file_sha256, source_row, source_col_map,
    parser_name, parser_version, ingest_run_id
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING;
"""


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class SilverSurveysConfig(Config):
    """Runtime configuration for the silver_surveys asset."""

    # The filename (basename only) of the CSV that was uploaded in the Bronze asset.
    # Example: "sample_surveys.csv"
    csv_filename: str

    # Project ID to scope the collar lookup. Must exist in silver.collars.
    project_id: str

    # Sprint 5 Phase 1 plumbing — vendor column-mapping profile ID.
    # Extracted from MinIO object metadata x-georag-vendor-profile-id by the
    # minio_upload_sensor.  The parser does NOT use this yet (Phase 2).
    vendor_profile_id: int | None = None


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

COLLAR_LOOKUP_SQL = """
SELECT hole_id, collar_id
FROM silver.collars
WHERE hole_id = ANY(%(hole_ids)s)
  AND project_id = %(project_id)s
"""

INSERT_SURVEY_SQL = """
INSERT INTO silver.surveys (
    survey_id,
    collar_id,
    depth,
    azimuth,
    dip,
    survey_method
) VALUES (
    %(survey_id)s,
    %(collar_id)s,
    %(depth)s,
    %(azimuth)s,
    %(dip)s,
    %(survey_method)s
)
ON CONFLICT (survey_id) DO UPDATE SET
    collar_id     = EXCLUDED.collar_id,
    depth         = EXCLUDED.depth,
    azimuth       = EXCLUDED.azimuth,
    dip           = EXCLUDED.dip,
    survey_method = EXCLUDED.survey_method
;
"""

POSTLOAD_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver'
          AND tablename  = 'surveys'
          AND indexname  = 'surveys_collar_id_depth_idx'
    ) THEN
        CREATE INDEX surveys_collar_id_depth_idx
            ON silver.surveys (collar_id, depth);
    END IF;
END$$;

ANALYZE silver.surveys;
"""


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="silver",
    deps=["bronze_surveys"],
    pool="csv_silver_ingest",  # 2026-05-23 CSV audit gap #3 — see silver.py
    description=(
        "Download raw survey CSV from MinIO Bronze, parse and validate it, "
        "resolve hole_id to collar_id FK, then insert valid records into "
        "silver.surveys."
    ),
)
def silver_surveys(
    context: AssetExecutionContext,
    config: SilverSurveysConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    """Parse Bronze survey CSV → validate → FK resolve → bulk insert into silver.surveys."""

    context.log.info("vendor_profile_id: %s", config.vendor_profile_id)
    object_name = f"{SURVEYS_PREFIX}/{config.csv_filename}"
    context.log.info("Silver: downloading '%s/%s' from MinIO", BRONZE_BUCKET, object_name)

    # --- Download from Bronze ---
    raw_bytes = minio.download_bytes(BRONZE_BUCKET, object_name)
    csv_text = raw_bytes.decode("utf-8", errors="replace")

    # --- Parse ---
    parse_result = parse_csv_surveys(StringIO(csv_text))

    context.log.info(
        "Parse complete — total: %d, valid: %d, skipped: %d, quality: %.1f%%",
        parse_result.total_rows,
        parse_result.valid_rows,
        parse_result.skipped_rows,
        parse_result.parse_quality_pct,
    )

    if parse_result.unmapped_columns:
        context.log.warning(
            "Unmapped CSV columns (dropped): %s",
            parse_result.unmapped_columns,
        )

    for skip in parse_result.skipped_details:
        context.log.warning("Skipped row: %s", skip.get("reason", skip))

    if not parse_result.records:
        context.log.warning("No valid records from parser — silver.surveys unchanged.")
        return MaterializeResult(
            metadata={
                "total_rows":     MetadataValue.int(parse_result.total_rows),
                "valid_rows":     MetadataValue.int(0),
                "skipped_rows":   MetadataValue.int(parse_result.total_rows),
                "fk_miss_rows":   MetadataValue.int(0),
                "inserted_count": MetadataValue.int(0),
                "parse_quality_pct": MetadataValue.float(parse_result.parse_quality_pct),
                "csv_filename":   MetadataValue.text(config.csv_filename),
            }
        )

    # --- Collar FK lookup: batch query for all hole_ids in one round-trip ---
    all_hole_ids = list({r["hole_id"] for r in parse_result.records if r.get("hole_id")})
    context.log.info(
        "Looking up collar_id for %d unique hole_id(s) in project '%s'",
        len(all_hole_ids),
        config.project_id,
    )

    collar_map: dict = {}
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(COLLAR_LOOKUP_SQL, {
                "hole_ids":  all_hole_ids,
                "project_id": config.project_id,
            })
            for row in cur.fetchall():
                collar_map[row["hole_id"]] = str(row["collar_id"])

    context.log.info(
        "Collar lookup resolved %d / %d hole_id(s)",
        len(collar_map),
        len(all_hole_ids),
    )

    # --- Build insert params, log FK misses ---
    insert_params: list = []
    fk_miss_count = 0

    for rec in parse_result.records:
        hole_id = rec.get("hole_id")
        collar_id = collar_map.get(hole_id)
        if collar_id is None:
            context.log.warning(
                "FK miss: hole_id '%s' not found in silver.collars for project '%s' — row skipped",
                hole_id,
                config.project_id,
            )
            fk_miss_count += 1
            continue

        insert_params.append({
            "survey_id":     str(uuid.uuid4()),
            "collar_id":     collar_id,
            "depth":         rec.get("depth"),
            "azimuth":       rec.get("azimuth"),
            "dip":           rec.get("dip"),
            "survey_method": rec.get("survey_method"),
            "_source_row":   rec.get("_source_row"),
        })

    to_insert = len(insert_params)
    inserted_count = 0

    context.log.info(
        "Inserting %d surveys into silver.surveys (%d FK misses skipped)",
        to_insert,
        fk_miss_count,
    )

    # --- Bulk insert ---
    if insert_params:
        # Strip _source_row tracking key before inserting into the DB
        db_params = [{k: v for k, v in p.items() if k != "_source_row"} for p in insert_params]

        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur,
                    INSERT_SURVEY_SQL,
                    db_params,
                    page_size=200,
                )
                inserted_count = to_insert
            conn.commit()

        # --- Provenance INSERT (bronze.provenance) ---
        prov = parse_result.provenance
        if prov:
            ingest_run_id = str(uuid.uuid4())
            prov_params = [
                (
                    "silver", "surveys", p["survey_id"],
                    prov.get("source_file"), prov.get("source_file_sha256"),
                    p.get("_source_row"),
                    psycopg2.extras.Json(prov.get("source_col_map") or {}),
                    prov.get("parser_name"), prov.get("parser_version"),
                    ingest_run_id,
                )
                for p in insert_params
            ]
            try:
                with postgres.get_connection() as conn:
                    with conn.cursor() as cur:
                        psycopg2.extras.execute_batch(
                            cur, PROVENANCE_INSERT_SQL, prov_params, page_size=200
                        )
                    conn.commit()
                context.log.info(
                    "Provenance: inserted %d rows into bronze.provenance for silver.surveys",
                    len(prov_params),
                )
            except Exception as prov_exc:
                context.log.warning(
                    "Provenance INSERT skipped (table may not exist yet): %s", prov_exc
                )

        # --- Post-load tuning ---
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(POSTLOAD_SQL)
            conn.commit()

        context.log.info("Post-load: index ensured and ANALYZE run on silver.surveys")
    else:
        context.log.warning("No records to insert after FK resolution — silver.surveys unchanged.")

    skipped_total = (parse_result.total_rows - parse_result.valid_rows) + fk_miss_count

    return MaterializeResult(
        metadata={
            "total_rows":        MetadataValue.int(parse_result.total_rows),
            "valid_rows":        MetadataValue.int(parse_result.valid_rows),
            "fk_miss_rows":      MetadataValue.int(fk_miss_count),
            "skipped_rows":      MetadataValue.int(skipped_total),
            "inserted_count":    MetadataValue.int(inserted_count),
            "parse_quality_pct": MetadataValue.float(parse_result.parse_quality_pct),
            "unmapped_columns":  MetadataValue.text(str(parse_result.unmapped_columns)),
            "csv_filename":      MetadataValue.text(config.csv_filename),
            "vendor_profile_id":   MetadataValue.text(str(config.vendor_profile_id) if config.vendor_profile_id is not None else "none"),
            "project_id":        MetadataValue.text(config.project_id),
        }
    )
