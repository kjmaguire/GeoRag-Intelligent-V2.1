"""Georeferencing survives the trip through tiff_normalize.

ADR-0005 wraps every uploaded TIFF to PDF for OCR. That wrap keeps the
pixels and drops the CRS, the geotransform and the bounds, so a scanned
geological map used to land as a picture with no idea where it is. These
tests pin the capture that runs first.

Two properties matter more than the happy path:

  * it never fails the ingest — a raster this cannot read, a database that
    is down, a re-run of the same workflow: all return a reason, none
    raise; and
  * it does not catalogue document scans. A report page is a TIFF too, and
    writing a "raster layer" row for each one would bury the handful of
    real map sheets under thousands of pages.
"""
from __future__ import annotations

import sys
import types

import pytest

from app.services.ingest.raster_metadata import (
    RasterCaptureResult,
    _layer_name,
    persist_raster_metadata,
)


class _FakeResult:
    """Stand-in for RasterParseResult with only the fields used here."""

    def __init__(self, crs="EPSG:32605", bounds_4326=(-160.5, 55.1, -160.1, 55.4)):
        self.driver = "GTiff"
        self.format = "GeoTIFF"
        self.width = 15000
        self.height = 10000
        self.band_count = 3
        self.crs = crs
        self.crs_confidence = 0.92
        self.pixel_size_x = 2.5
        self.pixel_size_y = 2.5
        self.bounds = (500000.0, 6100000.0, 537500.0, 6125000.0)
        self.bounds_4326 = bounds_4326
        self.bands = []
        self.is_cog = False
        self.has_alpha = False
        self.compression = "lzw"
        self.tags = {}
        self.warnings = []


@pytest.fixture
def args():
    return {
        "source_bytes": b"II*\x00fake-tiff",
        "source_key": "tiff/proj/20260820_155245_Geologic_Map_Unga_1982b_utm.tif",
        "source_sha256": "a" * 64,
        "project_id": "01a01fdc-a401-7015-8c46-b8ceb55aeb62",
        "workspace_id": "a0000000-0000-0000-0000-000000000001",
    }


def _patch_extract(monkeypatch, value=None, exc=None):
    def fake(_bytes, _suffix):
        if exc is not None:
            raise exc
        return value
    monkeypatch.setattr(
        "app.services.ingest.raster_metadata._extract", fake,
    )


def _patch_db(monkeypatch, *, returns="11111111-2222-3333-4444-555555555555", raises=None):
    """Replace the pool + scoped_connection the function imports lazily."""
    captured: dict = {}

    class _Conn:
        async def fetchval(self, _sql, *params):
            captured["params"] = params
            if raises is not None:
                raise raises
            return returns

    class _Scoped:
        def __init__(self, *a, **kw):
            captured["kwargs"] = kw

        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    async def _get_pool():
        return object()

    db_mod = types.ModuleType("app.db")
    db_mod.scoped_connection = _Scoped
    monkeypatch.setitem(sys.modules, "app.db", db_mod)

    prog_mod = types.ModuleType("app.hatchet_workflows._progress")
    prog_mod.get_pool = _get_pool
    monkeypatch.setitem(sys.modules, "app.hatchet_workflows._progress", prog_mod)

    return captured


class TestSkips:
    @pytest.mark.asyncio
    async def test_a_page_scan_with_no_crs_is_not_catalogued(self, monkeypatch, args):
        """The common case. Most TIFFs here are report pages, not maps."""
        _patch_extract(monkeypatch, value=_FakeResult(crs=None))
        out = await persist_raster_metadata(**args)
        assert out.written is False
        assert out.reason == "no_crs"

    @pytest.mark.asyncio
    async def test_an_unreadable_raster_does_not_raise(self, monkeypatch, args):
        _patch_extract(monkeypatch, exc=OSError("not a raster"))
        out = await persist_raster_metadata(**args)
        assert out.written is False
        assert out.reason == "not_a_readable_raster"

    @pytest.mark.asyncio
    async def test_a_database_failure_does_not_fail_the_ingest(self, monkeypatch, args):
        """The document still has to ingest. Losing the coordinates is bad;
        losing the document over the coordinates is worse."""
        _patch_extract(monkeypatch, value=_FakeResult())
        _patch_db(monkeypatch, raises=RuntimeError("connection refused"))
        out = await persist_raster_metadata(**args)
        assert out.written is False
        assert out.reason.startswith("persist_failed")
        assert out.crs == "EPSG:32605"

    @pytest.mark.asyncio
    async def test_a_workflow_retry_does_not_duplicate(self, monkeypatch, args):
        """ON CONFLICT DO NOTHING returns no row; that is success, not error."""
        _patch_extract(monkeypatch, value=_FakeResult())
        _patch_db(monkeypatch, returns=None)
        out = await persist_raster_metadata(**args)
        assert out.written is False
        assert out.reason == "already_recorded"


class TestCapture:
    @pytest.mark.asyncio
    async def test_a_georeferenced_map_is_recorded(self, monkeypatch, args):
        _patch_extract(monkeypatch, value=_FakeResult())
        captured = _patch_db(monkeypatch)
        out = await persist_raster_metadata(**args)

        assert out.written is True
        assert out.crs == "EPSG:32605"
        assert out.raster_id == "11111111-2222-3333-4444-555555555555"

        p = captured["params"]
        assert p[0] == args["project_id"]
        assert p[1] == args["workspace_id"]
        assert p[4] == args["source_sha256"], "sha is reused, not recomputed"
        assert p[10] == "EPSG:32605"
        # Trailing four are the WGS84 envelope corners for bbox.
        assert p[-4:] == (-160.5, 55.1, -160.1, 55.4)

    @pytest.mark.asyncio
    async def test_the_row_is_written_under_the_workspace_scope(self, monkeypatch, args):
        """RLS is enabled on silver.raster_layers. An insert that does not
        bind app.workspace_id is either rejected or, worse, invisible."""
        _patch_extract(monkeypatch, value=_FakeResult())
        captured = _patch_db(monkeypatch)
        await persist_raster_metadata(**args)
        assert captured["kwargs"]["workspace_id"] == args["workspace_id"]

    @pytest.mark.asyncio
    async def test_a_raster_with_no_reprojectable_bounds_still_records(
        self, monkeypatch, args
    ):
        """bbox is nullable; a CRS pyproj cannot reproject is still worth
        keeping, because the native bounds and the CRS string survive."""
        _patch_extract(monkeypatch, value=_FakeResult(bounds_4326=None))
        captured = _patch_db(monkeypatch)
        out = await persist_raster_metadata(**args)
        assert out.written is True
        assert captured["params"][-4:] == (None, None, None, None)


class TestLayerName:
    def test_the_uploader_timestamp_is_stripped(self):
        assert _layer_name(
            "tiff/p/20260820_155245_Geologic_Map_Unga_1982b_utm.tif"
        ) == "Geologic_Map_Unga_1982b_utm"

    def test_a_name_without_a_timestamp_is_left_alone(self):
        assert _layer_name("tiff/p/orange_mtn_geology.tif") == "orange_mtn_geology"

    def test_a_name_that_is_only_a_timestamp_is_kept_whole(self):
        assert _layer_name("tiff/p/20260820_155245.tif") == "20260820_155245"

    def test_a_name_whose_own_words_look_numeric_is_not_truncated(self):
        # "1982_04_map" must not be mistaken for an uploader prefix: the
        # first segment is not 8 digits.
        assert _layer_name("tiff/p/1982_04_map.tif") == "1982_04_map"


def test_result_repr_is_readable():
    r = RasterCaptureResult(written=True, reason="recorded", crs="EPSG:4326")
    assert "recorded" in repr(r)
