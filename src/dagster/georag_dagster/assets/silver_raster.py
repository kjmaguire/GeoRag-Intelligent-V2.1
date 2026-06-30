"""Silver layer asset — parse raster files from Bronze and insert into silver.raster_layers.

Downloads the raster file from MinIO Bronze, runs it through the raster_parser
(which extracts CRS, band stats, COG flag, etc.), and upserts one row into the
silver.raster_layers PostGIS table.

silver.raster_layers schema contract (Section 04e):
  raster_id             UUID PK default gen_random_uuid()
  project_id            UUID NULL FK → silver.projects(project_id)
  layer_name            VARCHAR(255) NOT NULL
  source_file           TEXT NOT NULL
  source_file_sha256    CHAR(64) NOT NULL
  format                VARCHAR(32) NOT NULL
  driver                VARCHAR(32) NULL
  width, height         INT NOT NULL
  band_count            INT NOT NULL
  crs                   VARCHAR(100) NULL
  crs_confidence        REAL NULL
  pixel_size_x/y        DOUBLE PRECISION NULL
  bounds_native         JSONB NULL
  compression           VARCHAR(32) NULL
  is_cog                BOOLEAN NOT NULL
  has_alpha             BOOLEAN NOT NULL
  band_stats            JSONB NULL
  tags                  JSONB NULL
  warnings              JSONB NULL
  bbox                  geometry(POLYGON, 4326) NULL
  created_at, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()

ON CONFLICT on (project_id, source_file_sha256) performs an upsert — re-runs
are idempotent because the Bronze layer is the immutable source of truth.

If the Bronze raster path does not exist in MinIO the asset skips gracefully
with a structured log rather than raising, so pipeline runs are not blocked by
missing optional raster files.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import os
import tempfile
import uuid
from pathlib import Path

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.parsers.raster_parser import parse_raster_file
from georag_dagster.resources import S3Resource, PostgresResource

BRONZE_BUCKET = "bronze"
RASTERS_PREFIX = "rasters"

# Supported raster extensions
_RASTER_EXTENSIONS = frozenset({".tif", ".tiff", ".nc", ".asc", ".grd", ".jp2"})


PROVENANCE_INSERT_SQL = """
INSERT INTO bronze.provenance (
    target_schema, target_table, target_id,
    source_file, source_file_sha256, source_row, source_col_map,
    parser_name, parser_version, ingest_run_id
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING;
"""

INSERT_RASTER_SQL = """
INSERT INTO silver.raster_layers (
    project_id, layer_name, source_file, source_file_sha256,
    format, driver, width, height, band_count,
    crs, crs_confidence, pixel_size_x, pixel_size_y,
    bounds_native, compression, is_cog, has_alpha,
    band_stats, tags, warnings, bbox
) VALUES (
    %(project_id)s, %(layer_name)s, %(source_file)s, %(source_file_sha256)s,
    %(format)s, %(driver)s, %(width)s, %(height)s, %(band_count)s,
    %(crs)s, %(crs_confidence)s, %(pixel_size_x)s, %(pixel_size_y)s,
    %(bounds_native)s::jsonb, %(compression)s, %(is_cog)s, %(has_alpha)s,
    %(band_stats)s::jsonb, %(tags)s::jsonb, %(warnings)s::jsonb,
    %(bbox_expr)s
)
ON CONFLICT (project_id, source_file_sha256)
DO UPDATE SET
    layer_name = EXCLUDED.layer_name,
    warnings   = EXCLUDED.warnings,
    band_stats = EXCLUDED.band_stats,
    updated_at = NOW()
RETURNING raster_id;
"""

# Version with bbox as ST_GeomFromText — used when bounds_4326 is available
INSERT_RASTER_WITH_BBOX_SQL = """
INSERT INTO silver.raster_layers (
    project_id, layer_name, source_file, source_file_sha256,
    format, driver, width, height, band_count,
    crs, crs_confidence, pixel_size_x, pixel_size_y,
    bounds_native, compression, is_cog, has_alpha,
    band_stats, tags, warnings, bbox
) VALUES (
    %(project_id)s, %(layer_name)s, %(source_file)s, %(source_file_sha256)s,
    %(format)s, %(driver)s, %(width)s, %(height)s, %(band_count)s,
    %(crs)s, %(crs_confidence)s, %(pixel_size_x)s, %(pixel_size_y)s,
    %(bounds_native)s::jsonb, %(compression)s, %(is_cog)s, %(has_alpha)s,
    %(band_stats)s::jsonb, %(tags)s::jsonb, %(warnings)s::jsonb,
    ST_GeomFromText(%(bbox_wkt)s, 4326)
)
ON CONFLICT (project_id, source_file_sha256)
DO UPDATE SET
    layer_name = EXCLUDED.layer_name,
    warnings   = EXCLUDED.warnings,
    band_stats = EXCLUDED.band_stats,
    updated_at = NOW()
RETURNING raster_id;
"""

INSERT_RASTER_NULL_BBOX_SQL = """
INSERT INTO silver.raster_layers (
    project_id, layer_name, source_file, source_file_sha256,
    format, driver, width, height, band_count,
    crs, crs_confidence, pixel_size_x, pixel_size_y,
    bounds_native, compression, is_cog, has_alpha,
    band_stats, tags, warnings, bbox
) VALUES (
    %(project_id)s, %(layer_name)s, %(source_file)s, %(source_file_sha256)s,
    %(format)s, %(driver)s, %(width)s, %(height)s, %(band_count)s,
    %(crs)s, %(crs_confidence)s, %(pixel_size_x)s, %(pixel_size_y)s,
    %(bounds_native)s::jsonb, %(compression)s, %(is_cog)s, %(has_alpha)s,
    %(band_stats)s::jsonb, %(tags)s::jsonb, %(warnings)s::jsonb,
    NULL
)
ON CONFLICT (project_id, source_file_sha256)
DO UPDATE SET
    layer_name = EXCLUDED.layer_name,
    warnings   = EXCLUDED.warnings,
    band_stats = EXCLUDED.band_stats,
    updated_at = NOW()
RETURNING raster_id;
"""

POSTLOAD_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver'
          AND tablename  = 'raster_layers'
          AND indexname  = 'idx_raster_layers_bbox'
    ) THEN
        CREATE INDEX idx_raster_layers_bbox
            ON silver.raster_layers USING GIST (bbox);
    END IF;
END$$;

ANALYZE silver.raster_layers;
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_bbox_wkt(bounds_4326: tuple[float, float, float, float]) -> str:
    """Build a WKT polygon string from (minx, miny, maxx, maxy) in WGS84.

    Returns a closed ring polygon suitable for ST_GeomFromText(..., 4326).
    """
    minx, miny, maxx, maxy = bounds_4326
    return (
        f"POLYGON(("
        f"{minx} {miny}, "
        f"{maxx} {miny}, "
        f"{maxx} {maxy}, "
        f"{minx} {maxy}, "
        f"{minx} {miny}"
        f"))"
    )


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class SilverRasterConfig(Config):
    """Runtime configuration for the silver_raster asset."""

    # Basename of the raster file uploaded in the Bronze stage.
    # Supported: .tif, .tiff, .nc, .asc, .grd, .jp2
    # Example: "dem_utm13n.tif"
    raster_filename: str

    # Optional project_id (UUID string). Leave empty if not project-scoped.
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
    description=(
        "Download a raster file (GeoTIFF, NetCDF, ASCII Grid, JPEG2000) from "
        "MinIO Bronze, parse it with raster_parser, and upsert one row into "
        "silver.raster_layers with band stats, CRS info, and a spatial bbox."
    ),
)
def silver_raster(
    context: AssetExecutionContext,
    config: SilverRasterConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    """Parse Bronze raster file → validate → upsert into silver.raster_layers."""

    context.log.info("vendor_profile_id: %s", config.vendor_profile_id)
    filename = config.raster_filename
    ext = Path(filename).suffix.lower()

    if ext not in _RASTER_EXTENSIONS:
        context.log.warning(
            "silver_raster: unsupported extension '%s' for file '%s' — skipping.",
            ext, filename,
        )
        return MaterializeResult(
            metadata={
                "raster_filename": MetadataValue.text(filename),
                "skipped":         MetadataValue.bool(True),
                "skip_reason":     MetadataValue.text(f"unsupported extension: {ext}"),
            }
        )

    object_name = f"{RASTERS_PREFIX}/{filename}"

    # --- Graceful skip if object not yet in MinIO Bronze ---
    if not minio.object_exists(BRONZE_BUCKET, object_name):
        context.log.warning(
            "silver_raster: '%s/%s' not found in MinIO — skipping gracefully.",
            BRONZE_BUCKET, object_name,
        )
        return MaterializeResult(
            metadata={
                "raster_filename": MetadataValue.text(filename),
                "skipped":         MetadataValue.bool(True),
                "skip_reason":     MetadataValue.text("not found in MinIO Bronze"),
            }
        )

    context.log.info(
        "silver_raster: downloading '%s/%s' from MinIO", BRONZE_BUCKET, object_name
    )
    file_bytes = minio.download_bytes(BRONZE_BUCKET, object_name)

    # Write to temp file preserving extension so rasterio detects the driver
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    context.log.info(
        "silver_raster: downloaded %d bytes → temp file '%s'", len(file_bytes), tmp_path
    )

    try:
        parse_result = parse_raster_file(tmp_path)
    except Exception as parse_exc:
        context.log.error(
            "silver_raster: parse failed for '%s': %s", filename, parse_exc
        )
        return MaterializeResult(
            metadata={
                "raster_filename": MetadataValue.text(filename),
                "skipped":         MetadataValue.bool(True),
                "skip_reason":     MetadataValue.text(f"parse error: {parse_exc}"),
            }
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    prov = parse_result.provenance
    sha256_hex = prov.get("source_file_sha256", "")
    layer_name = Path(filename).stem

    context.log.info(
        "silver_raster: parse complete — driver=%s format=%s size=%dx%d bands=%d "
        "crs=%s is_cog=%s sha256=%.12s...",
        parse_result.driver,
        parse_result.format,
        parse_result.width,
        parse_result.height,
        parse_result.band_count,
        parse_result.crs,
        parse_result.is_cog,
        sha256_hex,
    )

    for w in parse_result.warnings:
        context.log.warning("raster_parser warning: %s", w)

    # --- Build INSERT params ---
    project_id_val: str | None = config.project_id if config.project_id else None

    # bounds_native — serialise CRS-native bounds list as JSONB
    bounds = parse_result.bounds
    bounds_native_json = psycopg2.extras.Json(list(bounds)) if bounds else psycopg2.extras.Json(None)

    # band_stats — list of per-band dicts
    band_stats_list = [
        {
            "band_index":  b.band_index,
            "dtype":       b.dtype,
            "nodata":      b.nodata,
            "min":         b.min,
            "max":         b.max,
            "mean":        b.mean,
            "description": b.description,
        }
        for b in parse_result.bands
    ]
    band_stats_json = psycopg2.extras.Json(band_stats_list)

    tags_json = psycopg2.extras.Json(parse_result.tags or {})
    warnings_json = psycopg2.extras.Json(parse_result.warnings or [])

    # bbox WKT — build from bounds_4326 when available
    bounds_4326 = parse_result.bounds_4326
    bbox_wkt: str | None = _build_bbox_wkt(bounds_4326) if bounds_4326 is not None else None

    base_params = {
        "project_id":         project_id_val,
        "layer_name":         layer_name,
        "source_file":        filename,
        "source_file_sha256": sha256_hex,
        "format":             parse_result.format,
        "driver":             parse_result.driver,
        "width":              parse_result.width,
        "height":             parse_result.height,
        "band_count":         parse_result.band_count,
        "crs":                parse_result.crs,
        "crs_confidence":     parse_result.crs_confidence,
        "pixel_size_x":       parse_result.pixel_size_x,
        "pixel_size_y":       parse_result.pixel_size_y,
        "bounds_native":      bounds_native_json,
        "compression":        parse_result.compression,
        "is_cog":             parse_result.is_cog,
        "has_alpha":          parse_result.has_alpha,
        "band_stats":         band_stats_json,
        "tags":               tags_json,
        "warnings":           warnings_json,
    }

    raster_id: str | None = None

    # --- DB insert wrapped in a single transaction ---
    try:
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                if bbox_wkt is not None:
                    params = dict(base_params, bbox_wkt=bbox_wkt)
                    cur.execute(INSERT_RASTER_WITH_BBOX_SQL, params)
                else:
                    cur.execute(INSERT_RASTER_NULL_BBOX_SQL, base_params)
                row = cur.fetchone()
                if row:
                    raster_id = str(row[0])
            conn.commit()

        context.log.info(
            "silver_raster: upserted raster_id=%s for '%s'", raster_id, filename
        )
    except Exception as db_exc:
        context.log.error(
            "silver_raster: DB insert failed for '%s': %s", filename, db_exc
        )
        return MaterializeResult(
            metadata={
                "raster_filename": MetadataValue.text(filename),
                "skipped":         MetadataValue.bool(True),
                "skip_reason":     MetadataValue.text(f"db insert error: {db_exc}"),
            }
        )

    # --- Provenance INSERT (bronze.provenance) ---
    if raster_id and prov:
        ingest_run_id = str(uuid.uuid4())
        prov_params = [(
            "silver", "raster_layers", raster_id,
            prov.get("source_file"), sha256_hex,
            None,  # source_row not applicable for rasters
            psycopg2.extras.Json(prov.get("source_col_map") or {}),
            prov.get("parser_name"), prov.get("parser_version"),
            ingest_run_id,
        )]
        try:
            with postgres.get_connection() as conn:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_batch(
                        cur, PROVENANCE_INSERT_SQL, prov_params, page_size=10
                    )
                conn.commit()
            context.log.info(
                "Provenance: inserted 1 row into bronze.provenance for silver.raster_layers"
            )
        except Exception as prov_exc:
            context.log.warning(
                "Provenance INSERT skipped (table may not exist yet): %s", prov_exc
            )

    # --- Post-load PostGIS tuning ---
    try:
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(POSTLOAD_SQL)
            conn.commit()
        context.log.info(
            "silver_raster: GIST index ensured and ANALYZE run on silver.raster_layers"
        )
    except Exception as postload_exc:
        context.log.warning(
            "silver_raster: post-load tuning skipped: %s", postload_exc
        )

    return MaterializeResult(
        metadata={
            "raster_filename":  MetadataValue.text(filename),
            "raster_id":        MetadataValue.text(raster_id or ""),
            "layer_name":       MetadataValue.text(layer_name),
            "driver":           MetadataValue.text(parse_result.driver),
            "format":           MetadataValue.text(parse_result.format),
            "width":            MetadataValue.int(parse_result.width),
            "height":           MetadataValue.int(parse_result.height),
            "band_count":       MetadataValue.int(parse_result.band_count),
            "crs":              MetadataValue.text(parse_result.crs or ""),
            "crs_confidence":   MetadataValue.float(parse_result.crs_confidence),
            "is_cog":           MetadataValue.bool(parse_result.is_cog),
            "has_alpha":        MetadataValue.bool(parse_result.has_alpha),
            "compression":      MetadataValue.text(parse_result.compression or ""),
            "bbox_wkt":         MetadataValue.text(bbox_wkt or ""),
            "project_id":       MetadataValue.text(project_id_val or ""),
            "warning_count":    MetadataValue.int(len(parse_result.warnings)),
            "vendor_profile_id":   MetadataValue.text(str(config.vendor_profile_id) if config.vendor_profile_id is not None else "none"),
            "skipped":          MetadataValue.bool(False),
        }
    )
