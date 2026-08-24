"""Capture a georeferenced raster's coordinates before it becomes a page image.

Why this exists
---------------
ADR-0005 routes every uploaded ``.tif`` through ``tiff_normalize``, which
wraps it losslessly to PDF so the §04p document stack can OCR it. That is
the right path for a scanned report page. It is a lossy one for a
*georeferenced* raster: the PDF wrap preserves every pixel and discards the
CRS, the geotransform and the bounds entirely. A scanned geological map
arrives as a flat picture with no idea where on Earth it is.

Nothing else recovers that. ``silver_raster`` / ``silver_cog_rasters`` live
in the Dagster tree, which has no deployed resources, so an upload can never
reach them. The information is present in the file for exactly as long as
``tiff_normalize`` holds its bytes, and is gone afterwards.

So this module reads the header off the file the workflow has *already*
downloaded and writes one ``silver.raster_layers`` row. It does not touch
the OCR path, does not change what gets wrapped, and cannot fail the
ingest — it is pure capture, deliberately additive.

Deliberately separate from ``tiff_normalize``
---------------------------------------------
The call site is three lines so that this survives whatever happens to the
TIFF→PDF path. If ADR-0005 is revisited and the wrap is replaced by direct
image OCR, ``persist_raster_metadata`` is still correct — only the caller
moves.

What counts as worth recording
------------------------------
Only rasters that actually carry a CRS. A scanned report page is a TIFF
too, and cataloguing every one of them as a "raster layer" would bury the
handful of real map sheets in thousands of document pages. No CRS means
nothing to preserve, so nothing is written.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("georag.ingest.raster_metadata")

__all__ = [
    "persist_raster_metadata",
    "RasterCaptureResult",
    "_is_measurement_raster",
]


class RasterCaptureResult:
    """Outcome of one capture attempt. Never raised, always returned."""

    __slots__ = (
        "written", "reason", "crs", "raster_id", "is_measurement_raster",
    )

    def __init__(
        self,
        *,
        written: bool,
        reason: str,
        crs: str | None = None,
        raster_id: str | None = None,
        is_measurement_raster: bool = False,
    ) -> None:
        self.written = written
        self.reason = reason
        self.crs = crs
        self.raster_id = raster_id
        #: Set from the raster header, INDEPENDENTLY of whether the row was
        #: written. `already_recorded` and `persist_failed` must classify
        #: the same way `recorded` does, or a Hatchet retry would push a
        #: magnetics grid through OCR that the first attempt correctly
        #: skipped.
        self.is_measurement_raster = is_measurement_raster

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return (
            f"RasterCaptureResult(written={self.written}, reason={self.reason!r}, "
            f"crs={self.crs!r}, "
            f"is_measurement_raster={self.is_measurement_raster})"
        )


#: Bit depths a document scanner can produce. A scanned map sheet is 1-bit
#: bilevel, 8-bit greyscale or 8-bit RGB, always {D} there is no scanner that
#: emits Float32. Anything wider is a measurement grid.
_SCANNABLE_DTYPES = frozenset({"uint8", "int8", "bool", "uint1"})


def _is_measurement_raster(result: Any) -> bool:
    """True when this raster is data to be read by a machine, not by eye.

    ADR-0005's reasoning {D} wrap a TIFF to PDF and run the §04p stack over
    it {D} is sound for a scanned map sheet, which is a picture of a page
    with text on it. It is wrong for a DEM, an airborne magnetics grid or a
    multispectral scene: those have no text at all, so Document
    Intelligence bills for a continuous-tone surface and whatever character
    noise comes back is chunked, embedded and indexed as retrievable
    passages that then compete in the recall set of every future query.

    Deliberately conservative in the ambiguous direction. It takes BOTH a
    CRS and a non-scanner bit depth to skip OCR, so:

    * a scanned sheet that was later georeferenced (CRS, 8-bit) still goes
      through OCR {D} it is exactly ADR-0005's target;
    * a raster whose bands could not be read at all still goes through OCR;
    * an 8-bit RGB orthophoto still goes through OCR. That is a waste, but
      telling an aerial photo from a photographed map needs to look at the
      pixels, not the header, and a false skip loses a real map.

    The skipped raster is not lost: its `silver.raster_layers` row carries
    the CRS, bounds, band statistics and bit depth. It is not TEXT-
    retrievable, which is the gap [[project-master-plan-phase3]] tracks
    separately for all structured geological data.
    """
    if not result.crs:
        return False
    dtypes = {
        (getattr(b, "dtype", None) or "").lower()
        for b in (result.bands or [])
    }
    if not dtypes:
        return False
    return not (dtypes & _SCANNABLE_DTYPES)


_INSERT = """
INSERT INTO silver.raster_layers (
    project_id, workspace_id, layer_name, source_file, source_file_sha256,
    format, driver, width, height, band_count,
    crs, crs_confidence, pixel_size_x, pixel_size_y,
    bounds_native, compression, is_cog, has_alpha,
    band_stats, tags, warnings, bbox
)
VALUES (
    $1::uuid, $2::uuid, $3, $4, $5,
    $6, $7, $8, $9, $10,
    $11, $12, $13, $14,
    $15::jsonb, $16, $17, $18,
    $19::jsonb, $20::jsonb, $21::jsonb,
    CASE WHEN $22::double precision IS NULL THEN NULL
         ELSE ST_MakeEnvelope($22, $23, $24, $25, 4326)
    END
)
ON CONFLICT (project_id, source_file_sha256) WHERE project_id IS NOT NULL
DO NOTHING
RETURNING CAST(raster_id AS TEXT)
"""


def _layer_name(source_key: str) -> str:
    """Human-facing name for the layer: the uploaded file's stem.

    Upload keys are prefixed with a timestamp by UploadController
    (``20260820_155245_Geologic_Map_Unga_1982b_utm.tif``). Strip it so the
    catalogue reads as the geologist named the file, not as the uploader
    stored it. A stem that is *only* a timestamp keeps the whole thing
    rather than becoming empty.
    """
    stem = Path(source_key).stem or "raster"
    parts = stem.split("_", 2)
    if len(parts) == 3 and len(parts[0]) == 8 and parts[0].isdigit() and parts[1].isdigit():
        return parts[2] or stem
    return stem


def _extract(source_bytes: bytes, suffix: str) -> Any:
    """Run the shared raster parser over bytes held in memory.

    ``parse_raster_file`` takes a path because GDAL wants a file, so the
    bytes go to a temp file first. The workflow is already holding the whole
    object in memory, so this adds disk I/O but no new peak memory, and the
    file is removed before returning.
    """
    from georag_geoparsers.raster_parser import parse_raster_file  # noqa: PLC0415

    fd, tmp_path = tempfile.mkstemp(prefix="georag_raster_", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(source_bytes)
        return parse_raster_file(tmp_path)
    finally:
        with contextlib.suppress(OSError):  # best effort — the dir is temp anyway
            os.unlink(tmp_path)


async def persist_raster_metadata(
    *,
    source_bytes: bytes,
    source_key: str,
    source_sha256: str,
    project_id: str,
    workspace_id: str,
) -> RasterCaptureResult:
    """Read the raster header and record it, or explain why it was skipped.

    Never raises. Every failure path returns a ``RasterCaptureResult`` with
    ``written=False`` and a reason, because losing the coordinates is bad
    but failing the document ingest over it would be worse.

    ``source_sha256`` is passed in rather than recomputed: ``tiff_normalize``
    already hashes these bytes for its idempotency key, and the partial
    unique index on ``(project_id, source_file_sha256)`` is what makes this
    safe to re-run on a workflow retry.
    """
    try:
        result = _extract(source_bytes, Path(source_key).suffix or ".tif")
    except Exception as exc:  # noqa: BLE001 — a non-raster TIFF is normal here
        log.info(
            "raster_metadata: %s is not a readable raster (%s) — no row written",
            source_key, exc,
        )
        return RasterCaptureResult(written=False, reason="not_a_readable_raster")

    if not result.crs:
        # A scanned page, not a map. Nothing to preserve.
        log.info("raster_metadata: %s carries no CRS — no row written", source_key)
        return RasterCaptureResult(written=False, reason="no_crs")

    measurement = _is_measurement_raster(result)

    b4326 = result.bounds_4326
    # RasterBandStats' fields are `min`/`max`/`description` (raster_parser.py).
    # This read them as `minimum`/`maximum` and omitted `description`, so
    # every band's range was computed by the parser — which calls
    # `statistics(bidx)` per band to get it — and then dropped on the floor
    # by a getattr() whose default swallowed the typo. The frozen Dagster
    # writer this was ported from uses `min`/`max` as both the attribute
    # AND the JSON key (silver_raster.py:315-326), so the live rows also
    # disagreed with the shape anything reading this column would expect.
    band_stats = [
        {
            "band_index": b.band_index,
            "dtype": b.dtype,
            "min": b.min,
            "max": b.max,
            "mean": b.mean,
            "nodata": b.nodata,
            "description": b.description,
        }
        for b in (result.bands or [])
    ]

    try:
        from app.db import scoped_connection  # noqa: PLC0415
        from app.hatchet_workflows._progress import get_pool  # noqa: PLC0415

        pool = await get_pool()
        async with scoped_connection(
            pool,
            workspace_id=workspace_id,
            site="hatchet.tiff_normalize.raster_metadata",
        ) as conn:
            raster_id = await conn.fetchval(
                _INSERT,
                project_id,
                workspace_id,
                _layer_name(source_key),
                source_key,
                source_sha256,
                result.format,
                result.driver,
                result.width,
                result.height,
                result.band_count,
                result.crs,
                result.crs_confidence,
                result.pixel_size_x,
                result.pixel_size_y,
                json.dumps(list(result.bounds) if result.bounds else None),
                result.compression,
                result.is_cog,
                result.has_alpha,
                json.dumps(band_stats),
                json.dumps(result.tags or {}),
                json.dumps(result.warnings or []),
                b4326[0] if b4326 else None,
                b4326[1] if b4326 else None,
                b4326[2] if b4326 else None,
                b4326[3] if b4326 else None,
            )
    except Exception as exc:  # noqa: BLE001 — capture must never fail the ingest
        log.warning(
            "raster_metadata: persist failed for %s (%s) — the PDF ingest "
            "continues, but this raster's coordinates were not recorded",
            source_key, exc,
        )
        return RasterCaptureResult(
            written=False, reason=f"persist_failed: {exc}", crs=result.crs,
            is_measurement_raster=measurement,
        )

    if raster_id is None:
        # ON CONFLICT DO NOTHING — already captured on an earlier attempt.
        log.info(
            "raster_metadata: %s already recorded (sha %s) — skipped",
            source_key, source_sha256[:8],
        )
        return RasterCaptureResult(
            written=False, reason="already_recorded", crs=result.crs,
            is_measurement_raster=measurement,
        )

    log.info(
        "raster_metadata: recorded %s as %s crs=%s %dx%d bands=%d",
        source_key, raster_id, result.crs, result.width, result.height,
        result.band_count,
    )
    return RasterCaptureResult(
        written=True, reason="recorded", crs=result.crs, raster_id=raster_id,
        is_measurement_raster=measurement,
    )
