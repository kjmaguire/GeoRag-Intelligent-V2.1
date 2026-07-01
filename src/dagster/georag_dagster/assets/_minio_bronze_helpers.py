"""Shared helpers for Bronze assets that need to source data either from
a local file path (legacy / admin / backfill) or from a MinIO object key
(Laravel UploadController + minio_upload_sensor flow).

Why this exists (2026-05-23): every Bronze asset (collars, surveys,
lithology, samples, well_logs, spatial, reports, xlsx, seismic, xyz,
geophysics) was hard-wired to a local ``*_file_path: str`` config. The
production upload path was MinIO-first via Laravel, so the sensor could
detect new objects but couldn't actually feed any bronze asset — it
would fail config validation because no local path existed.

This helper lets every bronze asset accept EITHER:

  * the legacy local ``*_file_path``  — admin/backfill flow, still
    uploads to MinIO with idempotent skip-if-matching-size, OR
  * a new ``object_key`` pointing at an existing MinIO object —
    sensor-driven flow, no re-upload, computes checksum/row-count by
    streaming the object body.

Each bronze asset wires up via :func:`resolve_bronze_source` and treats
its rest-of-flow uniformly against the returned ``BronzeSource``.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class BronzeSource:
    """The materialised local handle a Bronze asset can read.

    ``local_path`` is always populated (either the caller's original path
    or a temp file streamed down from MinIO). Callers MUST NOT delete it
    — it's a context-managed handle owned by this module's ``with`` form
    if you use :func:`stream_minio_to_temp`, otherwise the caller's own.
    """

    local_path: str
    object_key: str
    sha256: str
    file_size: int
    sourced_from_minio: bool


def sha256_file(path: str) -> str:
    """SHA-256 a file by streaming 64 KiB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65_536), b""):
            h.update(chunk)
    return h.hexdigest()


def stream_minio_to_temp(
    minio,
    bucket: str,
    object_key: str,
    *,
    suffix: str = "",
) -> tuple[str, str, int]:
    """Download a MinIO object to a NamedTemporaryFile, hashing as we go.

    Returns ``(local_path, sha256_hex, byte_count)``.

    The temp file is *not* auto-deleted — callers either reuse it as a
    parser input or unlink it themselves. The pattern matches how the
    other parsers consume disk-bound paths in this codebase.
    """
    s3 = minio.get_client()
    suffix = suffix or Path(object_key).suffix

    h = hashlib.sha256()
    total = 0
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="bronze_minio_")
    try:
        with os.fdopen(fd, "wb") as out:
            resp = s3.get_object(Bucket=bucket, Key=object_key)
            body = resp["Body"]
            while True:
                chunk = body.read(65_536)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                total += len(chunk)
    except Exception:
        try:  # noqa: SIM105
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path, h.hexdigest(), total


def resolve_bronze_source(
    *,
    minio,
    bucket: str,
    prefix: str,
    object_key: Optional[str],
    local_path: Optional[str],
    upload_content_type: str,
) -> BronzeSource:
    """Resolve a bronze input from EITHER ``object_key`` or ``local_path``.

    Behaviour:

    * ``object_key`` set, ``local_path`` not — sensor-driven flow. Streams
      the MinIO object down to a temp file, hashes the body, returns a
      :class:`BronzeSource` with ``sourced_from_minio=True``.
    * ``local_path`` set, ``object_key`` not — admin/backfill flow. Hashes
      the local file, uploads to ``bucket/{prefix}/{basename}`` (skips if
      an object with matching size already exists), returns a
      :class:`BronzeSource` with ``sourced_from_minio=False``.
    * Both unset — raises ``ValueError``.
    * Both set — ``object_key`` wins (sensor is the authoritative path).
    """
    if not object_key and not local_path:
        raise ValueError(
            "Bronze asset requires either `object_key` (MinIO) or a "
            "`*_file_path` (local) config — both are unset."
        )

    if object_key:
        tmp_path, sha, size = stream_minio_to_temp(minio, bucket, object_key)
        return BronzeSource(
            local_path=tmp_path,
            object_key=object_key,
            sha256=sha,
            file_size=size,
            sourced_from_minio=True,
        )

    # Local-file mode (legacy admin / backfill)
    if not os.path.isfile(local_path):
        raise FileNotFoundError(f"Bronze: local file not found: {local_path!r}")

    filename = Path(local_path).name
    derived_key = f"{prefix}/{filename}"
    file_size = os.path.getsize(local_path)
    sha = sha256_file(local_path)

    already_uploaded = False
    if minio.object_exists(bucket, derived_key):
        stat = minio.stat_object(bucket, derived_key)
        if stat["size"] == file_size:
            already_uploaded = True

    if not already_uploaded:
        minio.upload_file(
            bucket=bucket,
            object_name=derived_key,
            file_path=local_path,
            content_type=upload_content_type,
        )

    return BronzeSource(
        local_path=local_path,
        object_key=derived_key,
        sha256=sha,
        file_size=file_size,
        sourced_from_minio=False,
    )


__all__ = [
    "BronzeSource",
    "resolve_bronze_source",
    "sha256_file",
    "stream_minio_to_temp",
]
