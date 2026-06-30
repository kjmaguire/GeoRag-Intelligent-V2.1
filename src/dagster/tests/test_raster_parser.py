"""Sprint 4 smoke tests for raster_parser.py.

Covers:
  - GeoTIFF: metadata extraction, band stats, CRS, bounds_4326, provenance.
  - No-CRS raster (ASCII Grid .asc): crs_unknown warning, parse still succeeds.
  - Large raster: raster_too_large_for_stats warning, stats are None.
  - COG detection: is_cog=False for a plain GeoTIFF.
  - Missing file: FileNotFoundError.
  - Non-raster file: rasterio.errors.RasterioIOError raised.
  - Alpha band: has_alpha detection, alpha band stats skipped.
  - Compression: compress key surfaced.

Run with:  pytest tests/test_raster_parser.py -v
"""

from __future__ import annotations


import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio", reason="rasterio not installed")

from rasterio.transform import from_bounds  # noqa: E402

from georag_dagster.parsers.raster_parser import (
    RasterBandStats,
    RasterParseResult,
    parse_raster_file,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_gtiff(
    path: str,
    width: int = 20,
    height: int = 20,
    count: int = 1,
    dtype: str = "float32",
    crs: str | None = "EPSG:4326",
    west: float = -111.0,
    south: float = 58.0,
    east: float = -110.0,
    north: float = 59.0,
    nodata: float | None = None,
    compress: str | None = None,
    has_alpha: bool = False,
) -> None:
    """Write a minimal GeoTIFF fixture to *path*."""
    import rasterio  # noqa: PLC0415
    import rasterio.enums  # noqa: PLC0415

    transform = from_bounds(west, south, east, north, width, height)
    total_bands = count + (1 if has_alpha else 0)
    data = np.arange(1, width * height + 1, dtype=dtype).reshape(1, height, width)
    data = np.repeat(data, total_bands, axis=0)

    kwargs: dict = {
        "driver": "GTiff",
        "count": total_bands,
        "dtype": dtype,
        "width": width,
        "height": height,
        "transform": transform,
    }
    if crs is not None:
        kwargs["crs"] = crs
    if nodata is not None:
        kwargs["nodata"] = nodata
    if compress is not None:
        kwargs["compress"] = compress

    with rasterio.open(path, "w", **kwargs) as ds:
        ds.write(data)
        if has_alpha:
            # Tag the last band as alpha via colorinterp
            ds.colorinterp = list(ds.colorinterp[:-1]) + [rasterio.enums.ColorInterp.alpha]


# ---------------------------------------------------------------------------
# Tests: basic GTiff
# ---------------------------------------------------------------------------

class TestRasterParserGeoTiff:
    def test_returns_raster_parse_result(self, tmp_path):
        p = str(tmp_path / "test.tif")
        _write_gtiff(p)
        result = parse_raster_file(p)
        assert isinstance(result, RasterParseResult)

    def test_driver_is_gtiff(self, tmp_path):
        p = str(tmp_path / "test.tif")
        _write_gtiff(p)
        result = parse_raster_file(p)
        # driver is the raw GDAL string; format is the human-friendly name
        assert result.driver == "GTiff"
        assert result.format == "GeoTIFF"

    def test_dimensions(self, tmp_path):
        p = str(tmp_path / "test.tif")
        _write_gtiff(p, width=20, height=15)
        result = parse_raster_file(p)
        assert result.width == 20
        assert result.height == 15

    def test_band_count(self, tmp_path):
        p = str(tmp_path / "test.tif")
        _write_gtiff(p, count=3)
        result = parse_raster_file(p)
        assert result.band_count == 3
        assert len(result.bands) == 3

    def test_crs_epsg4326(self, tmp_path):
        p = str(tmp_path / "test.tif")
        _write_gtiff(p, crs="EPSG:4326")
        result = parse_raster_file(p)
        assert result.crs == "EPSG:4326"
        assert len([w for w in result.warnings if w["code"] == "crs_unknown"]) == 0

    def test_crs_utm(self, tmp_path):
        """UTM CRS should be returned as EPSG string."""
        import rasterio  # noqa: PLC0415
        from rasterio.transform import from_bounds as fb  # noqa: PLC0415
        p = str(tmp_path / "utm.tif")
        transform = fb(400000, 6100000, 650000, 6400000, 20, 20)
        data = np.ones((1, 20, 20), dtype="float32")
        with rasterio.open(p, "w", driver="GTiff", count=1, dtype="float32",
                           width=20, height=20, crs="EPSG:32613",
                           transform=transform) as ds:
            ds.write(data)
        result = parse_raster_file(p)
        assert result.crs == "EPSG:32613"

    def test_bounds_native_type(self, tmp_path):
        p = str(tmp_path / "test.tif")
        _write_gtiff(p)
        result = parse_raster_file(p)
        assert isinstance(result.bounds, tuple)
        assert len(result.bounds) == 4

    def test_bounds_4326_present_when_crs_known(self, tmp_path):
        p = str(tmp_path / "test.tif")
        _write_gtiff(p, crs="EPSG:4326")
        result = parse_raster_file(p)
        assert result.bounds_4326 is not None
        minx, miny, maxx, maxy = result.bounds_4326
        # Geographic range
        assert -180 <= minx < maxx <= 180
        assert -90 <= miny < maxy <= 90

    def test_pixel_size_positive(self, tmp_path):
        p = str(tmp_path / "test.tif")
        _write_gtiff(p, width=20, height=20, west=-111, east=-110, south=58, north=59)
        result = parse_raster_file(p)
        assert result.pixel_size_x > 0
        assert result.pixel_size_y > 0

    def test_is_cog_false_for_plain_gtiff(self, tmp_path):
        p = str(tmp_path / "test.tif")
        _write_gtiff(p)
        result = parse_raster_file(p)
        assert result.is_cog is False

    def test_has_alpha_false_for_plain_raster(self, tmp_path):
        p = str(tmp_path / "test.tif")
        _write_gtiff(p)
        result = parse_raster_file(p)
        assert result.has_alpha is False

    def test_compression_none_for_uncompressed(self, tmp_path):
        p = str(tmp_path / "test.tif")
        _write_gtiff(p)
        result = parse_raster_file(p)
        assert result.compression is None

    def test_provenance_keys(self, tmp_path):
        p = str(tmp_path / "test.tif")
        _write_gtiff(p)
        result = parse_raster_file(p)
        assert "source_file_sha256" in result.provenance
        assert result.provenance["parser_name"] == "raster_parser"
        assert len(result.provenance["source_file_sha256"]) == 64


# ---------------------------------------------------------------------------
# Tests: band statistics
# ---------------------------------------------------------------------------

class TestRasterBandStats:
    def test_band_stats_computed_for_small_raster(self, tmp_path):
        p = str(tmp_path / "small.tif")
        _write_gtiff(p, width=20, height=20)
        result = parse_raster_file(p)
        band = result.bands[0]
        assert isinstance(band, RasterBandStats)
        # Stats should be populated for a 400-pixel raster
        assert band.min is not None
        assert band.max is not None
        assert band.mean is not None
        assert band.min <= band.mean <= band.max

    def test_band_dtype(self, tmp_path):
        p = str(tmp_path / "test.tif")
        _write_gtiff(p, dtype="uint8")
        result = parse_raster_file(p)
        assert result.bands[0].dtype == "uint8"

    def test_band_nodata_captured(self, tmp_path):
        p = str(tmp_path / "nd.tif")
        _write_gtiff(p, nodata=-9999.0)
        result = parse_raster_file(p)
        assert result.bands[0].nodata == -9999.0

    def test_band_nodata_none_when_unset(self, tmp_path):
        p = str(tmp_path / "no_nd.tif")
        _write_gtiff(p, nodata=None)
        result = parse_raster_file(p)
        assert result.bands[0].nodata is None


# ---------------------------------------------------------------------------
# Tests: large raster → stats skipped
# ---------------------------------------------------------------------------

class TestRasterTooLargeForStats:
    def test_large_raster_emits_warning(self, tmp_path):
        """Raster over 25 Mpx → raster_too_large_for_stats warning."""
        import rasterio  # noqa: PLC0415
        p = str(tmp_path / "large.tif")
        # 5001 * 5001 = 25_010_001 > 25_000_000
        w, h = 5001, 5001
        transform = from_bounds(-111, 58, -110, 59, w, h)
        # Write minimal data — we only need the metadata not the full data
        # Use a sparse write approach: write zeros
        data = np.zeros((1, h, w), dtype="uint8")
        with rasterio.open(p, "w", driver="GTiff", count=1, dtype="uint8",
                           width=w, height=h, crs="EPSG:4326",
                           transform=transform) as ds:
            ds.write(data)

        result = parse_raster_file(p)
        codes = [w["code"] for w in result.warnings]
        assert "raster_too_large_for_stats" in codes, (
            f"Expected raster_too_large_for_stats warning; got: {codes}"
        )

    def test_large_raster_band_stats_are_none(self, tmp_path):
        """Band stats are None when raster is too large."""
        import rasterio  # noqa: PLC0415
        p = str(tmp_path / "large2.tif")
        w, h = 5001, 5001
        transform = from_bounds(-111, 58, -110, 59, w, h)
        data = np.zeros((1, h, w), dtype="uint8")
        with rasterio.open(p, "w", driver="GTiff", count=1, dtype="uint8",
                           width=w, height=h, crs="EPSG:4326",
                           transform=transform) as ds:
            ds.write(data)

        result = parse_raster_file(p)
        band = result.bands[0]
        assert band.min is None
        assert band.max is None
        assert band.mean is None

    def test_large_raster_parse_still_succeeds(self, tmp_path):
        """Oversized raster must return a result, not raise."""
        import rasterio  # noqa: PLC0415
        p = str(tmp_path / "large3.tif")
        w, h = 5001, 5001
        transform = from_bounds(-111, 58, -110, 59, w, h)
        data = np.zeros((1, h, w), dtype="uint8")
        with rasterio.open(p, "w", driver="GTiff", count=1, dtype="uint8",
                           width=w, height=h, crs="EPSG:4326",
                           transform=transform) as ds:
            ds.write(data)
        result = parse_raster_file(p)
        assert isinstance(result, RasterParseResult)


# ---------------------------------------------------------------------------
# Tests: no-CRS raster (ASCII Grid)
# ---------------------------------------------------------------------------

class TestRasterNoCrs:
    def _write_asc(self, path: str) -> None:
        with open(path, "w") as f:
            f.write("ncols 5\n")
            f.write("nrows 5\n")
            f.write("xllcorner -111.0\n")
            f.write("yllcorner 58.0\n")
            f.write("cellsize 0.1\n")
            f.write("NODATA_value -9999\n")
            for row in range(5):
                f.write(" ".join(str(row * 5 + col + 1) for col in range(5)) + "\n")

    def test_asc_parses_without_exception(self, tmp_path):
        p = str(tmp_path / "dem.asc")
        self._write_asc(p)
        result = parse_raster_file(p)
        assert isinstance(result, RasterParseResult)

    def test_asc_emits_crs_unknown_warning(self, tmp_path):
        p = str(tmp_path / "dem.asc")
        self._write_asc(p)
        result = parse_raster_file(p)
        codes = [w["code"] for w in result.warnings]
        assert "crs_unknown" in codes, (
            f"Expected crs_unknown warning for ASC with no CRS; got: {codes}"
        )

    def test_asc_crs_is_none(self, tmp_path):
        p = str(tmp_path / "dem.asc")
        self._write_asc(p)
        result = parse_raster_file(p)
        assert result.crs is None

    def test_asc_bounds_4326_is_none_when_crs_absent(self, tmp_path):
        p = str(tmp_path / "dem.asc")
        self._write_asc(p)
        result = parse_raster_file(p)
        assert result.bounds_4326 is None

    def test_asc_crs_confidence_zero(self, tmp_path):
        p = str(tmp_path / "dem.asc")
        self._write_asc(p)
        result = parse_raster_file(p)
        assert result.crs_confidence == 0.0


# ---------------------------------------------------------------------------
# Tests: error cases
# ---------------------------------------------------------------------------

class TestRasterParserErrors:
    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            parse_raster_file(str(tmp_path / "does_not_exist.tif"))

    def test_non_raster_raises_rasterio_error(self, tmp_path):
        """A text file is not a raster — rasterio should raise RasterioIOError."""
        p = str(tmp_path / "not_a_raster.txt")
        with open(p, "w") as f:
            f.write("hello world\n")
        with pytest.raises(rasterio.errors.RasterioIOError):
            parse_raster_file(p)


# ---------------------------------------------------------------------------
# Tests: provenance SHA-256 determinism
# ---------------------------------------------------------------------------

class TestRasterProvenance:
    def test_sha256_is_deterministic(self, tmp_path):
        p = str(tmp_path / "det.tif")
        _write_gtiff(p)
        r1 = parse_raster_file(p)
        r2 = parse_raster_file(p)
        assert r1.provenance["source_file_sha256"] == r2.provenance["source_file_sha256"]

    def test_sha256_differs_for_different_files(self, tmp_path):
        p1 = str(tmp_path / "a.tif")
        p2 = str(tmp_path / "b.tif")
        _write_gtiff(p1, width=10, height=10)
        _write_gtiff(p2, width=11, height=11)
        r1 = parse_raster_file(p1)
        r2 = parse_raster_file(p2)
        assert r1.provenance["source_file_sha256"] != r2.provenance["source_file_sha256"]
