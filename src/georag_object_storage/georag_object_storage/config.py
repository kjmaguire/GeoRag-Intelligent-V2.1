"""Environment-based configuration for georag_object_storage.

Canonical env vars (``AWS_*``) are read first; each has a documented legacy
fallback chain covering the ``S3_*``/``MINIO_*``/``SEAWEEDFS_*`` names
already scattered across today's deployments (confirmed via a repo-wide
grep of ``docker-compose.yml`` and the Python call sites), so existing
``.env`` files in any environment keep working without a day-one change —
only application code changes to read through this module instead of
constructing its own client.

Deliberately a single ``AWS_ENDPOINT_URL`` canonical var, not the runbook's
original two-var ``AWS_ENDPOINT_URL`` + ``S3_ENDPOINT_URL`` "kept in sync"
requirement — that duplication was itself a source of drift.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from georag_object_storage.buckets import Bucket

logger = logging.getLogger(__name__)

_warned: set[str] = set()


def _resolve(
    canonical: str,
    *legacy: str,
    default: str | None = None,
    required: bool = False,
) -> str | None:
    value = os.environ.get(canonical)
    if value:
        return value
    for name in legacy:
        value = os.environ.get(name)
        if value:
            if canonical not in _warned:
                logger.warning(
                    "georag_object_storage: %s is unset; falling back to legacy env var %s. "
                    "Set %s to silence this warning.",
                    canonical,
                    name,
                    canonical,
                )
                _warned.add(canonical)
            return value
    if value is None and default is not None:
        return default
    if required:
        names = ", ".join((canonical, *legacy))
        raise ValueError(f"one of these env vars must be set: {names}")
    return default


# (canonical env var, legacy fallback env vars, default bucket name)
_BUCKET_ENV: dict[Bucket, tuple[str, tuple[str, ...], str]] = {
    Bucket.BRONZE: ("AWS_BUCKET_BRONZE", ("S3_BUCKET_BRONZE", "MINIO_BUCKET_BRONZE", "S3_BUCKET"), "bronze"),
    Bucket.BRONZE_RASTER: ("AWS_BUCKET_BRONZE_RASTER", ("MINIO_BUCKET_BRONZE_RASTER",), "bronze-raster"),
    Bucket.EXPORTS: ("AWS_BUCKET_EXPORTS", ("MINIO_BUCKET_EXPORTS",), "exports"),
    Bucket.BACKUPS: ("AWS_BUCKET_BACKUPS", ("MINIO_BUCKET_BACKUPS",), "georag-backups"),
}


@dataclass(frozen=True)
class StorageConfig:
    """Resolved object-storage connection settings.

    Build via :meth:`from_env` rather than constructing directly, so the
    canonical/legacy env-var resolution logic stays in one place.
    """

    endpoint_url: str
    access_key: str
    secret_key: str
    region: str
    bucket_names: dict[Bucket, str] = field(default_factory=dict)

    def bucket_name(self, bucket: Bucket) -> str:
        return self.bucket_names[bucket]

    @classmethod
    def from_env(cls) -> StorageConfig:
        endpoint_url = _resolve(
            "AWS_ENDPOINT_URL",
            "S3_ENDPOINT_URL",
            "S3_ENDPOINT",
            "MINIO_ENDPOINT",
            "SEAWEEDFS_S3_ENDPOINT",
            default="http://minio:8333",
        )
        access_key = _resolve(
            "AWS_ACCESS_KEY_ID",
            "S3_ACCESS_KEY",
            "MINIO_ROOT_USER",
            "MINIO_ACCESS_KEY",
            "SEAWEEDFS_ACCESS_KEY",
            "SEAWEEDFS_S3_ACCESS_KEY",
            required=True,
        )
        secret_key = _resolve(
            "AWS_SECRET_ACCESS_KEY",
            "S3_SECRET_KEY",
            "MINIO_ROOT_PASSWORD",
            "MINIO_SECRET_KEY",
            "SEAWEEDFS_SECRET_KEY",
            "SEAWEEDFS_S3_SECRET_KEY",
            required=True,
        )
        # SEAWEEDFS_S3_REGION — found during the PR5b migration of
        # backup_seaweedfs.py, which reads a region override no other call
        # site did (every other file hardcodes "us-east-1" directly).
        region = _resolve("AWS_DEFAULT_REGION", "SEAWEEDFS_S3_REGION", default="us-east-1")

        bucket_names = {
            bucket: _resolve(canonical, *legacy, default=default)
            for bucket, (canonical, legacy, default) in _BUCKET_ENV.items()
        }

        return cls(
            endpoint_url=endpoint_url,
            access_key=access_key,
            secret_key=secret_key,
            region=region,
            bucket_names=bucket_names,
        )
