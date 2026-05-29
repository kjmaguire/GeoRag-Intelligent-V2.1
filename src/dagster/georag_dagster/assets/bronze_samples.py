"""Bronze layer asset — raw sample CSV ingestion into MinIO.

Accepts either a MinIO ``object_key`` (sensor-driven) or a local
``csv_file_path`` (admin/backfill). See ``_minio_bronze_helpers`` for
the resolution contract.
"""

import os
from pathlib import Path
from typing import Optional

import polars as pl
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets._minio_bronze_helpers import resolve_bronze_source
from georag_dagster.resources import S3Resource

BRONZE_BUCKET = "bronze"
SAMPLES_PREFIX = "samples"


class BronzeSamplesConfig(Config):
    object_key: Optional[str] = None
    csv_file_path: Optional[str] = None


def _count_csv_rows(path: str) -> int:
    try:
        df = pl.read_csv(path, infer_schema=False, truncate_ragged_lines=True)
        return len(df)
    except Exception:
        return -1


@asset(
    group_name="bronze",
    description=(
        "Ingest a raw geochemical sample CSV file into the MinIO Bronze "
        "bucket (bronze/samples/). Files are stored immutably."
    ),
)
def bronze_samples(
    context: AssetExecutionContext,
    config: BronzeSamplesConfig,
    minio: S3Resource,
) -> MaterializeResult:
    source = resolve_bronze_source(
        minio=minio,
        bucket=BRONZE_BUCKET,
        prefix=SAMPLES_PREFIX,
        object_key=config.object_key,
        local_path=config.csv_file_path,
        upload_content_type="text/csv",
    )
    context.log.info(
        "Bronze samples: source=%s key=%s size=%d sha256=%s",
        "minio" if source.sourced_from_minio else "local",
        source.object_key, source.file_size, source.sha256,
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
