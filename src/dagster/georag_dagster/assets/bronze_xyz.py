"""Bronze layer asset — raw Geosoft XYZ export ingestion into MinIO.

Accepts either a MinIO ``object_key`` (sensor-driven) or a local
``xyz_file_path`` (admin/backfill).

Geosoft Oasis montaj exports geophysics line data as XYZ text; the
.GDB binary format is not openly readable, so XYZ is the canonical
exchange format for Bronze.

NOTE: Do NOT add ``from __future__ import annotations`` to this file.
"""

import os
from pathlib import Path
from typing import Optional

from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets._minio_bronze_helpers import resolve_bronze_source
from georag_dagster.resources import S3Resource

BRONZE_BUCKET = "bronze"
XYZ_PREFIX = "xyz"


class BronzeXyzConfig(Config):
    object_key: Optional[str] = None
    xyz_file_path: Optional[str] = None


def _count_data_lines(path: str) -> int:
    """Count non-comment, non-blank lines (Geosoft uses ``/`` as a comment prefix)."""
    try:
        n = 0
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("/"):
                    continue
                n += 1
        return n
    except Exception:
        return -1


@asset(
    group_name="bronze",
    description=(
        "Ingest a raw Geosoft XYZ export into the MinIO Bronze bucket "
        "(bronze/xyz/). Files are stored immutably."
    ),
)
def bronze_xyz(
    context: AssetExecutionContext,
    config: BronzeXyzConfig,
    minio: S3Resource,
) -> MaterializeResult:
    source = resolve_bronze_source(
        minio=minio,
        bucket=BRONZE_BUCKET,
        prefix=XYZ_PREFIX,
        object_key=config.object_key,
        local_path=config.xyz_file_path,
        upload_content_type="text/plain",
    )
    context.log.info(
        "Bronze xyz: source=%s key=%s size=%d sha256=%s",
        "minio" if source.sourced_from_minio else "local",
        source.object_key, source.file_size, source.sha256,
    )
    data_line_count = _count_data_lines(source.local_path)
    try:
        return MaterializeResult(
            metadata={
                "file_name":         MetadataValue.text(Path(source.object_key).name),
                "upload_path":       MetadataValue.text(f"{BRONZE_BUCKET}/{source.object_key}"),
                "file_size_bytes":   MetadataValue.int(source.file_size),
                "data_line_count":   MetadataValue.int(data_line_count),
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
