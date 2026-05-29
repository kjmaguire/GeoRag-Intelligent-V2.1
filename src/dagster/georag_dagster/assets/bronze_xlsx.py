"""Bronze layer asset — raw Excel (.xlsx) ingestion into MinIO.

Accepts either a MinIO ``object_key`` (sensor-driven) or a local
``xlsx_file_path`` (admin/backfill).

NOTE: Do NOT add ``from __future__ import annotations`` to this file.
"""

import os
from pathlib import Path
from typing import Optional

from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets._minio_bronze_helpers import resolve_bronze_source
from georag_dagster.resources import S3Resource

BRONZE_BUCKET = "bronze"
EXCEL_PREFIX = "excel"


class BronzeXlsxConfig(Config):
    object_key: Optional[str] = None
    xlsx_file_path: Optional[str] = None


@asset(
    group_name="bronze",
    description=(
        "Ingest a raw Excel (.xlsx) file into the MinIO Bronze bucket "
        "(bronze/excel/). Files are stored immutably."
    ),
)
def bronze_xlsx(
    context: AssetExecutionContext,
    config: BronzeXlsxConfig,
    minio: S3Resource,
) -> MaterializeResult:
    source = resolve_bronze_source(
        minio=minio,
        bucket=BRONZE_BUCKET,
        prefix=EXCEL_PREFIX,
        object_key=config.object_key,
        local_path=config.xlsx_file_path,
        upload_content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    context.log.info(
        "Bronze xlsx: source=%s key=%s size=%d sha256=%s",
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
