"""Silver layer asset — parse Geosoft XYZ export and insert into silver.spatial_features.

Downloads the XYZ file from MinIO Bronze, runs the xyz_parser to extract
point data and channel statistics, then:

  1. Groups points by line number (if a LINE column is present) — each unique
     line number becomes one spatial feature (LineString geometry).
  2. If no LINE column is present, all points are treated as a single feature.
  3. Reprojects coordinates from source_crs (default EPSG:32613) to EPSG:4326
     using pyproj, following the Section 04b CRS pipeline.
  4. Builds a WKT LineString per line (capped at 1000 points — evenly sampled
     if the line has more points — to keep WKT manageable in PostgreSQL).
  5. Stores channel statistics (min / max / mean per channel) as a JSONB dict
     in the `properties` column.
  6. Inserts into silver.spatial_features with feature_type='geophysics'.

Re-runs are idempotent: existing rows for (source_file, project_id) are deleted
before re-insertion, matching the silver_spatial asset pattern.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import json
import uuid

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets.bronze_xyz import BRONZE_BUCKET, XYZ_PREFIX
from georag_dagster.parsers.xyz_parser import XyzChannel, XyzParseResult, parse_xyz_file
from georag_dagster.resources import S3Resource, PostgresResource

import tempfile

# Maximum points per line in the WKT LineString.  Longer lines are evenly
# sub-sampled.  This prevents extreme WKT sizes in PostGIS.
MAX_POINTS_PER_LINE = 1000


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class SilverXyzConfig(Config):
    """Runtime configuration for the silver_xyz asset."""

    # Basename of the XYZ file uploaded in the bronze_xyz asset.
    xyz_filename: str

    # Project UUID — associated with all features inserted.
    # Leave empty if not scoped to a project.
    project_id: str = ""

    # EPSG code of the source CRS for X/Y coordinates.
    # Default is EPSG:32613 (WGS 84 / UTM zone 13N — Athabasca Basin).
    source_crs: str = "EPSG:32613"

    # Prefix for feature names.  Each line feature is named
    # "<feature_name_prefix>_<line_number>" or "<feature_name_prefix>_all".
    feature_name_prefix: str = "geophysics_line"

    # Sprint 5 Phase 1 plumbing — vendor column-mapping profile ID.
    # Extracted from MinIO object metadata x-georag-vendor-profile-id by the
    # minio_upload_sensor.  The parser does NOT use this yet (Phase 2).
    vendor_profile_id: int | None = None


# ---------------------------------------------------------------------------
# SQL — matches silver_spatial.py pattern
# ---------------------------------------------------------------------------

DELETE_EXISTING_SQL = """
DELETE FROM silver.spatial_features
WHERE source_file = %(source_file)s
  AND (
      %(project_id)s IS NULL AND project_id IS NULL
      OR project_id = %(project_id)s::uuid
  );
"""

INSERT_FEATURE_SQL = """
INSERT INTO silver.spatial_features (
    feature_id,
    project_id,
    feature_type,
    feature_name,
    source,
    source_file,
    source_crs,
    properties,
    geom
) VALUES (
    %(feature_id)s,
    %(project_id)s,
    %(feature_type)s,
    %(feature_name)s,
    %(source)s,
    %(source_file)s,
    %(source_crs)s,
    %(properties)s::jsonb,
    ST_GeomFromText(%(geometry_wkt)s, 4326)
);
"""

POSTLOAD_SQL = """
DO $$
BEGIN
    -- DB review #5 — converge on the Laravel-migration index name
    -- (idx_spatial_features_geom) so Dagster doesn't race-create a
    -- duplicate GIST on silver.spatial_features.geom.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'silver'
          AND tablename  = 'spatial_features'
          AND indexname  = 'idx_spatial_features_geom'
    ) THEN
        CREATE INDEX idx_spatial_features_geom
            ON silver.spatial_features USING GIST (geom);
    END IF;
END$$;

CLUSTER silver.spatial_features USING idx_spatial_features_geom;
ANALYZE silver.spatial_features;
"""


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _reproject_points(
    x_values: list,
    y_values: list,
    source_crs: str,
) -> list:
    """Reproject (easting, northing) pairs from source_crs to EPSG:4326.

    Returns a list of (lon, lat) tuples.  Points where either coordinate is
    None are returned as (None, None) and should be filtered out by the caller.
    """
    from pyproj import Transformer  # noqa: PLC0415

    transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
    wgs84: list = []
    for x, y in zip(x_values, y_values):
        if x is None or y is None:
            wgs84.append((None, None))
        else:
            lon, lat = transformer.transform(x, y)
            wgs84.append((lon, lat))
    return wgs84


def _subsample(items: list, max_count: int) -> list:
    """Return a list of at most max_count items, evenly sampled."""
    if len(items) <= max_count:
        return items
    step = len(items) / max_count
    return [items[int(i * step)] for i in range(max_count)]


def _build_linestring_wkt(points: list) -> str:
    """Build a WKT LINESTRING from (lon, lat) pairs.  Filters None pairs."""
    valid = [(lon, lat) for lon, lat in points if lon is not None and lat is not None]
    if len(valid) < 2:
        return None  # PostGIS requires at least 2 points for a LineString
    coords = ", ".join(f"{lon} {lat}" for lon, lat in valid)
    return f"LINESTRING({coords})"


def _channel_stats(channel: XyzChannel, indices: list) -> dict:
    """Return min/max/mean statistics for a channel subset selected by indices."""
    values = [channel.values[i] for i in indices if i < len(channel.values)]
    clean = [v for v in values if v is not None]
    if not clean:
        return {"min": None, "max": None, "mean": None, "unit": channel.unit}
    return {
        "min": min(clean),
        "max": max(clean),
        "mean": round(sum(clean) / len(clean), 6),
        "unit": channel.unit,
    }


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="silver",
    deps=["bronze_xyz"],
    description=(
        "Download Geosoft XYZ export from MinIO Bronze, parse geophysics line "
        "data, reproject coordinates from source_crs to EPSG:4326, and insert "
        "one LineString feature per survey line into silver.spatial_features."
    ),
)
def silver_xyz(
    context: AssetExecutionContext,
    config: SilverXyzConfig,
    postgres: PostgresResource,
    minio: S3Resource,
) -> MaterializeResult:
    """Parse Bronze XYZ file → reproject → insert into silver.spatial_features."""

    context.log.info("vendor_profile_id: %s", config.vendor_profile_id)
    object_name = f"{XYZ_PREFIX}/{config.xyz_filename}"
    context.log.info(
        "Silver XYZ: downloading '%s/%s' from MinIO", BRONZE_BUCKET, object_name
    )

    # --- Download from Bronze to a temporary file ---
    file_bytes = minio.download_bytes(BRONZE_BUCKET, object_name)

    with tempfile.NamedTemporaryFile(suffix=".xyz", delete=False, mode="wb") as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    context.log.info(
        "Silver XYZ: downloaded %d bytes to temp file '%s'", len(file_bytes), tmp_path
    )

    # --- Parse ---
    parse_result = parse_xyz_file(tmp_path)

    context.log.info(
        "XYZ parse complete — points=%d channels=%d easting='%s' northing='%s' line=%r",
        parse_result.point_count,
        parse_result.channel_count,
        parse_result.easting_column,
        parse_result.northing_column,
        parse_result.line_column,
    )

    for err in parse_result.parse_errors:
        context.log.warning("XYZ parse warning: %s", err)

    if parse_result.point_count == 0:
        context.log.warning("Silver XYZ: no points parsed — silver.spatial_features unchanged.")
        return MaterializeResult(
            metadata={
                "xyz_filename":   MetadataValue.text(config.xyz_filename),
                "point_count":    MetadataValue.int(0),
                "feature_count":  MetadataValue.int(0),
                "channel_count":  MetadataValue.int(0),
                "source_crs":     MetadataValue.text(config.source_crs),
                "project_id":     MetadataValue.text(config.project_id or ""),
            }
        )

    # --- CRS step 4: Reproject to EPSG:4326 ---
    context.log.info(
        "Silver XYZ: reprojecting %d points from %s to EPSG:4326",
        parse_result.point_count,
        config.source_crs,
    )
    try:
        wgs84_points = _reproject_points(
            parse_result.x_values,
            parse_result.y_values,
            config.source_crs,
        )
    except Exception as exc:
        raise RuntimeError(
            f"silver_xyz: CRS reprojection failed from {config.source_crs} to EPSG:4326: {exc}"
        ) from exc

    # --- Group points by line number ---
    # Build a mapping: line_number (or sentinel "all") → list of point indices
    line_groups: dict = {}

    if parse_result.line_column and parse_result.line_values:
        for idx, line_val in enumerate(parse_result.line_values):
            key = line_val if line_val is not None else "unknown"
            line_groups.setdefault(key, []).append(idx)
        context.log.info(
            "Silver XYZ: grouped %d points into %d survey lines",
            parse_result.point_count,
            len(line_groups),
        )
    else:
        line_groups["all"] = list(range(parse_result.point_count))
        context.log.info(
            "Silver XYZ: no LINE column — treating all %d points as one feature",
            parse_result.point_count,
        )

    # --- Build insert params ---
    project_id_val = config.project_id if config.project_id else None
    insert_params: list = []
    empty_line_skipped = 0

    for line_key, indices in line_groups.items():
        # Sub-sample if line exceeds MAX_POINTS_PER_LINE
        if len(indices) > MAX_POINTS_PER_LINE:
            sampled_indices = _subsample(indices, MAX_POINTS_PER_LINE)
            context.log.info(
                "Silver XYZ: line %s has %d points — sub-sampled to %d",
                line_key,
                len(indices),
                len(sampled_indices),
            )
        else:
            sampled_indices = indices

        # Build WKT LineString
        line_points = [wgs84_points[i] for i in sampled_indices]
        geometry_wkt = _build_linestring_wkt(line_points)

        if geometry_wkt is None:
            context.log.warning(
                "Silver XYZ: line %s has fewer than 2 valid points after filtering — skipped",
                line_key,
            )
            empty_line_skipped += 1
            continue

        # Build channel statistics properties for this line's points
        channel_props: dict = {}
        for ch in parse_result.channels:
            channel_props[ch.name] = _channel_stats(ch, indices)

        properties = {
            "line": str(line_key),
            "point_count_original": len(indices),
            "point_count_stored": len(sampled_indices),
            "source_crs": config.source_crs,
            "channels": channel_props,
        }

        feature_name = f"{config.feature_name_prefix}_{line_key}"

        insert_params.append({
            "feature_id":   str(uuid.uuid4()),
            "project_id":   project_id_val,
            "feature_type": "geophysics",
            "feature_name": feature_name,
            "source":       "xyz",
            "source_file":  parse_result.source_file,
            "source_crs":   config.source_crs,
            "properties":   psycopg2.extras.Json(properties),
            "geometry_wkt": geometry_wkt,
        })

    context.log.info(
        "Silver XYZ: prepared %d feature(s) for insertion (%d empty lines skipped)",
        len(insert_params),
        empty_line_skipped,
    )

    inserted_count = 0

    if insert_params:
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                # Delete prior rows for this file to keep re-runs idempotent
                cur.execute(
                    DELETE_EXISTING_SQL,
                    {
                        "source_file": parse_result.source_file,
                        "project_id":  project_id_val,
                    },
                )
                deleted_count = cur.rowcount
                if deleted_count > 0:
                    context.log.info(
                        "Silver XYZ: removed %d existing rows for source_file='%s'",
                        deleted_count,
                        parse_result.source_file,
                    )

                psycopg2.extras.execute_batch(
                    cur,
                    INSERT_FEATURE_SQL,
                    insert_params,
                    page_size=100,
                )
                inserted_count = len(insert_params)
            conn.commit()

        context.log.info(
            "Silver XYZ: inserted %d features into silver.spatial_features", inserted_count
        )

        # --- Post-load PostGIS tuning ---
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(POSTLOAD_SQL)
            conn.commit()

        context.log.info(
            "Silver XYZ: GIST index ensured, CLUSTER run, ANALYZE complete on "
            "silver.spatial_features"
        )
    else:
        context.log.warning(
            "Silver XYZ: no features to insert — silver.spatial_features unchanged."
        )

    return MaterializeResult(
        metadata={
            "xyz_filename":        MetadataValue.text(config.xyz_filename),
            "point_count":         MetadataValue.int(parse_result.point_count),
            "channel_count":       MetadataValue.int(parse_result.channel_count),
            "channel_names":       MetadataValue.text(
                str([ch.name for ch in parse_result.channels])
            ),
            "line_count":          MetadataValue.int(len(line_groups)),
            "empty_lines_skipped": MetadataValue.int(empty_line_skipped),
            "feature_count":       MetadataValue.int(inserted_count),
            "source_crs":          MetadataValue.text(config.source_crs),
            "target_crs":          MetadataValue.text("EPSG:4326"),
            "project_id":          MetadataValue.text(project_id_val or ""),
            "feature_name_prefix": MetadataValue.text(config.feature_name_prefix),
            "vendor_profile_id":   MetadataValue.text(str(config.vendor_profile_id) if config.vendor_profile_id is not None else "none"),
            "parse_errors":        MetadataValue.text(str(parse_result.parse_errors)),
        }
    )
