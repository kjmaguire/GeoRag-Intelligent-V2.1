"""Silver layer asset — parse SEG-Y metadata and insert into silver.seismic_surveys.

Downloads the SEG-Y file from MinIO Bronze, runs it through the segy_parser
(which reads header metadata only — no trace data is loaded), and inserts one
row into silver.seismic_surveys.

The bbox column is left NULL for Milestone 2 — trace-coordinate extraction
required for bounding-box computation is deferred to a later milestone.

Invalid parse results (e.g. corrupt files) are logged and raise an error rather
than silently inserting garbage — a single row per file means there is nothing
to skip and continue; the whole materialisation should fail fast.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import tempfile
import uuid

from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets.bronze_seismic import BRONZE_BUCKET, SEISMIC_PREFIX
from georag_dagster.parsers.segy_parser import parse_segy_file
from georag_dagster.resources import S3Resource, PostgresResource


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class SilverSeismicConfig(Config):
    """Runtime configuration for the silver_seismic asset."""

    # Basename of the SEG-Y file uploaded in the bronze_seismic asset.
    # Example: "line1234.segy"
    segy_filename: str

    # Project UUID to associate this survey with.  Must exist in silver.projects.
    # Leave empty if the survey is not yet scoped to a project.
    project_id: str = ""

    # Human-readable survey name for reporting and downstream queries.
    # Example: "Line 1234 - 2019 Seismic Campaign"
    survey_name: str

    # Sprint 5 Phase 1 plumbing — vendor column-mapping profile ID.
    # Extracted from MinIO object metadata x-georag-vendor-profile-id by the
    # minio_upload_sensor.  The parser does NOT use this yet (Phase 2).
    vendor_profile_id: int | None = None


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

INSERT_SEISMIC_SQL = """
INSERT INTO silver.seismic_surveys (
    survey_id,
    project_id,
    survey_name,
    survey_type,
    num_traces,
    num_samples_per_trace,
    sample_interval_us,
    record_length_ms,
    inline_min,
    inline_max,
    xline_min,
    xline_max,
    source_file,
    file_size_bytes,
    segy_revision,
    header_text,
    bbox
) VALUES (
    %(survey_id)s,
    %(project_id)s,
    %(survey_name)s,
    %(survey_type)s,
    %(num_traces)s,
    %(num_samples_per_trace)s,
    %(sample_interval_us)s,
    %(record_length_ms)s,
    %(inline_min)s,
    %(inline_max)s,
    %(xline_min)s,
    %(xline_max)s,
    %(source_file)s,
    %(file_size_bytes)s,
    %(segy_revision)s,
    %(header_text)s,
    NULL
)
ON CONFLICT (survey_id) DO UPDATE SET
    project_id            = EXCLUDED.project_id,
    survey_name           = EXCLUDED.survey_name,
    survey_type           = EXCLUDED.survey_type,
    num_traces            = EXCLUDED.num_traces,
    num_samples_per_trace = EXCLUDED.num_samples_per_trace,
    sample_interval_us    = EXCLUDED.sample_interval_us,
    record_length_ms      = EXCLUDED.record_length_ms,
    inline_min            = EXCLUDED.inline_min,
    inline_max            = EXCLUDED.inline_max,
    xline_min             = EXCLUDED.xline_min,
    xline_max             = EXCLUDED.xline_max,
    source_file           = EXCLUDED.source_file,
    file_size_bytes       = EXCLUDED.file_size_bytes,
    segy_revision         = EXCLUDED.segy_revision,
    header_text           = EXCLUDED.header_text,
    updated_at            = NOW()
;
"""

POSTLOAD_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver'
          AND tablename  = 'seismic_surveys'
          AND indexname  = 'idx_seismic_surveys_type'
    ) THEN
        CREATE INDEX idx_seismic_surveys_type
            ON silver.seismic_surveys (survey_type);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver'
          AND tablename  = 'seismic_surveys'
          AND indexname  = 'idx_seismic_surveys_project'
    ) THEN
        CREATE INDEX idx_seismic_surveys_project
            ON silver.seismic_surveys (project_id);
    END IF;
END$$;

ANALYZE silver.seismic_surveys;
"""


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="silver",
    deps=["bronze_seismic"],
    description=(
        "Download SEG-Y file from MinIO Bronze, extract header metadata without "
        "loading trace data, and insert one row into silver.seismic_surveys. "
        "bbox is left NULL until trace-coordinate extraction is implemented."
    ),
)
def silver_seismic(
    context: AssetExecutionContext,
    config: SilverSeismicConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    """Parse Bronze SEG-Y metadata → insert into silver.seismic_surveys."""

    context.log.info("vendor_profile_id: %s", config.vendor_profile_id)
    object_name = f"{SEISMIC_PREFIX}/{config.segy_filename}"
    context.log.info(
        "Silver seismic: downloading '%s/%s' from MinIO", BRONZE_BUCKET, object_name
    )

    # --- Download from Bronze to a temporary file ---
    # segyio.open() requires a real filesystem path.
    file_bytes = minio.download_bytes(BRONZE_BUCKET, object_name)

    # Preserve the original extension (segy, sgy, etc.) so segyio detects it
    ext = "." + config.segy_filename.rsplit(".", 1)[-1] if "." in config.segy_filename else ".segy"
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    context.log.info(
        "Silver seismic: downloaded %d bytes (%.1f MB) to temp file '%s'",
        len(file_bytes),
        len(file_bytes) / (1024 * 1024),
        tmp_path,
    )

    # --- Parse metadata ---
    parse_result = parse_segy_file(tmp_path)

    context.log.info(
        "SEG-Y parse complete — type=%s traces=%d samples_per_trace=%d "
        "interval_us=%d record_ms=%.1f revision=%s",
        parse_result.survey_type,
        parse_result.num_traces,
        parse_result.num_samples_per_trace,
        parse_result.sample_interval_us,
        parse_result.record_length_ms,
        parse_result.segy_revision,
    )

    project_id_val = config.project_id if config.project_id else None
    survey_id = str(uuid.uuid4())

    insert_params = {
        "survey_id":            survey_id,
        "project_id":           project_id_val,
        "survey_name":          config.survey_name,
        "survey_type":          parse_result.survey_type,
        "num_traces":           parse_result.num_traces,
        "num_samples_per_trace":parse_result.num_samples_per_trace,
        "sample_interval_us":   parse_result.sample_interval_us,
        "record_length_ms":     parse_result.record_length_ms,
        "inline_min":           parse_result.inline_min,
        "inline_max":           parse_result.inline_max,
        "xline_min":            parse_result.xline_min,
        "xline_max":            parse_result.xline_max,
        "source_file":          parse_result.source_file,
        "file_size_bytes":      parse_result.file_size_bytes,
        "segy_revision":        parse_result.segy_revision,
        "header_text":          parse_result.header_text,
    }

    context.log.info(
        "Inserting survey_id='%s' survey_name='%s' into silver.seismic_surveys",
        survey_id,
        config.survey_name,
    )

    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(INSERT_SEISMIC_SQL, insert_params)
        conn.commit()

    # --- Post-load tuning ---
    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(POSTLOAD_SQL)
        conn.commit()

    context.log.info(
        "Post-load: survey_type and project_id indices ensured, ANALYZE run on "
        "silver.seismic_surveys"
    )

    file_size_mb = round(parse_result.file_size_bytes / (1024 * 1024), 2)

    return MaterializeResult(
        metadata={
            "survey_id":            MetadataValue.text(survey_id),
            "survey_name":          MetadataValue.text(config.survey_name),
            "survey_type":          MetadataValue.text(parse_result.survey_type),
            "num_traces":           MetadataValue.int(parse_result.num_traces),
            "num_samples_per_trace":MetadataValue.int(parse_result.num_samples_per_trace),
            "sample_interval_us":   MetadataValue.int(parse_result.sample_interval_us),
            "record_length_ms":     MetadataValue.float(parse_result.record_length_ms),
            "file_size_mb":         MetadataValue.float(file_size_mb),
            "segy_revision":        MetadataValue.text(parse_result.segy_revision or "unknown"),
            "segy_filename":        MetadataValue.text(config.segy_filename),
            "vendor_profile_id":   MetadataValue.text(str(config.vendor_profile_id) if config.vendor_profile_id is not None else "none"),
            "project_id":           MetadataValue.text(project_id_val or ""),
        }
    )
