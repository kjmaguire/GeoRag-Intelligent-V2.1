"""`.str` through parse_spatial_file — the seam, not the reader.

surpac_parser has its own 42 tests. These cover what the SPATIAL layer adds:
the early return past GeoPandas, WKT construction, the ring-vs-line decision,
and the CRS contract. All against the real file, which is the Main Vein
orebody as 129 strings across 73 levels.
"""

import hashlib
import os
from pathlib import Path

import pytest
from pyproj import Transformer

from georag_geoparsers.spatial_parser import parse_spatial_file

#: GEORAG_REDSTAR_DIR points at the RedStar delivery when it is mounted
#: somewhere else.
_REDSTAR = Path(os.environ.get("GEORAG_REDSTAR_DIR", r"C:\Users\GeoRAG\Desktop\RedStar"))
STR_FILE = (
    _REDSTAR / "Shumagin" / "Raster_Surfaces" / "MODELS"
    / "Main Vein" / "JCG_Sections" / "Main Plan Sections.str"
)

#: Per test, not per module: a module-level skip also skipped the test that
#: never reads the file (audit finding 24).
needs_redstar = pytest.mark.skipif(
    not STR_FILE.exists(),
    reason="RedStar delivery not present on this machine (set GEORAG_REDSTAR_DIR)",
)

#: NAD83 / UTM zone 4N — the code every CRS carrier in that delivery declares.
UTM_4N = 26904


@pytest.fixture(scope="module")
def parsed():
    if not STR_FILE.exists():
        pytest.skip("RedStar delivery not present on this machine (set GEORAG_REDSTAR_DIR)")
    return parse_spatial_file(str(STR_FILE), source_epsg=UTM_4N)


def test_it_does_not_go_through_geopandas(parsed):
    # There is no OGR driver for Surpac; gpd.read_file cannot open a .str at
    # all. The early return is what makes this work, and `driver=None` is how
    # a caller can tell this result was not built from a GeoDataFrame.
    assert parsed.source_format == "surpac"
    assert parsed.driver is None


def test_every_string_becomes_a_feature(parsed):
    assert parsed.feature_count == 129
    assert len(parsed.features) == 129


def test_closed_strings_are_polygons_and_open_ones_are_not(parsed):
    # 127 of 129 repeat their first vertex byte-for-byte; the other 2 have
    # real endpoint gaps (0.73 m and 0.40 m). Closing those would invent vein
    # outline nobody digitised.
    kinds: dict[str, int] = {}
    for f in parsed.features:
        kinds[f.geometry_type] = kinds.get(f.geometry_type, 0) + 1
    assert kinds == {"Polygon": 127, "LineString": 2}


def test_the_level_elevation_survives_in_properties(parsed):
    # silver.spatial_features.geom is 2D and every insert is ST_Force2D'd, so
    # a level carried in the geometry is a level lost. 73 distinct elevations
    # from -235 m to +125 m ARE the dataset — flatten them and 73 level plans
    # collapse into one plane.
    levels = sorted({f.properties["level_z"] for f in parsed.features})
    assert len(levels) == 73
    assert levels[0] == -235.0
    assert levels[-1] == 125.0


def test_coordinates_are_reprojected_to_wgs84(parsed):
    # spatial_features.geom is geometry(Geometry,4326) and the INSERT does not
    # transform — the GeoPandas path reprojects before WKT is taken, and the
    # early return for .str skipped that. A UTM easting stored under SRID 4326
    # is longitude 399,183, which is the same class of failure as the
    # .prj-less shapefile that landed at longitude 400,797.
    #
    # Shumagin Island sits at roughly 160.6 W, 55.2 N.
    first = parsed.features[0].geometry_wkt
    lon_text, lat_text = first.split("((")[1].split(",")[0].split()
    lon, lat = float(lon_text), float(lat_text)
    assert -161.0 < lon < -160.0, f"longitude out of range for Shumagin: {lon}"
    assert 55.0 < lat < 55.5, f"latitude out of range for Shumagin: {lat}"


def test_the_axes_are_not_swapped(parsed):
    # The FILE stores Y,X,Z. Emitting them in file order mirrors the orebody
    # about the diagonal — which after reprojection lands it in the Indian
    # Ocean rather than merely somewhere odd, so the sign check is the tell.
    for f in parsed.features[:20]:
        body = f.geometry_wkt.split("((")[-1].split("(")[-1].rstrip(")")
        lon, lat = (float(v) for v in body.split(",")[0].split())
        assert lon < 0, "longitude should be negative in Alaska"
        assert lat > 0, "latitude should be positive in Alaska"


def test_wkt_carries_no_scientific_notation(parsed):
    # PostGIS rejects "1e-05" in WKT. Reprojected longitudes are small enough
    # that an f-string could produce it, which would lose the whole file at
    # the insert rather than at the parse.
    for f in parsed.features:
        assert "e-" not in f.geometry_wkt.lower()
        assert "e+" not in f.geometry_wkt.lower()


def test_properties_carry_what_the_map_needs_to_label_a_string(parsed):
    props = parsed.features[0].properties
    assert set(props) == {"surpac_string_number", "level_z", "point_count", "closed"}
    assert props["point_count"] > 0


class TestCrsContract:
    """Surpac declares nothing, so the EPSG must come from the operator."""

    def test_an_epsg_is_recorded_as_an_override_not_as_a_declaration(self, parsed):
        assert parsed.source_crs == f"EPSG:{UTM_4N}"
        assert parsed.crs_missing is False
        assert parsed.crs_override_applied is True

    @needs_redstar
    def test_without_one_the_caller_is_told_not_to_persist(self):
        # Same contract as a .prj-less shapefile, for the same reason:
        # assuming 4326 for projected coordinates is what put a previous
        # delivery at longitude 400,797.
        result = parse_spatial_file(str(STR_FILE))
        assert result.crs_missing is True
        assert [w["code"] for w in result.warnings] == ["surpac_no_crs"]

    @needs_redstar
    def test_the_warning_says_what_to_do_about_it(self):
        result = parse_spatial_file(str(STR_FILE))
        detail = result.warnings[0]["detail"]
        assert "EPSG" in detail
        assert "not written" in detail

    def test_a_bad_epsg_is_refused_before_any_parsing(self, tmp_path):
        # The early return sits AFTER _validate_source_epsg so a bad override
        # fails the same way it does for every other format. Refused before
        # the file is even opened, so no delivery is needed: the path below
        # does not exist, and a ValueError (not FileNotFoundError) proves the
        # order.
        with pytest.raises(ValueError, match="1024-32767"):
            parse_spatial_file(str(tmp_path / "missing.str"), source_epsg=42)


def test_provenance_names_the_surpac_reader(parsed):
    assert parsed.provenance["parser_name"] == "surpac_parser"
    assert len(parsed.provenance["source_file_sha256"]) == 64


# ===========================================================================
# The same seam on a file built here -- runs everywhere
# ===========================================================================
#
# Surpac record layout: string_number, Y, X, Z, descriptors. Y comes BEFORE X.
# Coordinates sit in NAD83 / UTM 4N near Shumagin Island; every expected
# longitude/latitude below is produced by pyproj in the test, not read back from
# the code under test.

TITLE = "synthetic.str,,Generated by test,"
AXIS = "0,0.000,0.000,0.000,0.000,1.000,0.000"
TERMINATOR = "0,0.000,0.000,0.000"
END = "0,0.000,0.000,0.000,END"

#: (string number, level z, [(x, y), ...]) in the order the test means them.
SQUARE_LEVEL_100 = (1, 100.0, [(400000.0, 6120000.0), (400050.0, 6120000.0), (400050.0, 6120050.0),
                               (400000.0, 6120050.0), (400000.0, 6120000.0)])
SQUARE_LEVEL_105 = (2, 105.0, [(400000.0, 6120000.0), (400050.0, 6120000.0), (400050.0, 6120050.0),
                               (400000.0, 6120050.0), (400000.0, 6120000.0)])
OPEN_LINE_LEVEL_110 = (3, 110.0, [(400100.0, 6120100.0), (400150.0, 6120100.0), (400150.0, 6120150.0)])
# Two vertices that are the same point: "closed" by the reader, but no area.
DEGENERATE_LEVEL_115 = (4, 115.0, [(400200.0, 6120200.0), (400200.0, 6120200.0)])

STRINGS = (SQUARE_LEVEL_100, SQUARE_LEVEL_105, OPEN_LINE_LEVEL_110, DEGENERATE_LEVEL_115)


def _str_text() -> bytes:
    lines = [TITLE, AXIS]
    for number, z, points in STRINGS:
        lines += [f"{number},{y!r},{x!r},{z!r}," for x, y in points]   # Y first, as Surpac writes it
        lines.append(TERMINATOR)
    lines.append(END)
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


@pytest.fixture(scope="module")
def synthetic_path(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("surpac") / "Synthetic Sections.str"
    path.write_bytes(_str_text())
    return path


@pytest.fixture(scope="module")
def synthetic(synthetic_path):
    return parse_spatial_file(str(synthetic_path), source_epsg=UTM_4N)


def _to_wgs84(points):
    transform = Transformer.from_crs(f"EPSG:{UTM_4N}", "EPSG:4326", always_xy=True).transform
    return [transform(x, y) for x, y in points]


def _wkt_vertices(wkt: str) -> list[tuple[float, float]]:
    body = wkt[wkt.index("(") :].replace("(", "").replace(")", "")
    return [tuple(float(v) for v in pair.split()) for pair in body.split(",")]


def _feature(parsed, number: int):
    return next(f for f in parsed.features if f.properties["surpac_string_number"] == number)


class TestSyntheticSeam:
    def test_it_does_not_go_through_geopandas(self, synthetic):
        assert synthetic.source_format == "surpac"
        assert synthetic.driver is None
        assert synthetic.layer_names == ["Synthetic Sections"]

    def test_every_string_becomes_a_feature(self, synthetic):
        assert synthetic.feature_count == len(STRINGS) == len(synthetic.features)

    def test_rings_become_polygons_lines_stay_lines_and_one_location_is_a_point(self, synthetic):
        kinds = {f.properties["surpac_string_number"]: f.geometry_type for f in synthetic.features}
        assert kinds == {1: "Polygon", 2: "Polygon", 3: "LineString", 4: "Point"}

    def test_a_closed_string_without_area_is_a_point_not_a_polygon(self, synthetic):
        # A,A satisfies "repeats its first vertex" and encloses nothing. As a
        # line it would be LINESTRING(A, A): zero length, invalid to PostGIS,
        # and drawn as nothing on the map. It marks one location, so it is one.
        feature = _feature(synthetic, 4)
        assert feature.properties["closed"] is True
        assert feature.properties["point_count"] == 2
        assert feature.geometry_wkt.startswith("POINT(")

    def test_an_open_string_is_not_closed_for_the_operator(self, synthetic):
        feature = _feature(synthetic, 3)
        assert feature.properties["closed"] is False
        vertices = _wkt_vertices(feature.geometry_wkt)
        assert vertices[0] != vertices[-1], "closing it would invent outline nobody digitised"

    def test_the_level_survives_in_properties_and_not_in_the_geometry(self, synthetic):
        levels = {f.properties["surpac_string_number"]: f.properties["level_z"] for f in synthetic.features}
        assert levels == {1: 100.0, 2: 105.0, 3: 110.0, 4: 115.0}
        for f in synthetic.features:
            assert all(len(v) == 2 for v in _wkt_vertices(f.geometry_wkt)), "the geometry column is 2D"

    def test_vertices_are_reprojected_with_x_east_and_y_north(self, synthetic):
        # The file stores Y,X,Z. Reading the columns in file order swaps the
        # axes, and pyproj then either raises or lands the orebody elsewhere.
        for number, _z, points in STRINGS:
            feature = _feature(synthetic, number)
            expected = _to_wgs84(points)
            if feature.geometry_type == "Point":
                expected = expected[:1]   # one distinct vertex, written once
            actual = _wkt_vertices(feature.geometry_wkt)
            assert len(actual) == len(expected)
            for (lon, lat), (exp_lon, exp_lat) in zip(actual, expected, strict=True):
                assert lon == pytest.approx(exp_lon, abs=1e-9), number
                assert lat == pytest.approx(exp_lat, abs=1e-9), number

    def test_the_result_is_in_the_right_hemisphere_for_shumagin(self, synthetic):
        lon, lat = _wkt_vertices(_feature(synthetic, 1).geometry_wkt)[0]
        assert -161.0 < lon < -160.0
        assert 55.0 < lat < 55.5

    def test_a_ring_is_written_as_a_polygon_with_its_first_vertex_repeated(self, synthetic):
        vertices = _wkt_vertices(_feature(synthetic, 1).geometry_wkt)
        assert vertices[0] == vertices[-1]
        assert len(vertices) == 5

    def test_wkt_carries_no_scientific_notation(self, synthetic):
        for f in synthetic.features:
            assert "e-" not in f.geometry_wkt.lower()
            assert "e+" not in f.geometry_wkt.lower()

    def test_properties_carry_what_the_map_needs_to_label_a_string(self, synthetic):
        for f in synthetic.features:
            assert set(f.properties) == {"surpac_string_number", "level_z", "point_count", "closed"}
        assert _feature(synthetic, 1).properties["point_count"] == 5
        assert _feature(synthetic, 3).properties["point_count"] == 3

    def test_the_feature_type_is_one_the_table_accepts(self, synthetic):
        # chk_spatial_features_type rejects anything outside its vocabulary,
        # and NOT VALID exempts only old rows: a wrong value loses the file.
        assert {f.feature_type for f in synthetic.features} == {"mineralization_zone"}

    def test_provenance_names_the_reader_and_hashes_the_bytes(self, synthetic, synthetic_path):
        assert synthetic.provenance["parser_name"] == "surpac_parser"
        assert synthetic.provenance["source_file_sha256"] == hashlib.sha256(synthetic_path.read_bytes()).hexdigest()


class TestSyntheticCrsContract:
    """Surpac declares nothing, so the EPSG must come from the operator."""

    def test_an_epsg_is_recorded_as_an_override_not_as_a_declaration(self, synthetic):
        assert synthetic.source_crs == f"EPSG:{UTM_4N}"
        assert synthetic.crs_missing is False
        assert synthetic.crs_override_applied is True
        assert synthetic.warnings == []

    def test_without_one_the_caller_is_told_not_to_persist(self, synthetic_path):
        result = parse_spatial_file(str(synthetic_path))

        assert result.crs_missing is True
        assert result.crs_override_applied is False
        assert result.source_crs == ""
        assert [w["code"] for w in result.warnings] == ["surpac_no_crs"]

    def test_the_warning_names_the_file_and_says_what_to_do(self, synthetic_path):
        detail = parse_spatial_file(str(synthetic_path)).warnings[0]["detail"]

        assert "Synthetic Sections.str" in detail
        assert "EPSG" in detail
        assert "not written" in detail

    def test_with_no_crs_the_coordinates_are_not_pretended_to_be_degrees(self, synthetic_path):
        # Unreprojected: the grid numbers come back as they are, which is why
        # crs_missing must stop the caller from persisting them under SRID 4326.
        first = _wkt_vertices(parse_spatial_file(str(synthetic_path)).features[0].geometry_wkt)[0]

        assert first == (400000.0, 6120000.0)

    def test_a_bad_epsg_is_refused_before_any_parsing(self, synthetic_path):
        with pytest.raises(ValueError):
            parse_spatial_file(str(synthetic_path), source_epsg=42)
