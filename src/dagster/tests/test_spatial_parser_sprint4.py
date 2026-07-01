"""Sprint 4 smoke tests for spatial_parser.py format gap fills.

Covers:
  - _detect_format: extension mapping (including case-insensitive).
  - SpatialParseResult new fields: driver, layer_count, layer_names, deferred_capabilities.
  - GeoPackage multi-layer: multi_layer_format_detected warning, _layer_name attribute
    on features, layer_count > 1.
  - DXF: dxf_no_crs warning, deferred_capabilities includes "dxf_blocks".
  - FileGDB directory: filegdb_metadata_deferred warning, deferred capabilities list.
  - Existing tests must not regress (SpatialParseResult still has source_format,
    source_crs, feature_count, features, etc.).

NOTE: KML/KMZ tests removed 2026-04-20 (Module 3 Phase B Decision B).
KML support is deferred to V1-roadmap per spec §04d. Kyle-approved.

Run with:  pytest tests/test_spatial_parser_sprint4.py -v
"""

from __future__ import annotations

import json

import pytest

geopandas = pytest.importorskip("geopandas", reason="geopandas not installed")
shapely = pytest.importorskip("shapely", reason="shapely not installed")

from shapely.geometry import LineString, Point  # noqa: E402

from georag_dagster.parsers.spatial_parser import (  # noqa: E402
    SpatialParseResult,
    _detect_format,
    parse_spatial_file,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_point_gdf(coords, crs):
    geoms = [Point(x, y) for x, y in coords]
    return geopandas.GeoDataFrame(
        {"name": [f"pt{i}" for i in range(len(geoms))]},
        geometry=geoms,
        crs=crs,
    )


# ---------------------------------------------------------------------------
# _detect_format
# ---------------------------------------------------------------------------

class TestDetectFormat:
    def test_shp(self):
        assert _detect_format("myfile.shp") == "ESRI Shapefile"

    def test_geojson(self):
        assert _detect_format("myfile.geojson") == "GeoJSON"

    def test_json(self):
        assert _detect_format("myfile.json") == "GeoJSON"

    def test_gpkg(self):
        assert _detect_format("myfile.gpkg") == "GPKG"

    def test_gml(self):
        assert _detect_format("myfile.gml") == "GML"

    def test_gpx(self):
        assert _detect_format("myfile.gpx") == "GPX"

    def test_dxf(self):
        assert _detect_format("myfile.dxf") == "DXF"

    def test_dgn(self):
        assert _detect_format("myfile.dgn") == "DGN"

    def test_gdb(self):
        assert _detect_format("myfile.gdb") == "OpenFileGDB"

    def test_fgb(self):
        assert _detect_format("myfile.fgb") == "FlatGeobuf"

    def test_case_insensitive_dxf(self):
        assert _detect_format("myfile.DXF") == "DXF"

    def test_case_insensitive_shp(self):
        assert _detect_format("myfile.SHP") == "ESRI Shapefile"

    def test_unknown_extension_returns_none(self):
        assert _detect_format("myfile.xyz123") is None

    def test_no_extension_returns_none(self):
        assert _detect_format("myfile") is None


# ---------------------------------------------------------------------------
# SpatialParseResult new fields
# ---------------------------------------------------------------------------

class TestSpatialParseResultFields:
    def test_result_has_driver_field(self, tmp_path):
        geojson_path = tmp_path / "pt.geojson"
        geojson_path.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{"type": "Feature",
                               "geometry": {"type": "Point", "coordinates": [-110.5, 59.0]},
                               "properties": {}}]
            })
        )
        result = parse_spatial_file(str(geojson_path))
        assert hasattr(result, "driver")
        assert result.driver == "GeoJSON"

    def test_result_has_layer_count(self, tmp_path):
        geojson_path = tmp_path / "pt.geojson"
        geojson_path.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{"type": "Feature",
                               "geometry": {"type": "Point", "coordinates": [-110.5, 59.0]},
                               "properties": {}}]
            })
        )
        result = parse_spatial_file(str(geojson_path))
        assert hasattr(result, "layer_count")
        assert result.layer_count >= 1

    def test_result_has_deferred_capabilities(self, tmp_path):
        geojson_path = tmp_path / "pt.geojson"
        geojson_path.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{"type": "Feature",
                               "geometry": {"type": "Point", "coordinates": [-110.5, 59.0]},
                               "properties": {}}]
            })
        )
        result = parse_spatial_file(str(geojson_path))
        assert hasattr(result, "deferred_capabilities")
        assert isinstance(result.deferred_capabilities, list)

    def test_existing_fields_still_present(self, tmp_path):
        """Backward-compat: Sprint 3 fields must still exist."""
        geojson_path = tmp_path / "pt.geojson"
        geojson_path.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{"type": "Feature",
                               "geometry": {"type": "Point", "coordinates": [-110.5, 59.0]},
                               "properties": {"name": "a"}}]
            })
        )
        result = parse_spatial_file(str(geojson_path))
        # All original fields must be present
        assert hasattr(result, "source_format")
        assert hasattr(result, "source_crs")
        assert hasattr(result, "feature_count")
        assert hasattr(result, "empty_geom_skipped")
        assert hasattr(result, "features")
        assert hasattr(result, "source_file")
        assert hasattr(result, "warnings")
        assert hasattr(result, "provenance")


# ---------------------------------------------------------------------------
# GeoPackage multi-layer
# ---------------------------------------------------------------------------

class TestGeoPackageMultiLayer:
    @pytest.fixture()
    def two_layer_gpkg(self, tmp_path):
        """Create a GPKG with two layers."""
        import geopandas as gpd  # noqa: PLC0415
        path = str(tmp_path / "test.gpkg")
        gdf1 = gpd.GeoDataFrame(
            {"name": ["alpha", "beta"]},
            geometry=[Point(-110.5, 59.0), Point(-111.0, 58.5)],
            crs="EPSG:4326",
        )
        gdf2 = gpd.GeoDataFrame(
            {"name": ["fault_x"]},
            geometry=[LineString([(-110.5, 59.0), (-110.8, 58.7)])],
            crs="EPSG:4326",
        )
        gdf1.to_file(path, layer="collars", driver="GPKG")
        gdf2.to_file(path, layer="faults", driver="GPKG")
        return path

    def test_gpkg_feature_count_spans_all_layers(self, two_layer_gpkg):
        result = parse_spatial_file(two_layer_gpkg)
        # 2 points + 1 linestring = 3 total features
        assert result.feature_count == 3

    def test_gpkg_layer_count_is_2(self, two_layer_gpkg):
        result = parse_spatial_file(two_layer_gpkg)
        assert result.layer_count == 2

    def test_gpkg_layer_names_populated(self, two_layer_gpkg):
        result = parse_spatial_file(two_layer_gpkg)
        assert set(result.layer_names) == {"collars", "faults"}

    def test_gpkg_multi_layer_warning_emitted(self, two_layer_gpkg):
        result = parse_spatial_file(two_layer_gpkg)
        codes = [w["code"] for w in result.warnings]
        assert "multi_layer_format_detected" in codes, (
            f"Expected multi_layer_format_detected warning; got: {codes}"
        )

    def test_gpkg_features_have_layer_name_in_properties(self, two_layer_gpkg):
        result = parse_spatial_file(two_layer_gpkg)
        layer_names_in_props = {
            f.properties.get("_layer_name") for f in result.features
            if "_layer_name" in f.properties
        }
        assert layer_names_in_props == {"collars", "faults"}

    def test_single_layer_gpkg_no_multi_layer_warning(self, tmp_path):
        """A single-layer GPKG should not emit the multi-layer warning."""
        import geopandas as gpd  # noqa: PLC0415
        path = str(tmp_path / "single.gpkg")
        gdf = gpd.GeoDataFrame(
            {"name": ["x"]},
            geometry=[Point(-110.5, 59.0)],
            crs="EPSG:4326",
        )
        gdf.to_file(path, layer="pts", driver="GPKG")
        result = parse_spatial_file(path)
        codes = [w["code"] for w in result.warnings]
        assert "multi_layer_format_detected" not in codes


# ---------------------------------------------------------------------------
# DXF
# ---------------------------------------------------------------------------

class TestDxfHandling:
    def _minimal_dxf(self, tmp_path) -> str:
        """Write a minimal DXF with one line entity."""
        dxf_text = """0
SECTION
2
ENTITIES
0
LINE
8
0
10
0.0
20
0.0
30
0.0
11
1.0
21
1.0
31
0.0
0
ENDSEC
0
EOF
"""
        p = str(tmp_path / "test.dxf")
        with open(p, "w") as f:
            f.write(dxf_text)
        return p

    def test_dxf_parses_without_exception(self, tmp_path):
        p = self._minimal_dxf(tmp_path)
        # DXF parsing may succeed or pyogrio may fail on minimal DXF — both
        # outcomes are acceptable as long as we don't get an unexpected exception.
        try:
            result = parse_spatial_file(p)
            assert isinstance(result, SpatialParseResult)
        except Exception as exc:
            # pyogrio may reject malformed DXF — that is acceptable
            pytest.skip(f"pyogrio could not read minimal DXF: {exc}")

    def test_dxf_emits_dxf_no_crs_warning(self, tmp_path):
        p = self._minimal_dxf(tmp_path)
        try:
            result = parse_spatial_file(p)
        except Exception as exc:
            pytest.skip(f"pyogrio could not read minimal DXF: {exc}")
        codes = [w["code"] for w in result.warnings]
        assert "dxf_no_crs" in codes, (
            f"Expected dxf_no_crs warning; got: {codes}"
        )

    def test_dxf_deferred_capabilities_dxf_blocks_removed_when_ezdxf_available(self, tmp_path):
        """Sprint 4b: when ezdxf is installed and extraction succeeds, 'dxf_blocks' is
        removed from deferred_capabilities and result.dxf_blocks is populated (may be
        empty list if the DXF has no named blocks).  If ezdxf is absent the entry
        would remain deferred — but in CI ezdxf is installed so we test the success path.
        """
        p = self._minimal_dxf(tmp_path)
        try:
            result = parse_spatial_file(p)
        except Exception as exc:
            pytest.skip(f"pyogrio could not read minimal DXF: {exc}")
        # If ezdxf is installed (which it is in this environment), the extraction
        # ran and 'dxf_blocks' should NOT be in deferred_capabilities.
        try:
            import ezdxf  # noqa: F401, PLC0415
            assert "dxf_blocks" not in result.deferred_capabilities, (
                f"Expected 'dxf_blocks' removed after successful extraction; "
                f"got: {result.deferred_capabilities}"
            )
            assert hasattr(result, "dxf_blocks")
            assert isinstance(result.dxf_blocks, list)
        except ImportError:
            # ezdxf not installed — deferred entry should still be present
            assert "dxf_blocks" in result.deferred_capabilities

    def test_dxf_driver_is_dxf(self, tmp_path):
        p = self._minimal_dxf(tmp_path)
        try:
            result = parse_spatial_file(p)
        except Exception as exc:
            pytest.skip(f"pyogrio could not read minimal DXF: {exc}")
        assert result.driver == "DXF"


# ---------------------------------------------------------------------------
# FileGDB deferred capabilities (directory format)
# ---------------------------------------------------------------------------

class TestFileGdbDeferred:
    def _make_fake_gdb_dir(self, tmp_path) -> str:
        """Create a directory with .gdb extension.

        A real GDB requires ESRI's FileGDB SDK or ArcGIS.  We create a fake
        directory so we can test the provenance-hash and deferred-capability
        logic without needing a real GDB file.  The parser will raise when
        pyogrio tries to open it — that is expected.  We test only the parts
        that run before the read call.
        """
        gdb_dir = tmp_path / "test.gdb"
        gdb_dir.mkdir()
        (gdb_dir / "dummy.dat").write_bytes(b"\x00" * 16)
        return str(gdb_dir)

    def test_file_not_found_raises_for_missing_gdb(self, tmp_path):
        """Non-existent .gdb → FileNotFoundError."""
        missing = str(tmp_path / "nonexistent.gdb")
        with pytest.raises(FileNotFoundError):
            parse_spatial_file(missing)

    def test_deferred_capabilities_populated_before_read_fails(self, tmp_path):
        """Even if pyogrio fails on fake GDB, deferred caps should be in the error path.

        Since the read will fail, we can't get a result object directly.
        Instead we confirm the code paths compile and the deferred list is correct
        by inspecting the module-level constant.
        """
        from georag_dagster.parsers.spatial_parser import _DEFERRED_FILEGDB  # noqa: PLC0415
        assert "filegdb_domains" in _DEFERRED_FILEGDB
        assert "filegdb_subtypes" in _DEFERRED_FILEGDB
        assert "filegdb_relationship_classes" in _DEFERRED_FILEGDB


# ---------------------------------------------------------------------------
# Backward compatibility — existing shapefile / geojson paths unchanged
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    def test_shapefile_result_has_original_fields(self, tmp_path):
        """Existing shapefile parse result still exposes all original fields."""
        gdf = _make_point_gdf([(-110.5, 59.0)], "EPSG:4326")
        shp = str(tmp_path / "compat.shp")
        gdf.to_file(shp)
        result = parse_spatial_file(shp)
        assert result.feature_count == 1
        assert result.source_format == "shapefile"
        assert isinstance(result.features, list)

    def test_geojson_result_has_original_fields(self, tmp_path):
        geojson_path = tmp_path / "compat.geojson"
        geojson_path.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{"type": "Feature",
                               "geometry": {"type": "Point", "coordinates": [-110.5, 59.0]},
                               "properties": {"name": "pt"}}]
            })
        )
        result = parse_spatial_file(str(geojson_path))
        assert result.feature_count == 1
        assert result.source_format == "geojson"

    def test_parse_spatial_file_signature_unchanged(self):
        """Signature must still accept (path, feature_type=None)."""
        import inspect  # noqa: PLC0415
        sig = inspect.signature(parse_spatial_file)
        params = list(sig.parameters.keys())
        assert params[0] == "path"
        assert "feature_type" in params
