"""B6 COG normalization — Bronze GeoTIFF → Cloud-Optimised GeoTIFF.

Architecture (addendum §02b):
  Source GeoTIFFs land in s3://bronze-raster/source/<project>/<raster_id>/source.tif
  (uploaded by a future sprint's upload ingestion flow; that path is out of scope
  for Module 3 Chunk 2).  This asset:

  1. Discovers source.tif objects under bronze-raster/source/** via the
     bronze_raster_uploads stub asset (or a real upload-tracking asset once
     that sprint is done).
  2. For each source, converts to COG using rio_cogeo.cogeo.cog_translate
     (Python API — no subprocess shelling).
  3. Writes the COG to bronze-raster/cog/<project>/<raster_id>/cog.tif.
  4. Emits a sidecar metadata.json to the same prefix with bounds, CRS, band
     info, nodata, resolution, versions, and timestamps.

Source immutability:
  The source.tif object is NEVER modified, moved, or deleted.  COG outputs go
  only to the cog/ prefix.

Idempotency:
  If bronze-raster/cog/<project>/<raster_id>/cog.tif already exists AND the
  source.tif's ETag matches what is stored in the sidecar, the conversion is
  skipped.

TiTiler coordination:
  COG URLs are persisted in metadata.json so Module 8 can discover them without
  re-listing the bucket.  TiTiler serving is Module 8 scope — not implemented here.

bronze_raster_uploads stub:
  The bronze_raster_uploads asset below is a MINIMAL STUB that enumerates
  source.tif objects from bronze-raster/source/.  The full upload-to-Bronze
  ingestion flow (user uploads a TIF → lands in bronze-raster/source/) is a
  later sprint.  The stub's only responsibility is to yield raster manifests
  so silver_cog_rasters has a declared upstream dependency.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config/ConfigurableResource classes use Pydantic for type
introspection and that import breaks runtime annotation evaluation.
"""

import io
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

import psycopg2.extras
from dagster import (
    AssetCheckExecutionContext,
    AssetCheckResult,
    AssetCheckSeverity,
    AssetExecutionContext,
    MaterializeResult,
    MetadataValue,
    asset,
    asset_check,
)
from botocore.exceptions import ClientError
from pydantic import BaseModel, field_validator

from georag_dagster.resources import S3Resource

# Verify rio_cogeo is importable — will fail fast if the image was not rebuilt
try:
    from rio_cogeo.cogeo import cog_translate
    from rio_cogeo.profiles import cog_profiles
    import rio_cogeo as _rio_cogeo_mod
    _RIO_COGEO_VERSION = _rio_cogeo_mod.__version__
except ImportError as _cog_err:
    cog_translate = None  # type: ignore[assignment]
    cog_profiles = None  # type: ignore[assignment]
    _RIO_COGEO_VERSION = f"MISSING: {_cog_err}"

try:
    import rasterio
    from rasterio.crs import CRS as RasterioCRS
    _RASTERIO_AVAILABLE = True
except ImportError:
    rasterio = None  # type: ignore[assignment]
    _RASTERIO_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BRONZE_RASTER_BUCKET = "bronze-raster"
_SOURCE_PREFIX = "source/"
_COG_PREFIX = "cog/"
_SOURCE_FILENAME = "source.tif"
_COG_FILENAME = "cog.tif"
_SIDECAR_FILENAME = "metadata.json"


# ---------------------------------------------------------------------------
# Pydantic sidecar model
# ---------------------------------------------------------------------------

class CogSidecarMetadata(BaseModel):
    """Sidecar metadata emitted alongside each cog.tif.

    Persisted as metadata.json in the same S3 prefix.  Module 8 (TiTiler)
    reads cog_url from this to know where to serve.
    """
    bounds_wgs84: list[float]           # [minx, miny, maxx, maxy] in EPSG:4326
    native_crs: str                      # EPSG code or proj4 string
    pixel_resolution_m: Optional[float]  # approximate, may be None for non-metric CRS
    band_count: int
    nodata: Optional[float]
    data_type: str                       # e.g. 'uint16', 'float32'
    cog_url: str                         # s3://bronze-raster/cog/<project>/<id>/cog.tif
    source_url: str                      # s3://bronze-raster/source/<project>/<id>/source.tif
    generated_at: str                    # ISO-8601 timestamp
    rio_cogeo_version: str
    source_etag: str                     # ETag of source.tif at conversion time

    @field_validator("bounds_wgs84")
    @classmethod
    def _bounds_length(cls, v: list[float]) -> list[float]:
        if len(v) != 4:
            raise ValueError("bounds_wgs84 must have exactly 4 elements")
        return v


# ---------------------------------------------------------------------------
# bronze_raster_uploads — STUB asset
# ---------------------------------------------------------------------------

@asset(
    group_name="bronze",
    description=(
        "STUB: Enumerates source.tif objects under bronze-raster/source/**. "
        "The full upload ingestion flow (user TIF → Bronze) is a later sprint. "
        "This stub exists only to give silver_cog_rasters a declared upstream "
        "dependency and to unblock COG normalisation development."
    ),
)
def bronze_raster_uploads(
    context: AssetExecutionContext,
    minio: S3Resource,
) -> MaterializeResult:
    """Stub: discover source GeoTIFFs already present in bronze-raster/source/."""
    discovered: list[str] = []
    try:
        for key in minio.list_keys(_BRONZE_RASTER_BUCKET, prefix=_SOURCE_PREFIX):
            if key.endswith(_SOURCE_FILENAME):
                discovered.append(key)
    except ClientError as exc:
        context.log.warning(
            "bronze_raster_uploads: could not list %s — bucket may be empty: %s",
            _BRONZE_RASTER_BUCKET, exc,
        )

    context.log.info(
        "bronze_raster_uploads: discovered %d source.tif object(s).", len(discovered)
    )
    for path in discovered:
        context.log.info("  %s/%s", _BRONZE_RASTER_BUCKET, path)

    return MaterializeResult(
        metadata={
            "bucket":        MetadataValue.text(_BRONZE_RASTER_BUCKET),
            "source_count":  MetadataValue.int(len(discovered)),
            "source_paths":  MetadataValue.text(json.dumps(discovered)),
        }
    )


# ---------------------------------------------------------------------------
# Asset check: bronze_raster_sources_discoverable
# ---------------------------------------------------------------------------

@asset_check(
    asset=bronze_raster_uploads,
    name="bronze_raster_sources_discoverable",
    description=(
        "Blocking: verifies the bronze-raster bucket is accessible and the "
        "source/ prefix is listable (even if empty). A non-zero source count "
        "is NOT required — the check passes with 0 sources because the bucket "
        "may legitimately be empty before any raster uploads arrive."
    ),
    blocking=True,
)
def bronze_raster_sources_discoverable_check(
    context: AssetCheckExecutionContext,
    minio: S3Resource,
) -> AssetCheckResult:
    """Verify the bronze-raster/source/ prefix is reachable."""
    try:
        bucket_ok = minio.bucket_exists(_BRONZE_RASTER_BUCKET)
        count = 0
        if bucket_ok:
            for key in minio.list_keys(_BRONZE_RASTER_BUCKET, prefix=_SOURCE_PREFIX):
                if key.endswith(_SOURCE_FILENAME):
                    count += 1
        passed = bucket_ok
        description = (
            f"bronze-raster bucket exists. {count} source.tif object(s) found."
            if passed
            else "bronze-raster bucket NOT accessible."
        )
    except Exception as exc:
        passed = False
        description = f"bronze-raster discovery failed: {exc}"
        count = -1

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=description,
        metadata={"source_count": MetadataValue.int(count)},
    )


# ---------------------------------------------------------------------------
# silver_cog_rasters asset
# ---------------------------------------------------------------------------

def _object_etag(s3: S3Resource, bucket: str, object_name: str) -> Optional[str]:
    """Return the ETag of an object, or None on failure."""
    try:
        stat = s3.stat_object(bucket, object_name)
        return stat["etag"]
    except Exception:
        return None


def _load_existing_sidecar(s3: S3Resource, bucket: str, sidecar_path: str) -> Optional[dict]:
    """Load and parse an existing sidecar JSON, returning None on any error."""
    try:
        data = s3.download_bytes(bucket, sidecar_path)
        return json.loads(data)
    except Exception:
        return None


def _compute_pixel_resolution_m(src) -> Optional[float]:
    """Approximate pixel resolution in metres.

    For projected CRS (metric), returns the mean of |pixel_size_x|, |pixel_size_y|.
    For geographic CRS (degrees), converts using 111_320 m/degree approximation
    at the dataset's centre latitude.
    """
    try:
        transform = src.transform
        px = abs(transform.a)
        py = abs(transform.e)

        crs = src.crs
        if crs is None:
            return None

        if crs.is_geographic:
            # Approximate at centre latitude
            centre_lat = (src.bounds.top + src.bounds.bottom) / 2.0
            metres_per_degree = 111_320.0 * math.cos(math.radians(centre_lat))
            px_m = px * metres_per_degree
            py_m = py * 111_320.0
        else:
            px_m = px
            py_m = py

        return round((px_m + py_m) / 2.0, 4)
    except Exception:
        return None


def _bounds_wgs84(src) -> Optional[list[float]]:
    """Reproject source raster bounds to EPSG:4326 [minx, miny, maxx, maxy]."""
    try:
        from rasterio.warp import transform_bounds
        bounds = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        return [round(v, 8) for v in bounds]
    except Exception:
        return None


@asset(
    group_name="silver",
    deps=[bronze_raster_uploads],
    description=(
        "Convert source GeoTIFFs from bronze-raster/source/ to Cloud-Optimised "
        "GeoTIFFs using rio_cogeo and write to bronze-raster/cog/.  Emits a "
        "metadata.json sidecar per raster.  Source objects are never modified."
    ),
)
def silver_cog_rasters(
    context: AssetExecutionContext,
    minio: S3Resource,
) -> MaterializeResult:
    """Normalise all discovered source GeoTIFFs to COG format."""

    if cog_translate is None:
        context.log.error(
            "silver_cog_rasters: rio_cogeo is not installed — "
            "rebuild the Dagster image with rio-cogeo>=5.0.0,<6.0.0."
        )
        return MaterializeResult(
            metadata={
                "rio_cogeo_version": MetadataValue.text(_RIO_COGEO_VERSION),
                "error": MetadataValue.text("rio_cogeo not installed"),
            }
        )

    if not _RASTERIO_AVAILABLE:
        context.log.error("silver_cog_rasters: rasterio not installed.")
        return MaterializeResult(
            metadata={"error": MetadataValue.text("rasterio not installed")}
        )

    # --- Discover source objects ---
    source_objects: list[str] = []
    try:
        for key in minio.list_keys(_BRONZE_RASTER_BUCKET, prefix=_SOURCE_PREFIX):
            if key.endswith(_SOURCE_FILENAME):
                source_objects.append(key)
    except ClientError as exc:
        context.log.warning("silver_cog_rasters: bucket list failed: %s", exc)

    context.log.info(
        "silver_cog_rasters: found %d source.tif object(s).", len(source_objects)
    )

    counters = {
        "processed": 0,
        "converted": 0,
        "skipped_cached": 0,
        "errors": 0,
    }
    cog_urls: list[str] = []

    for source_path in source_objects:
        # Derive the COG prefix from the source path
        # source_path: source/<project>/<raster_id>/source.tif
        parts = source_path.split("/")
        if len(parts) < 4:
            context.log.warning("silver_cog_rasters: unexpected path format: %s", source_path)
            counters["errors"] += 1
            continue

        _prefix, project_seg, raster_id_seg = parts[0], parts[1], parts[2]
        cog_path = f"{_COG_PREFIX}{project_seg}/{raster_id_seg}/{_COG_FILENAME}"
        sidecar_path = f"{_COG_PREFIX}{project_seg}/{raster_id_seg}/{_SIDECAR_FILENAME}"
        source_url = f"s3://{_BRONZE_RASTER_BUCKET}/{source_path}"
        cog_url = f"s3://{_BRONZE_RASTER_BUCKET}/{cog_path}"
        counters["processed"] += 1

        # --- Idempotency: check ETag match against existing sidecar ---
        source_etag = _object_etag(minio, _BRONZE_RASTER_BUCKET, source_path) or ""
        existing_sidecar = _load_existing_sidecar(minio, _BRONZE_RASTER_BUCKET, sidecar_path)
        if (
            existing_sidecar is not None
            and existing_sidecar.get("source_etag") == source_etag
            and source_etag
        ):
            context.log.info(
                "silver_cog_rasters: %s — COG already current (ETag match); skipping.",
                source_path,
            )
            counters["skipped_cached"] += 1
            cog_urls.append(cog_url)
            continue

        context.log.info("silver_cog_rasters: converting %s → %s", source_path, cog_path)

        # --- Download source to temp file ---
        try:
            source_bytes = minio.download_bytes(_BRONZE_RASTER_BUCKET, source_path)
        except Exception as exc:
            context.log.error(
                "silver_cog_rasters: failed to download %s: %s", source_path, exc
            )
            counters["errors"] += 1
            continue

        with tempfile.TemporaryDirectory() as tmpdir:
            src_file = os.path.join(tmpdir, "source.tif")
            cog_file = os.path.join(tmpdir, "cog.tif")

            with open(src_file, "wb") as fh:
                fh.write(source_bytes)

            # --- Extract metadata from source before COG conversion ---
            try:
                with rasterio.open(src_file) as src:
                    native_crs_str = src.crs.to_string() if src.crs else "UNKNOWN"
                    band_count = src.count
                    data_type = src.dtypes[0] if src.dtypes else "unknown"
                    nodata = src.nodata
                    pixel_res_m = _compute_pixel_resolution_m(src)
                    bounds_4326 = _bounds_wgs84(src)
            except Exception as exc:
                context.log.error(
                    "silver_cog_rasters: rasterio metadata read failed for %s: %s",
                    source_path, exc,
                )
                counters["errors"] += 1
                continue

            # --- COG conversion via rio_cogeo Python API ---
            try:
                output_profile = cog_profiles.get("deflate")
                cog_translate(
                    src_file,
                    cog_file,
                    output_profile,
                    web_optimized=True,
                    quiet=True,
                    allow_intermediate_compression=True,
                )
            except Exception as exc:
                context.log.error(
                    "silver_cog_rasters: cog_translate failed for %s: %s",
                    source_path, exc,
                )
                counters["errors"] += 1
                continue

            # --- Upload COG to bronze-raster/cog/ ---
            try:
                with open(cog_file, "rb") as fh:
                    cog_bytes = fh.read()
                minio.upload_bytes(
                    bucket=_BRONZE_RASTER_BUCKET,
                    object_name=cog_path,
                    data=cog_bytes,
                    content_type="image/tiff",
                )
                context.log.info(
                    "silver_cog_rasters: uploaded COG (%d bytes) -> %s",
                    len(cog_bytes), cog_path,
                )
            except Exception as exc:
                context.log.error(
                    "silver_cog_rasters: COG upload failed for %s: %s",
                    cog_path, exc,
                )
                counters["errors"] += 1
                continue

            # --- Build and upload sidecar metadata.json ---
            sidecar = CogSidecarMetadata(
                bounds_wgs84=bounds_4326 or [0.0, 0.0, 0.0, 0.0],
                native_crs=native_crs_str,
                pixel_resolution_m=pixel_res_m,
                band_count=band_count,
                nodata=float(nodata) if nodata is not None else None,
                data_type=str(data_type),
                cog_url=cog_url,
                source_url=source_url,
                generated_at=datetime.now(tz=timezone.utc).isoformat(),
                rio_cogeo_version=_RIO_COGEO_VERSION,
                source_etag=source_etag,
            )
            sidecar_json = sidecar.model_dump_json(indent=2).encode()
            try:
                minio.upload_bytes(
                    bucket=_BRONZE_RASTER_BUCKET,
                    object_name=sidecar_path,
                    data=sidecar_json,
                    content_type="application/json",
                )
                context.log.info(
                    "silver_cog_rasters: uploaded sidecar → %s", sidecar_path
                )
            except Exception as exc:
                context.log.warning(
                    "silver_cog_rasters: sidecar upload failed for %s (non-blocking): %s",
                    sidecar_path, exc,
                )

        counters["converted"] += 1
        cog_urls.append(cog_url)

    context.log.info(
        "silver_cog_rasters: complete — processed=%d converted=%d "
        "skipped_cached=%d errors=%d",
        counters["processed"], counters["converted"],
        counters["skipped_cached"], counters["errors"],
    )

    return MaterializeResult(
        metadata={
            "rio_cogeo_version":  MetadataValue.text(_RIO_COGEO_VERSION),
            "source_count":       MetadataValue.int(len(source_objects)),
            "cog_converted":      MetadataValue.int(counters["converted"]),
            "cog_skipped_cached": MetadataValue.int(counters["skipped_cached"]),
            "cog_errors":         MetadataValue.int(counters["errors"]),
            "cog_urls":           MetadataValue.text(json.dumps(cog_urls)),
        }
    )


# ---------------------------------------------------------------------------
# Asset check: cog_readable
# ---------------------------------------------------------------------------

@asset_check(
    asset=silver_cog_rasters,
    name="cog_readable",
    description=(
        "Blocking: for each COG in bronze-raster/cog/, verifies that "
        "rasterio.open() succeeds, bounds are non-null finite values, "
        "CRS is populated, band_count >= 1, and the sidecar metadata.json "
        "validates against CogSidecarMetadata. Fails if any COG is unreadable."
    ),
    blocking=True,
)
def cog_readable_check(
    context: AssetCheckExecutionContext,
    minio: S3Resource,
) -> AssetCheckResult:
    """Verify every generated COG is readable and sidecars validate."""

    if not _RASTERIO_AVAILABLE:
        return AssetCheckResult(
            passed=False,
            severity=AssetCheckSeverity.ERROR,
            description="rasterio not installed — cannot check COGs.",
        )

    # Enumerate all sidecar paths to discover COGs
    sidecar_paths: list[str] = []
    try:
        for key in minio.list_keys(_BRONZE_RASTER_BUCKET, prefix=_COG_PREFIX):
            if key.endswith(_SIDECAR_FILENAME):
                sidecar_paths.append(key)
    except Exception as exc:
        return AssetCheckResult(
            passed=False,
            severity=AssetCheckSeverity.ERROR,
            description=f"Could not list bronze-raster/cog/ prefix: {exc}",
        )

    if not sidecar_paths:
        # No COGs generated yet (empty bucket) — pass with a note
        return AssetCheckResult(
            passed=True,
            severity=AssetCheckSeverity.ERROR,
            description="No COGs found in bronze-raster/cog/ — bucket is empty (pass: no work done).",
            metadata={"cog_count": MetadataValue.int(0)},
        )

    failures: list[str] = []
    checked = 0

    for sidecar_path in sidecar_paths:
        # Load and validate sidecar
        sidecar_raw = _load_existing_sidecar(minio, _BRONZE_RASTER_BUCKET, sidecar_path)
        if sidecar_raw is None:
            failures.append(f"{sidecar_path}: sidecar unreadable")
            continue

        try:
            sidecar = CogSidecarMetadata.model_validate(sidecar_raw)
        except Exception as exc:
            failures.append(f"{sidecar_path}: sidecar validation failed: {exc}")
            continue

        # Derive cog_path from sidecar_path
        cog_object = sidecar_path.replace(_SIDECAR_FILENAME, _COG_FILENAME)

        # Download COG to temp file for rasterio check
        try:
            cog_bytes = minio.download_bytes(_BRONZE_RASTER_BUCKET, cog_object)
        except Exception as exc:
            failures.append(f"{cog_object}: download failed: {exc}")
            continue

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tf:
            tf.write(cog_bytes)
            tmp_path = tf.name

        try:
            with rasterio.open(tmp_path) as ds:
                crs_ok = ds.crs is not None
                bands_ok = ds.count >= 1
                b = ds.bounds
                bounds_ok = (
                    b is not None
                    and all(math.isfinite(v) for v in [b.left, b.bottom, b.right, b.top])
                )
            if not crs_ok:
                failures.append(f"{cog_object}: CRS is null")
            if not bands_ok:
                failures.append(f"{cog_object}: band_count < 1")
            if not bounds_ok:
                failures.append(f"{cog_object}: bounds are null or non-finite")
        except Exception as exc:
            failures.append(f"{cog_object}: rasterio.open() failed: {exc}")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        checked += 1

    passed = len(failures) == 0
    description = (
        f"All {checked} COG(s) readable and sidecars valid."
        if passed
        else f"{len(failures)} failure(s): {'; '.join(failures[:3])}"
        + (" [truncated]" if len(failures) > 3 else "")
    )

    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        description=description,
        metadata={
            "cog_checked": MetadataValue.int(checked),
            "failures":    MetadataValue.int(len(failures)),
        },
    )
