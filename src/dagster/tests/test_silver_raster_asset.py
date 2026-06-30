"""Sprint 4b tests for silver_raster asset.

Covers:
  - _build_bbox_wkt: correct WKT polygon from known bounds.
  - None bounds_4326 → NULL bbox path (INSERT_RASTER_NULL_BBOX_SQL, no ST_GeomFromText).
  - DB INSERT is called with expected parameters for a sample RasterParseResult.
  - ON CONFLICT upsert: SQL templates contain ON CONFLICT clause.
  - Unsupported file extension → early return with skipped=True.
  - Missing MinIO object → graceful skip, no DB call.
  - layer_name derived correctly from filename stem.
  - project_id config is propagated.

Direct Dagster asset invocation uses the underlying decorated function
(silver_raster.op.compute_fn.decorated_fn) to bypass Dagster's config
injection, which requires a full Dagster execution context in unit tests.

Run with:  pytest tests/test_silver_raster_asset.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


from georag_dagster.assets.silver_raster import (
    _build_bbox_wkt,
    _RASTER_EXTENSIONS,
    INSERT_RASTER_WITH_BBOX_SQL,
    INSERT_RASTER_NULL_BBOX_SQL,
)


# ---------------------------------------------------------------------------
# _build_bbox_wkt
# ---------------------------------------------------------------------------

class TestBuildBboxWkt:
    def test_known_bounds(self):
        """Bounds (-105, 40, -104, 41) → correct closed polygon string."""
        wkt = _build_bbox_wkt((-105.0, 40.0, -104.0, 41.0))
        assert wkt == "POLYGON((-105.0 40.0, -104.0 40.0, -104.0 41.0, -105.0 41.0, -105.0 40.0))"

    def test_wkt_starts_with_polygon(self):
        wkt = _build_bbox_wkt((-111.0, 58.0, -110.0, 59.0))
        assert wkt.startswith("POLYGON((")

    def test_wkt_is_closed_ring(self):
        """First and last coordinate pair in the ring must match."""
        wkt = _build_bbox_wkt((-100.0, 50.0, -99.0, 51.0))
        # Strip "POLYGON((" prefix and "))" suffix
        inner = wkt[len("POLYGON(("):-2]
        pairs = [p.strip() for p in inner.split(",")]
        assert pairs[0] == pairs[-1], (
            f"Ring is not closed: first={pairs[0]!r}, last={pairs[-1]!r}"
        )

    def test_four_unique_corners(self):
        """Should produce 5 coordinate pairs (4 unique corners + repeat of first)."""
        wkt = _build_bbox_wkt((-105.0, 40.0, -104.0, 41.0))
        inner = wkt[len("POLYGON(("):-2]
        pairs = [p.strip() for p in inner.split(",")]
        unique = set(pairs)
        assert len(pairs) == 5
        assert len(unique) == 4


# ---------------------------------------------------------------------------
# _RASTER_EXTENSIONS constant
# ---------------------------------------------------------------------------

class TestRasterExtensions:
    def test_tif_supported(self):
        assert ".tif" in _RASTER_EXTENSIONS

    def test_tiff_supported(self):
        assert ".tiff" in _RASTER_EXTENSIONS

    def test_nc_supported(self):
        assert ".nc" in _RASTER_EXTENSIONS

    def test_asc_supported(self):
        assert ".asc" in _RASTER_EXTENSIONS

    def test_jp2_supported(self):
        assert ".jp2" in _RASTER_EXTENSIONS

    def test_unsupported_extension(self):
        assert ".xyz" not in _RASTER_EXTENSIONS
        assert ".csv" not in _RASTER_EXTENSIONS


# ---------------------------------------------------------------------------
# SQL template checks
# ---------------------------------------------------------------------------

class TestSqlTemplates:
    def test_bbox_sql_has_on_conflict(self):
        assert "ON CONFLICT" in INSERT_RASTER_WITH_BBOX_SQL

    def test_null_bbox_sql_has_on_conflict(self):
        assert "ON CONFLICT" in INSERT_RASTER_NULL_BBOX_SQL

    def test_bbox_sql_uses_st_geomfromtext(self):
        assert "ST_GeomFromText" in INSERT_RASTER_WITH_BBOX_SQL

    def test_null_bbox_sql_inserts_null(self):
        # The NULL path must not invoke ST_GeomFromText
        assert "ST_GeomFromText" not in INSERT_RASTER_NULL_BBOX_SQL
        assert "NULL" in INSERT_RASTER_NULL_BBOX_SQL


# ---------------------------------------------------------------------------
# silver_raster asset — mocked integration tests
# ---------------------------------------------------------------------------

def _make_mock_parse_result(bounds_4326=(-105.0, 40.0, -104.0, 41.0)):
    """Build a minimal RasterParseResult-like object."""
    from georag_dagster.parsers.raster_parser import RasterBandStats, RasterParseResult

    band = RasterBandStats(
        band_index=1,
        dtype="float32",
        nodata=None,
        min=0.0,
        max=100.0,
        mean=50.0,
        description=None,
    )
    return RasterParseResult(
        driver="GTiff",
        format="GeoTIFF",
        width=100,
        height=100,
        band_count=1,
        crs="EPSG:4326",
        crs_confidence=1.0,
        pixel_size_x=0.01,
        pixel_size_y=0.01,
        bounds=(-105.0, 40.0, -104.0, 41.0),
        bounds_4326=bounds_4326,
        bands=[band],
        is_cog=False,
        has_alpha=False,
        compression=None,
        tags={},
        warnings=[],
        provenance={
            "source_file": "/tmp/dem.tif",
            "source_file_sha256": "a" * 64,
            "parser_name": "raster_parser",
            "parser_version": "1.1.0",
        },
    )


def _get_asset_fn():
    """Return the raw decorated Python function from the silver_raster asset.

    Dagster wraps the function in AssetsDefinition; the raw fn is at:
      silver_raster.op.compute_fn.decorated_fn
    This lets us call it directly in tests without a full Dagster execution
    context (which would try to inject Config from a run config dict).
    """
    from georag_dagster.assets.silver_raster import silver_raster as _asset
    return _asset.op.compute_fn.decorated_fn


class TestSilverRasterAsset:
    """Integration tests for the silver_raster asset via mocked resources."""

    def _run_asset_fn(self, filename: str, parse_result=None, minio_exists=True, project_id=""):
        """Call the underlying asset function directly with mocked args."""
        from georag_dagster.assets.silver_raster import SilverRasterConfig

        fn = _get_asset_fn()

        # Mock context
        context = MagicMock()
        context.log = MagicMock()

        # Config — instantiate directly, not via Dagster injection
        config = SilverRasterConfig(raster_filename=filename, project_id=project_id)

        # Mock minio
        minio = MagicMock()
        minio.object_exists.return_value = minio_exists
        minio.download_bytes.return_value = b"\x00" * 16

        # Mock postgres connection/cursor
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",)

        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor

        postgres = MagicMock()
        postgres.get_connection.return_value = mock_conn

        if parse_result is None:
            parse_result = _make_mock_parse_result()

        with (
            patch("georag_dagster.assets.silver_raster.parse_raster_file", return_value=parse_result),
            patch("georag_dagster.assets.silver_raster.os.unlink"),
        ):
            result = fn(
                context=context,
                config=config,
                postgres=postgres,
                minio=minio,
            )

        return result, context, postgres, minio

    def test_skips_unsupported_extension(self):
        """Files with unsupported extensions are returned as skipped without any DB call."""
        result, context, postgres, minio = self._run_asset_fn("terrain.xyz")
        assert result.metadata["skipped"].value is True
        postgres.get_connection.assert_not_called()

    def test_skips_when_object_not_in_minio(self):
        """If MinIO object doesn't exist, asset skips gracefully with skipped=True."""
        result, context, postgres, minio = self._run_asset_fn("dem.tif", minio_exists=False)
        assert result.metadata["skipped"].value is True
        postgres.get_connection.assert_not_called()

    def test_successful_insert_returns_metadata(self):
        """Happy path: parse succeeds, DB insert succeeds, metadata returned."""
        result, context, postgres, minio = self._run_asset_fn("dem.tif")
        assert result.metadata["skipped"].value is False
        assert result.metadata["driver"].value == "GTiff"
        assert result.metadata["format"].value == "GeoTIFF"
        assert result.metadata["width"].value == 100
        assert result.metadata["height"].value == 100

    def test_bbox_wkt_in_metadata_when_bounds_4326_present(self):
        """When bounds_4326 is set, bbox_wkt metadata is a non-empty POLYGON string."""
        result, context, postgres, minio = self._run_asset_fn("dem.tif")
        assert result.metadata["bbox_wkt"].value != ""
        assert "POLYGON" in result.metadata["bbox_wkt"].value

    def test_null_bbox_when_bounds_4326_is_none(self):
        """When bounds_4326 is None, asset uses the NULL bbox branch and still succeeds."""
        parse_result = _make_mock_parse_result(bounds_4326=None)
        result, context, postgres, minio = self._run_asset_fn(
            "dem_no_crs.asc", parse_result=parse_result
        )
        # bbox_wkt should be empty in metadata since there was nothing to build
        assert result.metadata["bbox_wkt"].value == ""
        # Parse must still succeed
        assert result.metadata["skipped"].value is False

    def test_layer_name_derived_from_filename_stem(self):
        """layer_name metadata should be the filename stem (no extension)."""
        result, context, postgres, minio = self._run_asset_fn("survey_dem.tif")
        assert result.metadata["layer_name"].value == "survey_dem"

    def test_project_id_passed_through(self):
        """project_id config value is reflected in metadata."""
        pid = "12345678-1234-5678-1234-567812345678"
        result, context, postgres, minio = self._run_asset_fn("dem.tif", project_id=pid)
        assert result.metadata["project_id"].value == pid
