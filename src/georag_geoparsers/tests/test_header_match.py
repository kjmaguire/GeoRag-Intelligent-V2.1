"""The header-matching rule every drill parser and the classifier share.

Written against the failure that produced it: on 2026-08-24 a delivery was
rejected in full because its collar table spelled the key column ``Hole ID``
rather than ``Hole_ID``, and the report told the geologist to go and rename
their spreadsheet.
"""

import pytest

from georag_geoparsers._drill_schema import (
    COLLAR_ALIASES,
    COLLAR_REQUIRED,
    coordinate_bounds,
    coordinate_family_conflict,
    detect_coordinate_mode,
)
from georag_geoparsers._header_match import (
    alias_skeletons,
    build_column_map,
    normalize_header,
)


class TestNormalizeHeader:
    @pytest.mark.parametrize("spelling", [
        "Hole ID", "Hole_ID", "HOLEID", "holeId", "HOLE-ID", "hole.id", " Hole ID ",
    ])
    def test_the_spellings_people_type_all_converge(self, spelling):
        assert normalize_header(spelling) == "holeid"

    @pytest.mark.parametrize("spelling", [
        "Depth", "Depth_m", "Depth (m)", "depth_metres", "DEPTH_FT", "Depth feet",
    ])
    def test_a_trailing_unit_is_dropped(self, spelling):
        assert normalize_header(spelling) == "depth"

    def test_a_unit_is_never_trimmed_off_a_real_word(self):
        # The reason stripping runs on tokens and not on the joined string:
        # "datum" ends in the metre token but is not a unit-suffixed name.
        assert normalize_header("datum") == "datum"
        assert normalize_header("Formation") == "formation"

    def test_a_column_genuinely_named_for_a_unit_survives(self):
        # Something must be left after the strip, or "M" would become "".
        assert normalize_header("M") == "m"
        assert normalize_header("ft") == "ft"

    def test_digits_are_kept(self):
        # from1/from2 are different columns in an interval table.
        assert normalize_header("From1") != normalize_header("From2")

    @pytest.mark.parametrize("empty", ["", "   ", "___", "()", None])
    def test_a_header_with_no_name_normalises_to_nothing(self, empty):
        assert normalize_header(empty) == ""

    def test_an_acronym_running_into_a_word_splits(self):
        assert normalize_header("DDHNumber") == normalize_header("DDH_Number")


class TestAliasSkeletons:
    def test_the_canonical_name_is_always_one_of_its_own_spellings(self):
        # _drill_schema no longer repeats each canonical inside its alias
        # list. Anything normalising alias lists by hand loses it — which
        # is how a sheet headed exactly "lithology_code" briefly stopped
        # counting toward lithology's coverage.
        assert "elevation" in alias_skeletons("elevation", ["RL", "Z"])

    def test_empty_aliases_are_dropped_not_matched(self):
        assert "" not in alias_skeletons("hole_id", ["", "   "])


class TestBuildColumnMap:
    def test_a_real_collar_header_row_maps_completely(self):
        columns = ["Hole ID", "East (m)", "North (m)", "Collar RL", "EOH Depth", "Bearing"]
        column_map, unmapped = build_column_map(columns, COLLAR_ALIASES)

        assert column_map["hole_id"] == "Hole ID"
        assert column_map["easting"] == "East (m)"
        assert column_map["northing"] == "North (m)"
        assert column_map["elevation"] == "Collar RL"
        assert unmapped == []
        assert COLLAR_REQUIRED - set(column_map) == frozenset()

    def test_one_column_is_claimed_by_at_most_one_field(self):
        # The caller inverts this dict to rename a dataframe, so a column
        # claimed twice silently drops a field.
        columns = ["HoleID", "Easting", "Northing", "Type", "Status"]
        column_map, _ = build_column_map(columns, COLLAR_ALIASES)
        assert len(set(column_map.values())) == len(column_map)

    def test_an_explicit_alias_beats_the_canonical_name(self):
        columns = ["Easting", "easting"]
        column_map, _ = build_column_map(columns, COLLAR_ALIASES)
        assert column_map["easting"] == "Easting"

    def test_unmatched_columns_come_back_in_file_order(self):
        columns = ["HoleID", "Grids_Name", "Easting", "LineNumber"]
        _, unmapped = build_column_map(columns, COLLAR_ALIASES)
        assert unmapped == ["Grids_Name", "LineNumber"]

    def test_a_sheet_with_no_recognisable_headers_maps_nothing(self):
        column_map, unmapped = build_column_map(["alpha", "beta"], COLLAR_ALIASES)
        assert column_map == {}
        assert unmapped == ["alpha", "beta"]


class TestCoordinateMode:
    def test_decimal_degrees_read_as_geographic(self):
        assert detect_coordinate_mode([-134.52, -134.61], [55.91, 55.88]) == "geographic"

    def test_utm_reads_as_projected(self):
        assert detect_coordinate_mode([471250.5], [4657100.0]) == "projected"

    def test_one_projected_row_makes_the_whole_file_projected(self):
        # The safe direction: projected bounds are wide enough to accept
        # degrees, so a misread costs nothing, while reading a UTM file as
        # geographic would reject every row of it.
        assert detect_coordinate_mode([-134.52, 471250.5], [55.91, 4657100.0]) == "projected"

    def test_a_file_with_no_populated_pairs_is_projected(self):
        assert detect_coordinate_mode([None, None], [None, None]) == "projected"

    def test_a_local_mine_grid_is_inside_projected_bounds(self):
        lo, hi = coordinate_bounds("projected")["easting"]
        assert lo <= 5_000.0 <= hi          # local grid
        assert lo <= 2_193_000.0 <= hi      # State Plane, survey feet
        assert lo <= -71_000.0 <= hi        # negative easting


class TestCoordinateFamilyConflict:
    def test_metres_paired_with_degrees_is_a_conflict(self):
        assert coordinate_family_conflict("Easting", "LATITUDE") == (
            "projected", "geographic",
        )

    def test_an_axis_name_that_says_nothing_is_not_a_conflict(self):
        # X/Y name an axis without saying what is measured along it.
        assert coordinate_family_conflict("X", "Latitude") is None
        assert coordinate_family_conflict("X_Coord", "Y_Coord") is None

    @pytest.mark.parametrize(("east", "north"), [
        ("Easting", "Northing"),
        ("Longitude", "Latitude"),
        ("UTM_E", "UTM_N"),
    ])
    def test_a_matched_pair_is_never_a_conflict(self, east, north):
        assert coordinate_family_conflict(east, north) is None

    def test_a_missing_column_cannot_conflict(self):
        assert coordinate_family_conflict(None, "Latitude") is None
        assert coordinate_family_conflict("Easting", None) is None
