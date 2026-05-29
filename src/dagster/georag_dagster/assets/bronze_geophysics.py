"""Bronze layer asset — raw geophysics interpretation JSON ingestion into MinIO.

Accepts either a MinIO ``object_key`` (sensor-driven, Laravel UploadController
already wrote the file) or a local ``json_file_path`` (admin/backfill —
asset uploads it under ``geophysics/{project_id}/{basename}``).

The geophysics ingestion path covers seismic / magnetic / gravity /
radiometric / IP / EM / other survey METADATA — not raw waveform/grid
binary, that's §11b roadmap. Each upload is a structured JSON document
with the survey-summary shape expected by ``silver_geophysics``:

    {
      "survey_id":             "<optional uuid>",     // omitted → generated
      "survey_type":           "magnetic",            // enum, required
      "survey_name":           "2024 Mag Survey AOI", // required
      "contractor":            "Contractor Ltd",
      "acquisition_date":      "2024-08-15",          // ISO date
      "line_ids":              ["L1","L2","L3"],
      "aoi_wkt":               "POLYGON((...))",      // EPSG:4326
      "crs_epsg":              4326,
      "processing_notes":      "...",
      "interpretation_pdf_id": "<bronze.source_files.id>",
      "anomaly_summary":       "Key anomalies at L2 1.2 km..."
    }

NOTE: Do NOT add ``from __future__ import annotations`` to this file.
"""

import json
import os
from pathlib import Path
from typing import Optional

from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets._minio_bronze_helpers import (
    resolve_bronze_source,
    stream_minio_to_temp,
)
from georag_dagster.resources import S3Resource


BRONZE_BUCKET = "bronze"
GEOPHYSICS_PREFIX = "geophysics"


class BronzeGeophysicsConfig(Config):
    """Runtime configuration for the bronze_geophysics asset.

    Exactly one of ``object_key`` / ``json_file_path`` must be set.
    ``project_id`` only applies in the local-path flow (used to compose
    the MinIO key as ``geophysics/{project_id}/{basename}``); the
    sensor-driven flow uses the key Laravel already wrote.
    """

    object_key: Optional[str] = None
    json_file_path: Optional[str] = None
    project_id: str = ""


def _validate_payload(path: str) -> dict:
    """Parse + minimally validate the JSON shape. Raises on failure."""
    with open(path, "rb") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError("geophysics payload must be a JSON object (got non-dict)")
    if not payload.get("survey_name"):
        raise ValueError("geophysics payload missing required 'survey_name'")
    if not payload.get("survey_type"):
        raise ValueError("geophysics payload missing required 'survey_type'")
    return payload


@asset(
    group_name="bronze",
    description=(
        "Ingest a raw geophysics interpretation JSON file into the MinIO Bronze "
        "bucket under geophysics/. Validates survey_name + survey_type so "
        "silver_geophysics can consume it."
    ),
)
def bronze_geophysics(
    context: AssetExecutionContext,
    config: BronzeGeophysicsConfig,
    minio: S3Resource,
) -> MaterializeResult:
    if not config.object_key and not config.json_file_path:
        raise ValueError(
            "bronze_geophysics requires either `object_key` (sensor) or "
            "`json_file_path` (local) — both unset."
        )

    if config.object_key:
        # Sensor-driven: stream the existing MinIO object down to a temp
        # file just long enough to validate + report metadata.
        local_path, sha, file_size = stream_minio_to_temp(
            minio, BRONZE_BUCKET, config.object_key, suffix=".json"
        )
        object_key = config.object_key
        sourced_from_minio = True
        cleanup_path = local_path
    else:
        # Local-path: upload via the standard resolver (chooses the key
        # off the basename) but rewrite key when project_id is set, to
        # preserve the original ``geophysics/{project_id}/{basename}``
        # contract.
        local_path = config.json_file_path
        basename = Path(local_path).name
        if config.project_id:
            target_key = f"{GEOPHYSICS_PREFIX}/{config.project_id}/{basename}"
            # Manual upload preserves the project_id-scoped key.
            with open(local_path, "rb") as fh:
                minio.put_object(BRONZE_BUCKET, target_key, fh.read())
            object_key = target_key
            file_size = os.path.getsize(local_path)
            from georag_dagster.assets._minio_bronze_helpers import sha256_file
            sha = sha256_file(local_path)
        else:
            source = resolve_bronze_source(
                minio=minio,
                bucket=BRONZE_BUCKET,
                prefix=GEOPHYSICS_PREFIX,
                object_key=None,
                local_path=local_path,
                upload_content_type="application/json",
            )
            object_key = source.object_key
            sha = source.sha256
            file_size = source.file_size
        sourced_from_minio = False
        cleanup_path = None

    try:
        payload = _validate_payload(local_path)
        context.log.info(
            "bronze_geophysics: source=%s key=%s size=%d sha256=%s survey_type=%s",
            "minio" if sourced_from_minio else "local",
            object_key, file_size, sha[:12], payload.get("survey_type"),
        )
        return MaterializeResult(
            metadata={
                "object_key":    MetadataValue.text(f"{BRONZE_BUCKET}/{object_key}"),
                "file_size":     MetadataValue.int(file_size),
                "sha256":        MetadataValue.text(sha),
                "survey_type":   MetadataValue.text(str(payload.get("survey_type"))),
                "survey_name":   MetadataValue.text(str(payload.get("survey_name"))),
                "has_aoi":       MetadataValue.bool(bool(payload.get("aoi_wkt"))),
                "line_count":    MetadataValue.int(len(payload.get("line_ids") or [])),
                "project_id":    MetadataValue.text(config.project_id or ""),
                "sourced_from_minio": MetadataValue.bool(sourced_from_minio),
            }
        )
    finally:
        if cleanup_path:
            try:
                os.unlink(cleanup_path)
            except OSError:
                pass
