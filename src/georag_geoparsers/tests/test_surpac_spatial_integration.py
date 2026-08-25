"""`.str` through parse_spatial_file — the seam, not the reader.

surpac_parser has its own 42 tests. These cover what the SPATIAL layer adds:
the early return past GeoPandas, WKT construction, the ring-vs-line decision,
and the CRS contract. All against the real file, which is the Main Vein
orebody as 129 strings across 73 levels.
"""

from pathlib import Path

import pytest

from georag_geoparsers.spatial_parser import parse_spatial_file

STR_FILE = (
    Path(r"C:\Users\GeoRAG\Desktop\RedStar\Shumagin\Raster_Surfaces\MODELS")
    / "Main Vein" / "JCG_Sections" / "Main Plan Sections.str"
)

pytestmark = pytest.mark.skipif(
    not STR_FILE.exists(), reason="RedStar delivery not present on this machine",
)

#: NAD83 / UTM zone 4N — the code every CRS carrier in that delivery declares.
UTM_4N = 26904


@pytest.fixture(scope="module")
def parsed():
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


def test_coordinates_are_easting_first(parsed):
    # The FILE stores Y,X,Z. Emitting them in file order mirrors the orebody
    # about the diagonal and puts it in the Indian Ocean.
    first = parsed.features[0].geometry_wkt
    x_text, y_text = first.split("((")[1].split(",")[0].split()
    assert 399_000 < float(x_text) < 400_000, "first ordinate should be an easting"
    assert 6_120_000 < float(y_text) < 6_121_000, "second should be a northing"


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

    def test_without_one_the_caller_is_told_not_to_persist(self):
        # Same contract as a .prj-less shapefile, for the same reason:
        # assuming 4326 for projected coordinates is what put a previous
        # delivery at longitude 400,797.
        result = parse_spatial_file(str(STR_FILE))
        assert result.crs_missing is True
        assert [w["code"] for w in result.warnings] == ["surpac_no_crs"]

    def test_the_warning_says_what_to_do_about_it(self):
        result = parse_spatial_file(str(STR_FILE))
        detail = result.warnings[0]["detail"]
        assert "EPSG" in detail
        assert "not written" in detail

    def test_a_bad_epsg_is_refused_before_any_parsing(self):
        # The early return sits AFTER _validate_source_epsg so a bad override
        # fails the same way it does for every other format.
        with pytest.raises(ValueError):
            parse_spatial_file(str(STR_FILE), source_epsg=42)


def test_provenance_names_the_surpac_reader(parsed):
    assert parsed.provenance["parser_name"] == "surpac_parser"
    assert len(parsed.provenance["source_file_sha256"]) == 64
