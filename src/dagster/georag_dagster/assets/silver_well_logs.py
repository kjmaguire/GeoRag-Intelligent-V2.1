"""Silver layer asset — parse LAS well logs from Bronze and insert into silver.well_log_curves.

Downloads the LAS file from MinIO Bronze, runs it through the las_parser,
looks up the matching collar in silver.collars by well_name / hole_id, then
bulk-inserts one row per curve into silver.well_log_curves.

silver.well_log_curves schema contract (Section 04e):
  curve_id          UUID PRIMARY KEY
  collar_id         UUID REFERENCES silver.collars(collar_id)
  curve_name        TEXT
  curve_unit        TEXT
  curve_description TEXT
  min_depth         FLOAT8
  max_depth         FLOAT8
  step              FLOAT8
  null_value        FLOAT8
  sample_count      INT
  depths            FLOAT8[]
  values            FLOAT8[]
  las_version       TEXT
  source_file       TEXT
  created_at        TIMESTAMPTZ DEFAULT NOW()
  updated_at        TIMESTAMPTZ DEFAULT NOW()

ON CONFLICT (collar_id, curve_name) performs an upsert so re-runs are safe.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import tempfile
import uuid

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets.bronze_well_logs import BRONZE_BUCKET, WELL_LOGS_PREFIX, bronze_well_logs
from georag_dagster.parsers.las_parser import parse_las_file
from georag_dagster.resources import S3Resource, PostgresResource


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

LOOKUP_COLLAR_SQL = """
SELECT collar_id
FROM silver.collars
WHERE hole_id = %(hole_id)s
  AND (%(project_id)s IS NULL OR project_id = %(project_id)s::uuid)
LIMIT 1;
"""

INSERT_CURVE_SQL = """
INSERT INTO silver.well_log_curves (
    curve_id,
    collar_id,
    curve_name,
    curve_unit,
    curve_description,
    min_depth,
    max_depth,
    step,
    null_value,
    sample_count,
    depths,
    values,
    las_version,
    source_file
) VALUES (
    %(curve_id)s,
    %(collar_id)s,
    %(curve_name)s,
    %(curve_unit)s,
    %(curve_description)s,
    %(min_depth)s,
    %(max_depth)s,
    %(step)s,
    %(null_value)s,
    %(sample_count)s,
    %(depths)s::float8[],
    %(values)s::float8[],
    %(las_version)s,
    %(source_file)s
)
ON CONFLICT (collar_id, curve_name) DO UPDATE SET
    curve_unit        = EXCLUDED.curve_unit,
    curve_description = EXCLUDED.curve_description,
    min_depth         = EXCLUDED.min_depth,
    max_depth         = EXCLUDED.max_depth,
    step              = EXCLUDED.step,
    null_value        = EXCLUDED.null_value,
    sample_count      = EXCLUDED.sample_count,
    depths            = EXCLUDED.depths,
    values            = EXCLUDED.values,
    las_version       = EXCLUDED.las_version,
    source_file       = EXCLUDED.source_file,
    updated_at        = NOW()
;
"""

# Post-load tuning: ensure GIST-compatible index and refresh statistics.
# well_log_curves has no geometry column — a btree index on collar_id is the
# primary access pattern; ANALYZE is the important step here.
POSTLOAD_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver'
          AND tablename  = 'well_log_curves'
          AND indexname  = 'well_log_curves_collar_idx'
    ) THEN
        CREATE INDEX well_log_curves_collar_idx
            ON silver.well_log_curves (collar_id, curve_name);
    END IF;
END$$;

ANALYZE silver.well_log_curves;
"""


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class SilverWellLogsConfig(Config):
    """Runtime configuration for the silver_well_logs asset."""

    # Basename of the LAS file uploaded in the bronze_well_logs asset.
    # Example: "DH-001.las"
    las_filename: str

    # project_id (UUID string) used to narrow the collar lookup.
    # If empty string the lookup matches any project.
    project_id: str = ""

    # Sprint 5 Phase 1 plumbing — vendor column-mapping profile ID.
    # Extracted from MinIO object metadata x-georag-vendor-profile-id by the
    # minio_upload_sensor.  The parser does NOT use this yet (Phase 2).
    vendor_profile_id: int | None = None


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="silver",
    deps=[bronze_well_logs],
    description=(
        "Download LAS well log from MinIO Bronze, parse curves with las_parser, "
        "look up the matching collar in silver.collars, and insert curves into "
        "silver.well_log_curves."
    ),
)
def silver_well_logs(
    context: AssetExecutionContext,
    config: SilverWellLogsConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    """Parse Bronze LAS → look up collar → bulk insert curves into silver.well_log_curves."""

    context.log.info("vendor_profile_id: %s", config.vendor_profile_id)
    object_name = f"{WELL_LOGS_PREFIX}/{config.las_filename}"
    context.log.info(
        "Silver well logs: downloading '%s/%s' from MinIO", BRONZE_BUCKET, object_name
    )

    # --- Download LAS from Bronze to a temporary file ---
    las_bytes = minio.download_bytes(BRONZE_BUCKET, object_name)

    with tempfile.NamedTemporaryFile(suffix=".las", delete=False) as tmp:
        tmp.write(las_bytes)
        tmp_path = tmp.name

    context.log.info(
        "Silver well logs: downloaded %d bytes to temp file '%s'",
        len(las_bytes),
        tmp_path,
    )

    # --- Parse ---
    parse_result = parse_las_file(tmp_path)

    context.log.info(
        "Silver well logs: parse complete — well='%s', curves=%d/%d, "
        "depth_curve='%s', quality=%.1f%%",
        parse_result.well_name or "<unknown>",
        len(parse_result.curves),
        parse_result.total_curves_in_file,
        parse_result.depth_curve_name,
        parse_result.parse_quality_pct * 100,
    )

    if parse_result.skipped_details:
        for detail in parse_result.skipped_details:
            context.log.warning("Skipped curve: %s", detail)

    # --- Collar lookup ---
    # The well_name from the LAS ~W section is used as the hole_id to join
    # against silver.collars.  If the well was ingested under a different ID
    # the operator must ensure names are harmonised before running this asset.
    hole_id = parse_result.well_name
    if not hole_id:
        context.log.error(
            "Silver well logs: LAS file '%s' has no WELL mnemonic in the ~W section — "
            "cannot look up collar.  Aborting with 0 curves inserted.",
            config.las_filename,
        )
        return MaterializeResult(
            metadata={
                "las_filename":    MetadataValue.text(config.las_filename),
                "well_name":       MetadataValue.text(""),
                "collar_id":       MetadataValue.text(""),
                "curve_count":     MetadataValue.int(0),
                "total_samples":   MetadataValue.int(0),
                "parse_quality":   MetadataValue.float(parse_result.parse_quality_pct),
                "error":           MetadataValue.text("no WELL mnemonic in LAS ~W section"),
            }
        )

    project_id_val = config.project_id if config.project_id else None

    context.log.info(
        "Silver well logs: looking up collar for hole_id='%s' project_id=%s",
        hole_id,
        project_id_val or "ANY",
    )

    collar_id: str | None = None
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                LOOKUP_COLLAR_SQL,
                {"hole_id": hole_id, "project_id": project_id_val},
            )
            row = cur.fetchone()
            if row:
                collar_id = str(row["collar_id"])

    if collar_id is None:
        context.log.error(
            "Silver well logs: no collar found for hole_id='%s' project_id=%s — "
            "0 curves inserted.  Ensure the collar CSV was ingested first.",
            hole_id,
            project_id_val or "ANY",
        )
        return MaterializeResult(
            metadata={
                "las_filename":  MetadataValue.text(config.las_filename),
                "well_name":     MetadataValue.text(hole_id),
                "collar_id":     MetadataValue.text(""),
                "curve_count":   MetadataValue.int(0),
                "total_samples": MetadataValue.int(0),
                "parse_quality": MetadataValue.float(parse_result.parse_quality_pct),
                "error":         MetadataValue.text(f"no collar found for hole_id='{hole_id}'"),
            }
        )

    context.log.info("Silver well logs: found collar_id='%s'", collar_id)

    # --- Build insert params ---
    # psycopg2 casts Python list[float] to PostgreSQL float8[] when the SQL
    # uses the ::float8[] cast suffix.
    insert_params: list[dict] = []
    total_samples = 0

    for curve in parse_result.curves:
        insert_params.append(
            {
                "curve_id":          str(uuid.uuid4()),
                "collar_id":         collar_id,
                "curve_name":        curve.name,
                "curve_unit":        curve.unit,
                "curve_description": curve.description,
                "min_depth":         curve.min_depth,
                "max_depth":         curve.max_depth,
                "step":              curve.step,
                "null_value":        curve.null_value,
                "sample_count":      curve.sample_count,
                "depths":            curve.depths,
                "values":            curve.values,
                "las_version":       parse_result.las_version,
                "source_file":       parse_result.source_file,
            }
        )
        total_samples += curve.sample_count

    curve_count = len(insert_params)
    context.log.info(
        "Silver well logs: inserting %d curves (%d total samples) for collar_id='%s'",
        curve_count,
        total_samples,
        collar_id,
    )

    if insert_params:
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur,
                    INSERT_CURVE_SQL,
                    insert_params,
                    page_size=50,  # curves can be large; smaller page_size avoids huge params
                )
            conn.commit()

        # --- Post-load tuning ---
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(POSTLOAD_SQL)
            conn.commit()

        context.log.info(
            "Silver well logs: index ensured and ANALYZE run on silver.well_log_curves"
        )
    else:
        context.log.warning(
            "Silver well logs: parse returned 0 valid curves — silver.well_log_curves unchanged."
        )

    return MaterializeResult(
        metadata={
            "las_filename":    MetadataValue.text(config.las_filename),
            "well_name":       MetadataValue.text(parse_result.well_name or ""),
            "collar_id":       MetadataValue.text(collar_id),
            "las_version":     MetadataValue.text(parse_result.las_version),
            "depth_curve":     MetadataValue.text(parse_result.depth_curve_name),
            "curve_count":     MetadataValue.int(curve_count),
            "total_samples":   MetadataValue.int(total_samples),
            "skipped_curves":  MetadataValue.int(parse_result.skipped_curves),
            "parse_quality":   MetadataValue.float(parse_result.parse_quality_pct),
            "vendor_profile_id":   MetadataValue.text(str(config.vendor_profile_id) if config.vendor_profile_id is not None else "none"),
            "project_id":      MetadataValue.text(project_id_val or ""),
        }
    )
