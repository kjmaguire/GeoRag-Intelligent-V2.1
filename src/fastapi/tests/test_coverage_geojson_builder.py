"""Unit tests for `_build_coverage_geojson` — §6b P4 helper.

The builder converts asyncpg-row-shaped dicts (collar_id, hole_id,
longitude, latitude, has_<dim>...) into a GeoJSON FeatureCollection
matching the contract the §6b `InlineViz` MapView consumes.

These tests cover the builder directly because it's a pure function —
the SQL fetch is exercised separately by the live query test against
the postgres fixture.
"""

from __future__ import annotations

from app.agent.tools import _build_coverage_geojson

# ---------------------------------------------------------------------------
# Fixture rows — minimal asyncpg-row shape (dict-like)
# ---------------------------------------------------------------------------


def _row(
    *,
    collar_id: str = "coll-1",
    hole_id: str = "H-001",
    longitude: float | None = -105.5,
    latitude: float | None = 44.5,
    has_assays: bool = False,
    has_lithology_logs: bool = False,
    has_structure: bool = False,
    has_alteration: bool = False,
    has_samples: bool = False,
) -> dict:
    return {
        "collar_id": collar_id,
        "hole_id": hole_id,
        "longitude": longitude,
        "latitude": latitude,
        "has_assays": has_assays,
        "has_lithology_logs": has_lithology_logs,
        "has_structure": has_structure,
        "has_alteration": has_alteration,
        "has_samples": has_samples,
    }


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


def test_empty_rows_returns_empty_feature_collection():
    """No rows in → valid empty FeatureCollection out. Pin the shape so
    the frontend doesn't crash on the disabled-map hint path."""
    out = _build_coverage_geojson([], selected_dims=None)
    assert out == {"type": "FeatureCollection", "features": []}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_single_collar_with_no_data_marks_has_data_false():
    """A collar with EXISTS=false on every requested attribute is a
    full gap — has_data=false + missing_attributes covers all dims."""
    out = _build_coverage_geojson([_row()], selected_dims=None)
    assert len(out["features"]) == 1
    feat = out["features"][0]
    assert feat["type"] == "Feature"
    assert feat["geometry"] == {"type": "Point", "coordinates": [-105.5, 44.5]}
    props = feat["properties"]
    assert props["has_data"] is False
    assert props["attributes_with_data"] == []
    # All 5 §04e detail tables in missing
    assert set(props["missing_attributes"]) == {
        "assays", "lithology_logs", "structure", "alteration", "samples",
    }


def test_single_collar_with_partial_data():
    """Mixed-state collar: has assays + lithology_logs but lacks the
    other three. Both lists populated correctly."""
    out = _build_coverage_geojson(
        [_row(has_assays=True, has_lithology_logs=True)],
        selected_dims=None,
    )
    props = out["features"][0]["properties"]
    assert props["has_data"] is True
    assert set(props["attributes_with_data"]) == {"assays", "lithology_logs"}
    assert set(props["missing_attributes"]) == {
        "structure", "alteration", "samples",
    }


def test_single_collar_with_full_data_marks_has_data_true():
    """All requested attributes present → has_data=true, missing list empty."""
    out = _build_coverage_geojson(
        [_row(has_assays=True, has_lithology_logs=True,
              has_structure=True, has_alteration=True, has_samples=True)],
        selected_dims=None,
    )
    props = out["features"][0]["properties"]
    assert props["has_data"] is True
    assert props["missing_attributes"] == []
    assert len(props["attributes_with_data"]) == 5


# ---------------------------------------------------------------------------
# Selected dimensions filter
# ---------------------------------------------------------------------------


def test_selected_dims_restricts_property_lists():
    """When the caller passed dimensions=['assays', 'structure'] the
    builder only considers those two — even if the row has data for
    other attributes, they don't count toward has_data."""
    row = _row(has_assays=False, has_structure=False,
               has_lithology_logs=True)  # has data, but not requested
    out = _build_coverage_geojson(
        [row], selected_dims={"assays", "structure"},
    )
    props = out["features"][0]["properties"]
    # has_lithology_logs=True doesn't count because it wasn't requested
    assert props["has_data"] is False
    assert set(props["missing_attributes"]) == {"assays", "structure"}
    assert "lithology_logs" not in props["missing_attributes"]


def test_selected_dims_single_attribute_focused_check():
    """Caller asked only about assays — has_data tracks ONLY assay
    presence. Common case for 'where are the assay-coverage gaps?'."""
    out = _build_coverage_geojson(
        [
            _row(collar_id="c1", has_assays=True),
            _row(collar_id="c2", has_assays=False, has_structure=True),
        ],
        selected_dims={"assays"},
    )
    feats = {f["properties"]["collar_id"]: f["properties"] for f in out["features"]}
    assert feats["c1"]["has_data"] is True
    assert feats["c2"]["has_data"] is False  # has_structure=true is ignored


# ---------------------------------------------------------------------------
# Geometry edge cases
# ---------------------------------------------------------------------------


def test_null_longitude_drops_feature():
    """Defensive: a row with longitude=None should be skipped silently
    (the WHERE clause should prevent this but belt-and-braces)."""
    out = _build_coverage_geojson(
        [_row(longitude=None), _row(collar_id="c2", hole_id="H-002")],
        selected_dims=None,
    )
    assert len(out["features"]) == 1
    assert out["features"][0]["properties"]["collar_id"] == "c2"


def test_null_latitude_drops_feature():
    out = _build_coverage_geojson(
        [_row(latitude=None)], selected_dims=None,
    )
    assert out["features"] == []


def test_coordinates_are_floats_not_decimals():
    """asyncpg may return Decimal for numeric columns; the builder
    explicitly casts to float so the GeoJSON is JSON-serialisable
    without a custom encoder."""
    from decimal import Decimal
    row = _row()
    row["longitude"] = Decimal("-105.5")
    row["latitude"] = Decimal("44.5")
    out = _build_coverage_geojson([row], selected_dims=None)
    coords = out["features"][0]["geometry"]["coordinates"]
    assert isinstance(coords[0], float)
    assert isinstance(coords[1], float)
    assert coords == [-105.5, 44.5]


# ---------------------------------------------------------------------------
# FeatureCollection shape
# ---------------------------------------------------------------------------


def test_feature_collection_has_required_top_level_keys():
    """RFC 7946 FeatureCollection requires `type` + `features`. The
    MapView reader assumes both are present."""
    out = _build_coverage_geojson([_row()], selected_dims=None)
    assert out["type"] == "FeatureCollection"
    assert isinstance(out["features"], list)


def test_each_feature_has_required_keys():
    """Each Feature needs `type`, `geometry`, `properties`. Without
    `properties` the MapView's colour-by-has_data logic crashes."""
    out = _build_coverage_geojson([_row()], selected_dims=None)
    feat = out["features"][0]
    assert set(feat.keys()) == {"type", "geometry", "properties"}
    assert feat["type"] == "Feature"
    assert "coordinates" in feat["geometry"]
    assert "has_data" in feat["properties"]


def test_collar_id_and_hole_id_carry_through_to_properties():
    """The MapView's click popup reads collar_id + hole_id to identify
    the well the user clicked. Pin both."""
    out = _build_coverage_geojson(
        [_row(collar_id="abc-123", hole_id="ECK-22-001")],
        selected_dims=None,
    )
    props = out["features"][0]["properties"]
    assert props["collar_id"] == "abc-123"
    assert props["hole_id"] == "ECK-22-001"


# ---------------------------------------------------------------------------
# Multi-row sanity
# ---------------------------------------------------------------------------


def test_three_collars_mix_of_gap_and_covered():
    """Realistic scenario: 3 collars, 1 has assays, 1 has structure
    only, 1 has nothing. Pin that the FeatureCollection preserves
    order + each has its own properties."""
    out = _build_coverage_geojson(
        [
            _row(collar_id="c1", hole_id="H-001", longitude=-105.5,
                 has_assays=True),
            _row(collar_id="c2", hole_id="H-002", longitude=-105.6,
                 has_structure=True),
            _row(collar_id="c3", hole_id="H-003", longitude=-105.7),
        ],
        selected_dims=None,
    )
    assert len(out["features"]) == 3
    # Order preserved
    ids = [f["properties"]["collar_id"] for f in out["features"]]
    assert ids == ["c1", "c2", "c3"]
    # Per-feature flags
    has_data = [f["properties"]["has_data"] for f in out["features"]]
    assert has_data == [True, True, False]
