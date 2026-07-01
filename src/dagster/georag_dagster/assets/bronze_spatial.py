"""Bronze layer asset — raw Shapefile / GeoJSON / GPKG ingestion into MinIO.

Accepts either a MinIO ``object_key`` (sensor-driven) or a local
``spatial_file_path`` (admin/backfill).

Shapefile note: .shp files are accompanied by .dbf, .shx, .prj sidecar
files. This asset only handles the primary file; sidecar handling is
the caller's responsibility.
"""

import os
from pathlib import Path
from typing import Optional

from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets._minio_bronze_helpers import resolve_bronze_source
from georag_dagster.resources import S3Resource

BRONZE_BUCKET = "bronze"
SPATIAL_PREFIX = "spatial"


class BronzeSpatialConfig(Config):
    object_key: Optional[str] = None
    spatial_file_path: Optional[str] = None


def _detect_content_type(filename: str) -> str:
    """Return a sensible MIME type for spatial uploads."""
    ext = Path(filename).suffix.lower()
    if ext in (".geojson", ".json"):
        return "application/geo+json"
    if ext == ".gpkg":
        return "application/geopackage+sqlite3"
    return "application/octet-stream"


@asset(
    group_name="bronze",
    description=(
        "Ingest a raw Shapefile, GeoJSON or GPKG into the MinIO Bronze bucket "
        "(bronze/spatial/). Files are stored immutably."
    ),
)
def bronze_spatial(
    context: AssetExecutionContext,
    config: BronzeSpatialConfig,
    minio: S3Resource,
) -> MaterializeResult:
    # Content type derives from the source filename — choose before resolve.
    filename_for_type = (
        config.object_key
        or config.spatial_file_path
        or ""
    )
    content_type = _detect_content_type(filename_for_type)

    source = resolve_bronze_source(
        minio=minio,
        bucket=BRONZE_BUCKET,
        prefix=SPATIAL_PREFIX,
        object_key=config.object_key,
        local_path=config.spatial_file_path,
        upload_content_type=content_type,
    )
    context.log.info(
        "Bronze spatial: source=%s key=%s ct=%s size=%d sha256=%s",
        "minio" if source.sourced_from_minio else "local",
        source.object_key, content_type, source.file_size, source.sha256,
    )
    try:
        return MaterializeResult(
            metadata={
                "file_name":         MetadataValue.text(Path(source.object_key).name),
                "upload_path":       MetadataValue.text(f"{BRONZE_BUCKET}/{source.object_key}"),
                "file_size_bytes":   MetadataValue.int(source.file_size),
                "content_type":      MetadataValue.text(content_type),
                "sha256_checksum":   MetadataValue.text(source.sha256),
                "sourced_from_minio": MetadataValue.bool(source.sourced_from_minio),
            }
        )
    finally:
        if source.sourced_from_minio:
            try:  # noqa: SIM105
                os.unlink(source.local_path)
            except OSError:
                pass
