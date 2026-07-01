"""Bronze layer asset — raw collar CSV ingestion into MinIO.

The Bronze layer stores files in their original, unmodified form inside the
`bronze` MinIO bucket. This is the immutable source of truth: if a
parser improves, reprocessing always starts here, never from Silver/Gold.

No data transformation happens in the Bronze asset. Its only job is:
  1. Accept a raw CSV file path via asset config
  2. Compute a SHA-256 checksum
  3. Upload to MinIO under `collars/<filename>` (skip if already uploaded with
     matching checksum to allow idempotent re-runs)
  4. Record materialisation metadata (size, row count, upload path, checksum)
"""

import os
from pathlib import Path
from typing import Optional

import polars as pl
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets._minio_bronze_helpers import resolve_bronze_source
from georag_dagster.resources import S3Resource

BRONZE_BUCKET = "bronze"
COLLARS_PREFIX = "collars"


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class BronzeCollarsConfig(Config):
    """Runtime configuration for the bronze_collars asset.

    Exactly one of ``object_key`` / ``csv_file_path`` must be set. The
    minio_upload_sensor populates ``object_key`` when Laravel uploads
    land in MinIO; admin/backfill flows still pass ``csv_file_path``.
    """

    object_key: Optional[str] = None
    csv_file_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_csv_rows(path: str) -> int:
    """Count data rows (excluding header) using Polars — fast even for large files."""
    try:
        df = pl.read_csv(path, infer_schema=False, truncate_ragged_lines=True)
        return len(df)
    except Exception:
        # Never block Bronze ingestion over a row-count failure
        return -1


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="bronze",
    description=(
        "Ingest a raw collar CSV file into the MinIO Bronze bucket (bronze/collars/). "
        "Files are stored immutably — this is the source of truth for all reprocessing."
    ),
)
def bronze_collars(
    context: AssetExecutionContext,
    config: BronzeCollarsConfig,
    minio: S3Resource,
) -> MaterializeResult:
    """Land a raw collar CSV in the MinIO Bronze bucket.

    Two modes:
      * sensor-driven: ``config.object_key`` points at an already-uploaded
        MinIO object (Laravel UploadController landed it). The asset
        streams the body to a temp file to compute checksum + row count;
        no re-upload.
      * admin/backfill: ``config.csv_file_path`` is a local path; the
        asset hashes + uploads to ``bronze/collars/{basename}`` (skipped
        if an object with matching size already exists).
    """
    source = resolve_bronze_source(
        minio=minio,
        bucket=BRONZE_BUCKET,
        prefix=COLLARS_PREFIX,
        object_key=config.object_key,
        local_path=config.csv_file_path,
        upload_content_type="text/csv",
    )

    context.log.info(
        "Bronze collars: source=%s key=%s size=%d sha256=%s",
        "minio" if source.sourced_from_minio else "local",
        source.object_key,
        source.file_size,
        source.sha256,
    )

    row_count = _count_csv_rows(source.local_path)
    upload_path = f"{BRONZE_BUCKET}/{source.object_key}"

    try:
        return MaterializeResult(
            metadata={
                "file_name": MetadataValue.text(Path(source.object_key).name),
                "upload_path": MetadataValue.text(upload_path),
                "file_size_bytes": MetadataValue.int(source.file_size),
                "row_count": MetadataValue.int(row_count),
                "sha256_checksum": MetadataValue.text(source.sha256),
                "sourced_from_minio": MetadataValue.bool(source.sourced_from_minio),
            }
        )
    finally:
        if source.sourced_from_minio:
            try:  # noqa: SIM105
                os.unlink(source.local_path)
            except OSError:
                pass
