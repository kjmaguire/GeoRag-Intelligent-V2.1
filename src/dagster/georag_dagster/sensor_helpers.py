"""Pure-function helpers for ``minio_upload_sensor``.

Extracted out of ``definitions.py`` so unit tests can exercise the
config-building logic without triggering the full asset import chain
(which pulls in dagster + every parser).
"""
from __future__ import annotations

from typing import Optional


# Maps bronze asset keys to their paired silver asset key. Used by the
# sensor to populate run_config with vendor_profile_id.
BRONZE_TO_SILVER: dict[str, str] = {
    "bronze_collars":    "silver_collars",
    "bronze_surveys":    "silver_surveys",
    "bronze_lithology":  "silver_lithology",
    "bronze_samples":    "silver_samples",
    "bronze_well_logs":  "silver_well_logs",
    "bronze_spatial":    "silver_spatial",
    "bronze_reports":    "silver_reports",
    "bronze_xlsx":       "silver_xlsx",
    "bronze_seismic":    "silver_seismic",
    "bronze_xyz":        "silver_xyz",
    "bronze_geophysics": "silver_geophysics",
}


def build_sensor_run_config(
    triggered_assets: set[str],
    asset_vendor_profile: dict[str, int | None],
    asset_object_key: Optional[dict[str, str]] = None,
) -> dict:
    """Build the run_config ops dict for a minio_upload_sensor RunRequest.

    Two responsibilities:

    * For each triggered **bronze** asset where the sensor observed a
      MinIO object key, set ``object_key`` so the asset reads from MinIO
      instead of demanding a local ``*_file_path``. This is the
      2026-05-23 unification that closed the carryover where Laravel
      uploads to MinIO landed but bronze assets had no way to consume them.
    * For each triggered bronze asset that has a paired **silver** asset,
      set ``vendor_profile_id`` on the silver op (Sprint 5 Phase 1).

    Args:
        triggered_assets: Set of bronze asset keys the sensor decided to
            materialise this poll.
        asset_vendor_profile: Bronze asset key → vendor_profile_id (or
            None) for the silver counterpart.
        asset_object_key: Bronze asset key → MinIO object key the sensor
            just observed. Optional — pre-unification callers may omit it
            and get back-compat (no object_key entries).

    Returns:
        A dict shaped ``{"ops": {asset_key: {"config": {...}}}}`` ready
        to drop into ``RunRequest(run_config=...)``. Empty ``{}`` when
        no ops need configuration.
    """
    ops_config: dict = {}

    if asset_object_key:
        for bronze_key, object_key in asset_object_key.items():
            ops_config[bronze_key] = {"config": {"object_key": object_key}}

    for bronze_key in triggered_assets:
        silver_key = BRONZE_TO_SILVER.get(bronze_key)
        if silver_key is None:
            continue
        vpid = asset_vendor_profile.get(bronze_key)
        ops_config[silver_key] = {"config": {"vendor_profile_id": vpid}}

    return {"ops": ops_config} if ops_config else {}


__all__ = ["BRONZE_TO_SILVER", "build_sensor_run_config"]
