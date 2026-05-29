"""Silver layer asset — parse, validate, and insert collar data into PostGIS.

Reads the raw CSV from the MinIO Bronze bucket, runs it through the CSV collar
parser, validates each row against the silver.collars schema, and bulk-inserts
valid records into PostgreSQL. Invalid rows are logged and counted but never
silently dropped — the final MaterializeResult carries skip counts.

CRS handling follows the Section 04b 4-step pipeline:
  Step 1: Inspect file for CRS metadata (header comments, embedded EPSG)
  Step 2: Heuristic from coordinate ranges (handled by the asset — UTM Zone 13N
          easting range 490 000 – 510 000 is consistent with Athabasca Basin)
  Step 3: Validate against project bounding box (rough check here)
  Step 4: Store geometry as EPSG:32613 (WGS 84 / UTM zone 13N)
          and record source_crs alongside for audit.
"""

import uuid
from io import StringIO

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets.bronze import BRONZE_BUCKET, COLLARS_PREFIX
from georag_dagster.parsers.csv_collar import parse_csv_collars
from georag_dagster.resources import S3Resource, PostgresResource

PROVENANCE_INSERT_SQL = """
INSERT INTO bronze.provenance (
    target_schema, target_table, target_id,
    source_file, source_file_sha256, source_row, source_col_map,
    parser_name, parser_version, ingest_run_id
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING;
"""

# Default project CRS — WGS 84 / UTM zone 13N
PROJECT_EPSG = 32613

# Approximate project bounding box for Athabasca Basin region (UTM 13N)
# Used for CRS step-3 bbox validation.  Expand if the project area grows.
PROJECT_BBOX = {
    "easting_min":  400_000.0,
    "easting_max":  650_000.0,
    "northing_min": 6_100_000.0,
    "northing_max": 6_400_000.0,
}


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class SilverCollarsConfig(Config):
    """Runtime configuration for the silver_collars asset."""

    # The filename (basename only) of the CSV that was uploaded in the Bronze asset.
    # Example: "sample_collars.csv"
    csv_filename: str

    # Optional: override the assumed source CRS EPSG code.
    # Leave as 0 to trigger heuristic detection.
    source_epsg: int = 0

    # Optional project_id to associate collars with (must exist in silver.projects).
    # If empty string, the project_id column is set to NULL.
    project_id: str = ""

    # Sprint 5 Phase 1 plumbing — vendor column-mapping profile ID.
    # Extracted from MinIO object metadata x-georag-vendor-profile-id by the
    # minio_upload_sensor.  The parser does NOT use this yet (Phase 2).
    vendor_profile_id: int | None = None


# ---------------------------------------------------------------------------
# CRS helpers  (Section 04b steps 2–4, step 1 is file-header parsing)
# ---------------------------------------------------------------------------

def _detect_source_epsg(records: list[dict], override: int) -> int:
    """Return the best-guess EPSG for the source coordinates.

    Step 1 (file header) is not applicable for plain CSV — no embedded CRS.
    Step 2: heuristic from coordinate ranges.
    """
    if override and override > 0:
        return override

    if not records:
        return PROJECT_EPSG

    # Sample the first record's easting/northing for the heuristic
    sample = records[0]
    easting = sample.get("easting") or 0.0
    northing = sample.get("northing") or 0.0

    # 6-digit easting (100 000 – 999 999) + 7-digit northing → UTM assumption
    if 100_000 <= easting <= 999_999 and 1_000_000 <= northing <= 10_000_000:
        # Athabasca Basin sits in UTM zone 13N; return the project default
        return PROJECT_EPSG

    return PROJECT_EPSG  # safe fallback


def _bbox_valid(easting: float, northing: float) -> bool:
    """Step 3: confirm coordinate falls inside the project bounding box."""
    return (
        PROJECT_BBOX["easting_min"] <= easting <= PROJECT_BBOX["easting_max"]
        and PROJECT_BBOX["northing_min"] <= northing <= PROJECT_BBOX["northing_max"]
    )


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

INSERT_COLLAR_SQL = """
INSERT INTO silver.collars (
    collar_id,
    project_id,
    hole_id,
    hole_id_canonical,
    easting,
    northing,
    elevation,
    total_depth,
    azimuth,
    dip,
    hole_type,
    drill_date,
    status,
    geom
) VALUES (
    %(collar_id)s,
    %(project_id)s,
    %(hole_id)s,
    %(hole_id_canonical)s,
    %(easting)s,
    %(northing)s,
    %(elevation)s,
    %(total_depth)s,
    %(azimuth)s,
    %(dip)s,
    %(hole_type)s,
    %(drill_date)s,
    %(status)s,
    ST_SetSRID(ST_MakePoint(%(easting)s, %(northing)s), %(geom_srid)s)
)
ON CONFLICT (project_id, hole_id) DO UPDATE SET
    hole_id_canonical = EXCLUDED.hole_id_canonical,
    easting     = EXCLUDED.easting,
    northing    = EXCLUDED.northing,
    elevation   = EXCLUDED.elevation,
    total_depth = EXCLUDED.total_depth,
    azimuth     = EXCLUDED.azimuth,
    dip         = EXCLUDED.dip,
    hole_type   = EXCLUDED.hole_type,
    drill_date  = EXCLUDED.drill_date,
    status      = EXCLUDED.status,
    geom        = EXCLUDED.geom,
    updated_at  = NOW()
;
"""

# Post-bulk-load PostGIS tuning (Section 06a pattern)
POSTLOAD_SQL = """
DO $$
BEGIN
    -- DB review #5 — converge on the Laravel-migration index name
    -- (idx_collars_geom) so Dagster doesn't race-create a duplicate GIST.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver'
          AND tablename  = 'collars'
          AND indexname  = 'idx_collars_geom'
    ) THEN
        CREATE INDEX idx_collars_geom ON silver.collars USING GIST (geom);
    END IF;
END$$;

ANALYZE silver.collars;
"""


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="silver",
    deps=["bronze_collars"],
    # 2026-05-23 CSV audit gap #3 — Dagster concurrency pool. Caps
    # concurrent silver-CSV materializations so multiple uploads to the
    # same workspace don't pile up bulk INSERTs on silver.* tables (DB
    # lock contention + pgbouncer pool exhaustion). Pool limit configured
    # in dagster.yaml. Mirrors the per-workspace concurrency cap we
    # shipped today for ingest_pdf, but using Dagster's native op-pool
    # mechanism since CSV ingest stays on Dagster per CLAUDE.md hard
    # rule 7 (Dagster = scheduled/bulk data pipelines).
    pool="csv_silver_ingest",
    description=(
        "Download raw collar CSV from MinIO Bronze, parse and validate it, "
        "then insert valid records into silver.collars with PostGIS geometry."
    ),
)
def silver_collars(
    context: AssetExecutionContext,
    config: SilverCollarsConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    """Parse Bronze CSV → validate → bulk insert into silver.collars."""

    context.log.info("vendor_profile_id: %s", config.vendor_profile_id)
    object_name = f"{COLLARS_PREFIX}/{config.csv_filename}"
    context.log.info("Silver: downloading '%s/%s' from MinIO", BRONZE_BUCKET, object_name)

    # --- Download from Bronze ---
    raw_bytes = minio.download_bytes(BRONZE_BUCKET, object_name)
    csv_text = raw_bytes.decode("utf-8", errors="replace")

    # --- Parse ---
    parse_result = parse_csv_collars(StringIO(csv_text))

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

    # --- CRS detection (Section 04b steps 2–4) ---
    source_epsg = _detect_source_epsg(parse_result.records, config.source_epsg)
    context.log.info("Detected source CRS: EPSG:%d", source_epsg)

    # --- Prepare insert params, applying bbox validation (step 3) ---
    project_id_val = config.project_id if config.project_id else None
    insert_params: list[dict] = []
    bbox_rejected: list[str] = []

    for rec in parse_result.records:
        east = rec["easting"]
        north = rec["northing"]

        if not _bbox_valid(east, north):
            reason = (
                f"collar '{rec.get('hole_id')}' coordinate ({east}, {north}) "
                f"outside project bounding box — skipped (CRS step 3)"
            )
            context.log.warning(reason)
            bbox_rejected.append(rec.get("hole_id", "unknown"))
            continue

        insert_params.append(
            {
                "collar_id":   str(uuid.uuid4()),
                "project_id":  project_id_val,
                "hole_id":     rec["hole_id"],
                # parser already produced the canonical form per
                # parsers/csv_collar.py (_validate_row) — pass it through
                # so the chat retrieval path can join on canonical without
                # waiting on a backfill sweep.
                "hole_id_canonical": rec.get("hole_id_canonical"),
                "easting":     east,
                "northing":    north,
                "elevation":   rec.get("elevation"),
                "total_depth": rec.get("total_depth"),
                "azimuth":     rec.get("azimuth"),
                "dip":         rec.get("dip"),
                "hole_type":   rec.get("hole_type"),
                "drill_date":  rec.get("drill_date"),
                "status":      rec.get("status"),
                "geom_srid":   PROJECT_EPSG,
                "_source_row": rec.get("_source_row"),
            }
        )

    total_rows = parse_result.total_rows
    valid_after_parse = parse_result.valid_rows
    bbox_skipped = len(bbox_rejected)
    to_insert = len(insert_params)
    inserted_count = 0

    context.log.info(
        "Inserting %d collars into silver.collars (%d bbox-rejected)",
        to_insert,
        bbox_skipped,
    )

    # --- Bulk insert ---
    if insert_params:
        # Strip the _source_row tracking key before inserting into the DB
        db_params = [{k: v for k, v in p.items() if k != "_source_row"} for p in insert_params]

        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur,
                    INSERT_COLLAR_SQL,
                    db_params,
                    page_size=200,
                )
                inserted_count = len(insert_params)
            conn.commit()

        # --- Provenance INSERT (bronze.provenance) ---
        prov = parse_result.provenance
        if prov:
            ingest_run_id = str(uuid.uuid4())
            prov_params = [
                (
                    "silver", "collars", p["collar_id"],
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
                    "Provenance: inserted %d rows into bronze.provenance for silver.collars",
                    len(prov_params),
                )
            except Exception as prov_exc:
                context.log.warning(
                    "Provenance INSERT skipped (table may not exist yet): %s", prov_exc
                )

        # --- Post-load PostGIS tuning (Section 06a) ---
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(POSTLOAD_SQL)
            conn.commit()

        context.log.info("Post-load: GIST index ensured and ANALYZE run on silver.collars")
    else:
        context.log.warning("No valid records to insert — silver.collars unchanged.")

    skipped_total = (total_rows - valid_after_parse) + bbox_skipped

    return MaterializeResult(
        metadata={
            "total_rows":         MetadataValue.int(total_rows),
            "valid_rows":         MetadataValue.int(valid_after_parse),
            "bbox_rejected_rows": MetadataValue.int(bbox_skipped),
            "skipped_rows":       MetadataValue.int(skipped_total),
            "inserted_count":     MetadataValue.int(inserted_count),
            "parse_quality_pct":  MetadataValue.float(parse_result.parse_quality_pct),
            "source_crs":         MetadataValue.text(f"EPSG:{source_epsg}"),
            "target_crs":         MetadataValue.text(f"EPSG:{PROJECT_EPSG}"),
            "unmapped_columns":   MetadataValue.text(str(parse_result.unmapped_columns)),
            "csv_filename":       MetadataValue.text(config.csv_filename),
            "vendor_profile_id":   MetadataValue.text(str(config.vendor_profile_id) if config.vendor_profile_id is not None else "none"),
        }
    )
