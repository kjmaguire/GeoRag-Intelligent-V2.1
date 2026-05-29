"""Bronze layer asset — raw lithology log CSV ingestion into MinIO.

The Bronze layer stores files in their original, unmodified form inside the
`bronze` MinIO bucket. This is the immutable source of truth: if a
parser improves, reprocessing always starts here, never from Silver/Gold.

No data transformation happens in the Bronze asset. Its only job is to
land the raw file in MinIO and record metadata (size, row count, SHA-256).
The actual source can be either a sensor-detected MinIO object (via
``object_key``, the Laravel UploadController flow) or a local file path
(via ``csv_file_path``, the admin/backfill flow).
"""

import os
from pathlib import Path
from typing import Optional

import polars as pl
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets._minio_bronze_helpers import resolve_bronze_source
from georag_dagster.resources import S3Resource

BRONZE_BUCKET = "bronze"
LITHOLOGY_PREFIX = "lithology"


class BronzeLithologyConfig(Config):
    """Runtime configuration for the bronze_lithology asset.

    Exactly one of ``object_key`` / ``csv_file_path`` must be set.
    """

    object_key: Optional[str] = None
    csv_file_path: Optional[str] = None


def _count_csv_rows(path: str) -> int:
    """Count data rows (excluding header) using Polars — fast even for large files."""
    try:
        df = pl.read_csv(path, infer_schema=False, truncate_ragged_lines=True)
        return len(df)
    except Exception:
        return -1


@asset(
    group_name="bronze",
    description=(
        "Ingest a raw lithology log CSV file into the MinIO Bronze bucket "
        "(bronze/lithology/). Files are stored immutably — this is the "
        "source of truth for all reprocessing."
    ),
)
def bronze_lithology(
    context: AssetExecutionContext,
    config: BronzeLithologyConfig,
    minio: S3Resource,
) -> MaterializeResult:
    """Land a raw lithology CSV in the MinIO Bronze bucket."""
    source = resolve_bronze_source(
        minio=minio,
        bucket=BRONZE_BUCKET,
        prefix=LITHOLOGY_PREFIX,
        object_key=config.object_key,
        local_path=config.csv_file_path,
        upload_content_type="text/csv",
    )

    context.log.info(
        "Bronze lithology: source=%s key=%s size=%d sha256=%s",
        "minio" if source.sourced_from_minio else "local",
        source.object_key,
        source.file_size,
        source.sha256,
    )

    row_count = _count_csv_rows(source.local_path)

    try:
        return MaterializeResult(
            metadata={
                "file_name":         MetadataValue.text(Path(source.object_key).name),
                "upload_path":       MetadataValue.text(f"{BRONZE_BUCKET}/{source.object_key}"),
                "file_size_bytes":   MetadataValue.int(source.file_size),
                "row_count":         MetadataValue.int(row_count),
                "sha256_checksum":   MetadataValue.text(source.sha256),
                "sourced_from_minio": MetadataValue.bool(source.sourced_from_minio),
            }
        )
    finally:
        if source.sourced_from_minio:
            try:
                os.unlink(source.local_path)
            except OSError:
                pass
