"""Raster file parser for GeoTIFF, NetCDF, ASCII Grid (.asc), and JPEG2000.

Uses rasterio to extract metadata, CRS, band statistics, and format details.
Band statistics are computed only for rasters with fewer than 25 million pixels
total; larger rasters emit a warning and continue with stats=None.

NetCDF files that expose multiple subdatasets are handled by selecting the first
subdataset and emitting a structured warning listing all available subdatasets.

ASCII Grid (.asc) files typically have no embedded CRS — a crs_unknown warning
is emitted and parsing continues.

CRS handling follows Section 04b:
  1. Parse from rasterio src.crs (contains EPSG or proj4 from file header).
  2. If no CRS: emit crs_unknown warning.
  3. If CRS has no EPSG code: emit crs_not_epsg warning.
  4. Reproject bounds to EPSG:4326 for the bounds_4326 field; on failure emit
     reprojection_failed warning and set bounds_4326 = None.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.

# TODO: Sprint 4b: persist RasterParseResult to silver.raster_layers once the
# storage schema decision is made (Section 04e does not yet include raster_layers).
"""

import hashlib
import logging
import warnings as _warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PARSER_VERSION = "1.1.0"

# Pixel count threshold above which band statistics are skipped.
_STATS_PIXEL_LIMIT = 25_000_000

# ---------------------------------------------------------------------------
# Driver → human-friendly format name mapping
# ---------------------------------------------------------------------------

_DRIVER_TO_FORMAT: dict[str, str] = {
    "GTiff":       "GeoTIFF",
    "COG":         "Cloud-Optimized GeoTIFF",
    "netCDF":      "NetCDF",
    "HDF5":        "HDF5",
    "HDF4":        "HDF4",
    "AAIGrid":     "ASCII Grid",
    "JP2OpenJPEG": "JPEG2000",
    "JP2ECW":      "JPEG2000",
    "JP2KAK":      "JPEG2000",
}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RasterBandStats:
    """Statistics for a single raster band (1-based index)."""

    band_index: int          # 1-based band index
    dtype: str               # e.g. "float32", "uint16"
    nodata: float | None
    min: float | None        # None if raster too large to summarise
    max: float | None
    mean: float | None
    description: str | None  # band description from rasterio metadata


@dataclass
class RasterParseResult:
    """Top-level result returned by parse_raster_file."""

    driver: str               # raw GDAL/rasterio driver string: "GTiff", "netCDF", "AAIGrid", etc.
    format: str               # human-friendly name derived from driver: "GeoTIFF", "NetCDF", etc.
    width: int                # pixels
    height: int               # pixels
    band_count: int
    crs: str | None           # "EPSG:32613" or proj4 string if no EPSG; None if absent
    crs_confidence: float     # 0–1 heuristic matching spatial_parser style
    pixel_size_x: float       # map units per pixel (absolute value)
    pixel_size_y: float       # map units per pixel (absolute value)
    bounds: tuple[float, float, float, float]           # (minx, miny, maxx, maxy) in CRS units
    bounds_4326: tuple[float, float, float, float] | None  # reprojected to WGS84; None if CRS absent
    bands: list[RasterBandStats]
    is_cog: bool              # cloud-optimized GeoTIFF: tiled=True AND has overviews
    has_alpha: bool           # True if any band has ColorInterp.alpha
    compression: str | None   # e.g. "lzw", "deflate"; None if uncompressed
    tags: dict[str, str]      # dataset-level tags from rasterio
    warnings: list[dict]
    provenance: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sha256_file(path: str) -> str:
    """Compute SHA-256 hex digest of the file at *path*."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_directory(path: str) -> str:
    """Deterministic SHA-256 of a directory by hashing '{name}:{size}' for each file.

    Used for GDB directories where the 'file' is actually a folder.
    Files are sorted for determinism.
    """
    h = hashlib.sha256()
    root = Path(path)
    for child in sorted(root.rglob("*")):
        if child.is_file():
            entry = f"{child.name}:{child.stat().st_size}"
            h.update(entry.encode())
    return h.hexdigest()


def _crs_to_string(crs) -> str | None:
    """Convert a rasterio CRS object to a canonical string.

    Returns "EPSG:XXXX" if an EPSG code is available, otherwise the proj4
    string. Returns None only if *crs* is None.
    """
    if crs is None:
        return None
    epsg = crs.to_epsg()
    if epsg is not None:
        return f"EPSG:{epsg}"
    return crs.to_string()


def _score_crs_confidence(crs, bounds_4326) -> float:
    """Heuristic CRS confidence score (0–1) mirroring spatial_parser style.

    Uses the CRS area_of_use (from PyProj) and compares against the raster's
    WGS84 bounds. Returns:
      1.0  — bounds fully inside CRS area of use
      0.5  — partial overlap or CRS has no area_of_use
      0.0  — CRS is None, or bounds are fully outside area of use
    """
    if crs is None or bounds_4326 is None:
        return 0.0
    try:
        from pyproj import CRS as ProjCRS  # noqa: PLC0415
        crs_obj = ProjCRS.from_user_input(crs)
        area = crs_obj.area_of_use
        if area is None:
            return 0.5

        data_west, data_south, data_east, data_north = bounds_4326

        # Fully inside
        if (
            data_west >= area.west
            and data_east <= area.east
            and data_south >= area.south
            and data_north <= area.north
        ):
            return 1.0
        # Fully outside
        if (
            data_east < area.west
            or data_west > area.east
            or data_north < area.south
            or data_south > area.north
        ):
            return 0.0
        # Partial overlap
        return 0.5
    except Exception as exc:
        logger.debug("raster_parser: CRS confidence scoring failed: %s", exc)
        return 0.5


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_raster_file(path: str | Path) -> RasterParseResult:
    """Parse a raster file (GeoTIFF, NetCDF, ASCII Grid, JPEG2000) using rasterio.

    Computes band statistics for small rasters (<25 Mpx total) and skips them
    for larger files (emits a ``raster_too_large_for_stats`` warning in that
    case — parse still succeeds).

    NetCDF files with multiple variables: the parser opens the first subdataset
    and warns with ``netcdf_multiple_subdatasets``.

    Args:
        path: Absolute path to the raster file.

    Returns:
        RasterParseResult with metadata, band stats, CRS info, and any warnings.

    Raises:
        rasterio.errors.RasterioIOError: if the file is not a readable raster.
        FileNotFoundError: if the path does not exist.
    """
    import rasterio  # noqa: PLC0415 — deferred; avoids cost in non-raster envs
    import rasterio.enums  # noqa: PLC0415
    import rasterio.errors  # noqa: PLC0415
    from rasterio.warp import transform_bounds  # noqa: PLC0415

    path = str(path)

    if not Path(path).exists():
        raise FileNotFoundError(f"raster_parser: file not found at '{path}'")

    # Provenance hash — computed before opening rasterio (handles large files
    # with streaming hash so the handle is not left open).
    sha256_hex = _sha256_file(path)
    provenance: dict[str, Any] = {
        "source_file": path,
        "source_file_sha256": sha256_hex,
        "parser_name": "raster_parser",
        "parser_version": PARSER_VERSION,
    }

    parse_warnings: list[dict] = []

    # Suppress rasterio's own deprecation warnings from our internal calls.
    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore", rasterio.errors.RasterioDeprecationWarning)
        _warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        result = _parse_raster_inner(
            path, provenance, parse_warnings, rasterio, rasterio.enums,
            rasterio.errors, transform_bounds
        )

    return result


def _parse_raster_inner(
    path: str,
    provenance: dict,
    parse_warnings: list,
    rasterio,
    rasterio_enums,
    rasterio_errors,
    transform_bounds,
) -> RasterParseResult:
    """Core parsing logic — called by parse_raster_file inside warning filters."""

    # First open: detect subdatasets (needed for NetCDF multi-variable files).
    with rasterio.open(path) as probe:
        subdatasets = list(probe.subdatasets or [])

    if subdatasets:
        # NetCDF (and HDF) expose variables as subdatasets; pick the first one.
        selected = subdatasets[0]
        parse_warnings.append({
            "code": "netcdf_multiple_subdatasets",
            "message": (
                f"File has {len(subdatasets)} subdataset(s); "
                f"selected the first for parsing."
            ),
            "context": {
                "subdatasets": subdatasets,
                "selected": selected,
            },
        })
        logger.warning(
            "raster_parser: '%s' has %d subdataset(s) — using '%s'",
            path, len(subdatasets), selected,
        )
        effective_path = selected
    else:
        effective_path = path

    with rasterio.open(effective_path) as src:
        driver_name: str = src.driver or "unknown"
        format_name: str = _DRIVER_TO_FORMAT.get(driver_name, driver_name)
        width: int = src.width
        height: int = src.height
        band_count: int = src.count
        profile: dict = src.profile

        # CRS handling (Section 04b)
        raw_crs = src.crs
        crs_str: str | None
        if raw_crs is None:
            crs_str = None
            parse_warnings.append({
                "code": "crs_unknown",
                "message": f"'{path}' has no CRS defined in file metadata.",
                "context": {"path": path},
            })
            logger.warning("raster_parser: '%s' has no CRS defined.", path)
        else:
            epsg = raw_crs.to_epsg()
            if epsg is not None:
                crs_str = f"EPSG:{epsg}"
            else:
                crs_str = raw_crs.to_string()
                parse_warnings.append({
                    "code": "crs_not_epsg",
                    "message": (
                        f"CRS has no EPSG code; stored as proj4/WKT: {crs_str!r}"
                    ),
                    "context": {"crs_string": crs_str},
                })
                logger.warning(
                    "raster_parser: '%s' CRS has no EPSG code — using proj4: %s",
                    path, crs_str,
                )

        # Pixel size from affine transform
        transform = src.transform
        pixel_size_x: float = abs(float(transform.a))
        pixel_size_y: float = abs(float(transform.e))

        # Bounds in native CRS units
        b = src.bounds
        bounds_native: tuple[float, float, float, float] = (
            float(b.left), float(b.bottom), float(b.right), float(b.top)
        )

        # Bounds reprojected to WGS84
        bounds_4326: tuple[float, float, float, float] | None = None
        if raw_crs is not None:
            try:
                west, south, east, north = transform_bounds(
                    raw_crs, "EPSG:4326",
                    b.left, b.bottom, b.right, b.top,
                )
                bounds_4326 = (float(west), float(south), float(east), float(north))
            except Exception as exc:
                parse_warnings.append({
                    "code": "reprojection_failed",
                    "message": "Could not reproject bounds to EPSG:4326.",
                    "context": {"error": str(exc)},
                })
                logger.warning(
                    "raster_parser: bounds reprojection failed for '%s': %s", path, exc
                )

        # CRS confidence
        crs_confidence: float = _score_crs_confidence(raw_crs, bounds_4326)

        # COG detection: tiled=True AND band 1 has at least one overview level
        is_cog: bool = bool(profile.get("tiled")) and len(src.overviews(1)) > 0

        # Alpha band detection via ColorInterp
        has_alpha: bool = any(
            ci == rasterio_enums.ColorInterp.alpha
            for ci in src.colorinterp
        )

        # Compression from profile (key is 'compress', not 'compression')
        compression: str | None = profile.get("compress") or None

        # Dataset-level tags
        tags: dict[str, str] = {k: str(v) for k, v in (src.tags() or {}).items()}

        # Band statistics
        total_pixels = width * height
        compute_stats = total_pixels < _STATS_PIXEL_LIMIT
        if not compute_stats:
            parse_warnings.append({
                "code": "raster_too_large_for_stats",
                "message": (
                    f"Raster has {total_pixels:,} pixels (limit {_STATS_PIXEL_LIMIT:,}); "
                    f"band statistics skipped."
                ),
                "context": {"width": width, "height": height, "total_pixels": total_pixels},
            })
            logger.info(
                "raster_parser: '%s' too large for stats (%d px) — skipping band stats",
                path, total_pixels,
            )

        bands: list[RasterBandStats] = []
        for bidx in range(1, band_count + 1):
            dtype_str: str = src.dtypes[bidx - 1]
            nodata_val = src.nodatavals[bidx - 1]
            if nodata_val is not None:
                try:
                    nodata_val = float(nodata_val)
                except (TypeError, ValueError):
                    nodata_val = None

            description: str | None = src.descriptions[bidx - 1]

            # Skip statistics for alpha band
            is_alpha = src.colorinterp[bidx - 1] == rasterio_enums.ColorInterp.alpha

            band_min = band_max = band_mean = None
            if compute_stats and not is_alpha:
                try:
                    # statistics(bidx) is deprecated in rasterio 2.x but is the
                    # correct per-band API in rasterio 1.x. Use it here with the
                    # deprecation warning suppressed by the outer context manager.
                    stat = src.statistics(bidx)
                    band_min = float(stat.min) if stat.min is not None else None
                    band_max = float(stat.max) if stat.max is not None else None
                    band_mean = float(stat.mean) if stat.mean is not None else None
                except Exception as exc:
                    logger.debug(
                        "raster_parser: band %d statistics failed for '%s': %s",
                        bidx, path, exc,
                    )

            bands.append(RasterBandStats(
                band_index=bidx,
                dtype=dtype_str,
                nodata=nodata_val,
                min=band_min,
                max=band_max,
                mean=band_mean,
                description=description,
            ))

    logger.info(
        "raster_parser: '%s' — driver=%s format=%s size=%dx%d bands=%d crs=%s is_cog=%s",
        path, driver_name, format_name, width, height, band_count, crs_str, is_cog,
    )

    return RasterParseResult(
        driver=driver_name,
        format=format_name,
        width=width,
        height=height,
        band_count=band_count,
        crs=crs_str,
        crs_confidence=crs_confidence,
        pixel_size_x=pixel_size_x,
        pixel_size_y=pixel_size_y,
        bounds=bounds_native,
        bounds_4326=bounds_4326,
        bands=bands,
        is_cog=is_cog,
        has_alpha=has_alpha,
        compression=compression,
        tags=tags,
        warnings=parse_warnings,
        provenance=provenance,
    )
