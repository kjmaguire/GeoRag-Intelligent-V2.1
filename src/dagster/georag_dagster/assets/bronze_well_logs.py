"""Bronze layer asset — raw LAS well log ingestion into MinIO.

Accepts either a MinIO ``object_key`` (sensor-driven) or a local
``las_file_path`` (admin/backfill).

NOTE: Do NOT add ``from __future__ import annotations`` to this file —
Dagster 1.13 Config classes rely on runtime annotation evaluation.
"""

import os
from pathlib import Path
from typing import Optional

from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets._minio_bronze_helpers import resolve_bronze_source
from georag_dagster.resources import S3Resource

BRONZE_BUCKET = "bronze"
WELL_LOGS_PREFIX = "well_logs"


class BronzeWellLogsConfig(Config):
    object_key: Optional[str] = None
    las_file_path: Optional[str] = None


@asset(
    group_name="bronze",
    description=(
        "Ingest a raw LAS 2.0 well log file into the MinIO Bronze bucket "
        "(bronze/well_logs/). Files are stored immutably."
    ),
)
def bronze_well_logs(
    context: AssetExecutionContext,
    config: BronzeWellLogsConfig,
    minio: S3Resource,
) -> MaterializeResult:
    source = resolve_bronze_source(
        minio=minio,
        bucket=BRONZE_BUCKET,
        prefix=WELL_LOGS_PREFIX,
        object_key=config.object_key,
        local_path=config.las_file_path,
        upload_content_type="application/octet-stream",
    )
    context.log.info(
        "Bronze well_logs: source=%s key=%s size=%d sha256=%s",
        "minio" if source.sourced_from_minio else "local",
        source.object_key, source.file_size, source.sha256,
    )
    try:
        return MaterializeResult(
            metadata={
                "file_name":         MetadataValue.text(Path(source.object_key).name),
                "upload_path":       MetadataValue.text(f"{BRONZE_BUCKET}/{source.object_key}"),
                "file_size_bytes":   MetadataValue.int(source.file_size),
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
