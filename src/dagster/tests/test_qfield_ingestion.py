"""CC-03 Item 4 — QField field-observation ingestion tests.

Covers the spatial_parser detection layer:

  - QField-shaped layers (accuracy + corroborating columns) flip is_qfield True.
  - qfield_layers names exactly the layers that match the QField schema.
  - Per-feature properties carry _qfield + _qfield_accuracy_m + _qfield_timestamp.
  - Photo BLOB columns are popped out of the JSONB-bound properties dict
    and surfaced as _qfield_photo_bytes for silver_spatial to upload.
  - A plain GPKG (no QField shape) leaves is_qfield False and the regular
    parse path unchanged.

Plus a smoke test for the SQLite metadata-table probe that detects
QGIS/QField-authored GPKGs even when the user layer doesn't fire the
attribute heuristic.

Run with:  pytest tests/test_qfield_ingestion.py -v
"""

from __future__ import annotations

import sqlite3

import pytest

geopandas = pytest.importorskip("geopandas", reason="geopandas not installed")
shapely = pytest.importorskip("shapely", reason="shapely not installed")

from shapely.geometry import Point  # noqa: E402

from georag_dagster.parsers.spatial_parser import (  # noqa: E402
    _detect_qfield_layer,
    _hoist_qfield_properties,
    _list_gpkg_sqlite_tables,
    parse_spatial_file,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_qfield_gpkg(path: str, *, layer: str = "field_waypoints") -> None:
    """Write a QField-shaped GeoPackage layer.

    Uses geopandas for the geometry + non-binary attribute write, then
    sqlite3 to ALTER TABLE ADD a BLOB photo column and inject one photo
    row. Going through geopandas directly is not viable because fiona /
    pyogrio infer TEXT for a Python ``None`` column, after which pyogrio
    refuses to decode binary bytes back during read.
    """
    import geopandas as gpd

    gdf = gpd.GeoDataFrame(
        {
            "name":       ["Outcrop A", "Outcrop B"],
            "accuracy":   [3.5, 12.0],
            "timestamp":  ["2026-05-23T10:15:00Z", "2026-05-23T10:42:00Z"],
            "device_id":  ["sgs7-001", "sgs7-001"],
        },
        geometry=[Point(-110.5, 59.0), Point(-110.7, 58.8)],
        crs="EPSG:4326",
    )
    gdf.to_file(path, layer=layer, driver="GPKG")

    # GeoPackage installs rtree update triggers that call ST_IsEmpty (a
    # SpatiaLite function not present in vanilla sqlite3). Drop them for
    # the duration of the photo column work — the parser only reads the
    # file so the missing rtree maintenance is harmless for the test.
    with sqlite3.connect(path) as conn:
        triggers = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name = ?",
                (layer,),
            )
        ]
        for t in triggers:
            conn.execute(f'DROP TRIGGER IF EXISTS "{t}"')
        conn.execute(f'ALTER TABLE "{layer}" ADD COLUMN "photo" BLOB')
        conn.execute(
            f'UPDATE "{layer}" SET "photo" = ? WHERE name = ?',
            (sqlite3.Binary(b"\xff\xd8\xff\xe0FAKEJPEG"), "Outcrop A"),
        )
        # Emit a QGIS-authored metadata signal as well.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS qgis_layer_metadata "
            "(layer_name TEXT PRIMARY KEY, meta TEXT)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO qgis_layer_metadata VALUES (?, ?)",
            (layer, "{}"),
        )
        conn.commit()


def _write_plain_gpkg(path: str) -> None:
    import geopandas as gpd

    gdf = gpd.GeoDataFrame(
        {"name": ["fault_x"], "kind": ["thrust"]},
        geometry=[Point(-110.5, 59.0)],
        crs="EPSG:4326",
    )
    gdf.to_file(path, layer="faults", driver="GPKG")


# ---------------------------------------------------------------------------
# Helper-level unit tests
# ---------------------------------------------------------------------------


class TestDetectQfieldLayer:
    def test_accuracy_plus_timestamp_is_qfield(self):
        is_qf, acc_col = _detect_qfield_layer(["name", "accuracy", "timestamp"])
        assert is_qf is True
        assert acc_col == "accuracy"

    def test_accuracy_plus_photo_is_qfield(self):
        is_qf, acc_col = _detect_qfield_layer(["name", "accuracy", "photo"])
        assert is_qf is True

    def test_horizontal_accuracy_variant(self):
        is_qf, acc_col = _detect_qfield_layer(
            ["name", "horizontal_accuracy", "device_id"]
        )
        assert is_qf is True
        assert acc_col == "horizontal_accuracy"

    def test_case_insensitive_column_names(self):
        is_qf, acc_col = _detect_qfield_layer(["NAME", "Accuracy", "Photo"])
        assert is_qf is True
        assert acc_col == "Accuracy"

    def test_accuracy_alone_is_not_qfield(self):
        is_qf, acc_col = _detect_qfield_layer(["name", "accuracy"])
        assert is_qf is False
        assert acc_col is None

    def test_random_geology_layer_is_not_qfield(self):
        is_qf, _ = _detect_qfield_layer(["fault_kind", "dip", "strike"])
        assert is_qf is False


class TestHoistQfieldProperties:
    def test_accuracy_mapped_to_uncertainty_m(self):
        row = {"name": "x", "accuracy": 4.2, "timestamp": "2026-05-23"}
        out = _hoist_qfield_properties(row, "accuracy")
        assert out["_qfield"] is True
        assert out["_qfield_accuracy_m"] == pytest.approx(4.2)
        assert out["_qfield_timestamp"] == "2026-05-23"

    def test_bytes_photo_popped_from_row(self):
        row = {"name": "x", "photo": b"\xff\xd8\xff\xe0JPG"}
        out = _hoist_qfield_properties(row, None)
        assert "_qfield_photo_bytes" in out
        assert out["_qfield_photo_bytes"] == b"\xff\xd8\xff\xe0JPG"
        # photo must NOT survive in row_dict, otherwise _sanitise_properties
        # would stringify the bytes to a useless repr.
        assert "photo" not in row

    def test_string_photo_kept_as_ref(self):
        row = {"name": "x", "photo": "DCIM/IMG_0001.JPG"}
        out = _hoist_qfield_properties(row, None)
        assert out["_qfield_photo_ref"] == "DCIM/IMG_0001.JPG"
        assert "_qfield_photo_bytes" not in out

    def test_empty_photo_blob_skipped(self):
        row = {"name": "x", "photo": b""}
        out = _hoist_qfield_properties(row, None)
        assert "_qfield_photo_bytes" not in out

    def test_no_accuracy_value_skipped(self):
        row = {"name": "x", "accuracy": None}
        out = _hoist_qfield_properties(row, "accuracy")
        assert "_qfield_accuracy_m" not in out


# ---------------------------------------------------------------------------
# End-to-end parse_spatial_file
# ---------------------------------------------------------------------------


class TestQfieldParseEndToEnd:
    def test_qfield_gpkg_flagged_is_qfield(self, tmp_path):
        path = str(tmp_path / "qfield.gpkg")
        _write_qfield_gpkg(path)
        result = parse_spatial_file(path)
        assert result.is_qfield is True
        assert "field_waypoints" in result.qfield_layers

    def test_qfield_gpkg_warning_emitted(self, tmp_path):
        path = str(tmp_path / "qfield.gpkg")
        _write_qfield_gpkg(path)
        result = parse_spatial_file(path)
        codes = [w["code"] for w in result.warnings]
        assert "qfield_detected" in codes

    def test_qfield_metadata_table_probe(self, tmp_path):
        path = str(tmp_path / "qfield.gpkg")
        _write_qfield_gpkg(path)
        result = parse_spatial_file(path)
        assert "qgis_layer_metadata" in result.qfield_metadata_tables

    def test_qfield_feature_carries_accuracy(self, tmp_path):
        path = str(tmp_path / "qfield.gpkg")
        _write_qfield_gpkg(path)
        result = parse_spatial_file(path)
        accuracies = [
            f.properties.get("_qfield_accuracy_m")
            for f in result.features
        ]
        # Both rows: 3.5 + 12.0 — order matches the geopandas insert order.
        assert pytest.approx(3.5) in accuracies
        assert pytest.approx(12.0) in accuracies

    def test_qfield_photo_bytes_attached(self, tmp_path):
        path = str(tmp_path / "qfield.gpkg")
        _write_qfield_gpkg(path)
        result = parse_spatial_file(path)
        photo_features = [
            f for f in result.features
            if "_qfield_photo_bytes" in f.properties
        ]
        assert len(photo_features) == 1
        photo_bytes = photo_features[0].properties["_qfield_photo_bytes"]
        assert isinstance(photo_bytes, bytes)
        assert photo_bytes.startswith(b"\xff\xd8\xff\xe0")

    def test_qfield_features_marked_qfield_true(self, tmp_path):
        path = str(tmp_path / "qfield.gpkg")
        _write_qfield_gpkg(path)
        result = parse_spatial_file(path)
        assert all(f.properties.get("_qfield") is True for f in result.features)

    def test_qfield_list_gpkg_sqlite_tables_smoke(self, tmp_path):
        path = str(tmp_path / "qfield.gpkg")
        _write_qfield_gpkg(path)
        tables = _list_gpkg_sqlite_tables(path)
        # GPKG always has gpkg_contents; our test adds qgis_layer_metadata.
        assert "gpkg_contents" in tables
        assert "qgis_layer_metadata" in tables

    def test_list_tables_returns_empty_on_missing_file(self, tmp_path):
        assert _list_gpkg_sqlite_tables(str(tmp_path / "does_not_exist.gpkg")) == []


class TestPlainGpkgUnchanged:
    def test_plain_gpkg_not_qfield(self, tmp_path):
        path = str(tmp_path / "plain.gpkg")
        _write_plain_gpkg(path)
        result = parse_spatial_file(path)
        assert result.is_qfield is False
        assert result.qfield_layers == []

    def test_plain_gpkg_no_qfield_warning(self, tmp_path):
        path = str(tmp_path / "plain.gpkg")
        _write_plain_gpkg(path)
        result = parse_spatial_file(path)
        codes = [w["code"] for w in result.warnings]
        assert "qfield_detected" not in codes

    def test_plain_gpkg_features_not_marked_qfield(self, tmp_path):
        path = str(tmp_path / "plain.gpkg")
        _write_plain_gpkg(path)
        result = parse_spatial_file(path)
        assert all("_qfield" not in f.properties for f in result.features)
