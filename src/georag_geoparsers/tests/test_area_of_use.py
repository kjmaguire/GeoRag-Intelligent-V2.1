"""CRS area-of-use tests that survive the antimeridian (GIS audit 2026-10, finding 6).

pyproj publishes an area that crosses 180 degrees as ``west > east`` (NAD83:
west 167.65, east -40.73). ``west <= lon <= east`` rejects every point in such
an area, so correctly placed Saskatchewan data in EPSG:4269 and Fairbanks data
in Alaska Albers (EPSG:3338) scored 0.0 ("coordinates outside declared CRS
extent") in both the vector and the raster parser.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from georag_geoparsers._area_of_use import classify_bounds, within_area

# pyproj's published areas, as of the version the repo pins.
NAD83 = SimpleNamespace(west=167.65, south=14.92, east=-40.73, north=86.45)
ALASKA_ALBERS = SimpleNamespace(west=172.42, south=51.3, east=-129.99, north=71.4)
UTM_13N = SimpleNamespace(west=-108.0, south=28.98, east=-102.0, north=84.0)
WORLD = SimpleNamespace(west=-180.0, south=-90.0, east=180.0, north=90.0)


class TestWithinArea:
    def test_a_point_in_a_wrapping_area_is_inside(self) -> None:
        assert within_area(NAD83, -106.0, 58.0)          # Saskatchewan
        assert within_area(NAD83, -160.5, 55.2)          # Unga Island
        assert within_area(NAD83, 175.0, 52.0)           # west of the antimeridian
        assert within_area(ALASKA_ALBERS, -147.7, 64.84)  # Fairbanks

    def test_a_point_outside_a_wrapping_area_is_outside(self) -> None:
        assert not within_area(NAD83, 10.0, 50.0)         # Europe
        assert not within_area(NAD83, 120.0, 40.0)        # China
        assert not within_area(NAD83, -106.0, -30.0)      # right longitude, wrong hemisphere

    def test_an_ordinary_area_is_unchanged(self) -> None:
        assert within_area(UTM_13N, -105.0, 58.0)
        assert not within_area(UTM_13N, -120.0, 58.0)
        assert within_area(WORLD, 179.9, -89.0)

    def test_slack_widens_the_area_on_every_side_including_across_the_wrap(self) -> None:
        assert not within_area(UTM_13N, -101.0, 58.0)
        assert within_area(UTM_13N, -101.0, 58.0, slack_deg=3.0)
        # 167.65 - 3 = 164.65 is inside the widened NAD83 west edge.
        assert within_area(NAD83, 165.0, 52.0, slack_deg=3.0)
        assert not within_area(NAD83, 165.0, 52.0)


class TestClassifyBounds:
    def test_saskatchewan_in_nad83_is_inside_not_outside(self) -> None:
        assert classify_bounds(NAD83, -106.1, 57.9, -105.9, 58.1) == "inside"

    def test_fairbanks_in_alaska_albers_is_inside(self) -> None:
        assert classify_bounds(ALASKA_ALBERS, -147.8, 64.8, -147.6, 64.9) == "inside"

    def test_an_island_east_of_the_antimeridian_is_inside_too(self) -> None:
        assert classify_bounds(ALASKA_ALBERS, 172.5, 52.0, 173.0, 52.5) == "inside"

    def test_data_nowhere_near_a_wrapping_area_is_outside(self) -> None:
        assert classify_bounds(NAD83, 9.9, 49.9, 10.1, 50.1) == "outside"
        assert classify_bounds(NAD83, -106.1, -31.0, -105.9, -30.0) == "outside"

    def test_data_straddling_the_edge_of_a_wrapping_area_is_partial(self) -> None:
        # -41.5 is inside (east edge -40.73 and beyond it west), -40.0 is not.
        assert classify_bounds(NAD83, -41.5, 50.0, -40.0, 51.0) == "partial"

    def test_a_world_wide_box_cannot_be_told_from_a_wrapping_one_and_is_partial(self) -> None:
        # total_bounds of data that really crosses 180 is -179.9 .. 179.9.
        assert classify_bounds(ALASKA_ALBERS, -179.9, 52.0, 179.9, 53.0) == "partial"

    def test_ordinary_areas_are_unchanged(self) -> None:
        assert classify_bounds(UTM_13N, -106.0, 57.0, -104.0, 58.0) == "inside"
        assert classify_bounds(UTM_13N, -120.0, 57.0, -118.0, 58.0) == "outside"
        assert classify_bounds(UTM_13N, -110.0, 57.0, -104.0, 58.0) == "partial"
        assert classify_bounds(UTM_13N, -106.0, 10.0, -104.0, 20.0) == "outside"
        assert classify_bounds(UTM_13N, -106.0, 20.0, -104.0, 40.0) == "partial"


# ---------------------------------------------------------------------------
# The two parsers' CRS-confidence scores, through real pyproj areas
# ---------------------------------------------------------------------------
class TestSpatialParserScore:
    def _gdf(self, lon: float, lat: float, crs: str):
        gpd = pytest.importorskip("geopandas")
        from shapely.geometry import Point

        return gpd.GeoDataFrame(geometry=[Point(lon, lat)], crs="EPSG:4326").to_crs(crs)

    def test_saskatchewan_in_nad83_scores_full(self) -> None:
        from georag_geoparsers.spatial_parser import _score_crs_confidence

        score, reason = _score_crs_confidence(self._gdf(-106.0, 58.0, "EPSG:4269"))
        assert (score, reason) == (1.0, "bounds match CRS extent")

    def test_saskatchewan_in_nad27_scores_full(self) -> None:
        from georag_geoparsers.spatial_parser import _score_crs_confidence

        assert _score_crs_confidence(self._gdf(-106.0, 58.0, "EPSG:4267"))[0] == 1.0

    def test_fairbanks_in_alaska_albers_scores_full(self) -> None:
        from georag_geoparsers.spatial_parser import _score_crs_confidence

        assert _score_crs_confidence(self._gdf(-147.72, 64.84, "EPSG:3338"))[0] == 1.0

    def test_data_that_really_is_outside_still_scores_zero(self) -> None:
        from georag_geoparsers.spatial_parser import _score_crs_confidence

        # NAD83 labelled on data in Germany.
        gpd = pytest.importorskip("geopandas")
        from shapely.geometry import Point

        gdf = gpd.GeoDataFrame(geometry=[Point(10.0, 50.0)], crs="EPSG:4269")
        score, reason = _score_crs_confidence(gdf)
        assert score == 0.0 and "outside" in reason

    def test_no_crs_is_still_zero(self) -> None:
        gpd = pytest.importorskip("geopandas")
        from shapely.geometry import Point

        from georag_geoparsers.spatial_parser import _score_crs_confidence

        assert _score_crs_confidence(gpd.GeoDataFrame(geometry=[Point(0, 0)]))[0] == 0.0


class TestRasterParserScore:
    def test_saskatchewan_in_nad83_scores_full(self) -> None:
        from georag_geoparsers.raster_parser import _score_crs_confidence

        assert _score_crs_confidence("EPSG:4269", (-106.1, 57.9, -105.9, 58.1)) == 1.0

    def test_fairbanks_in_alaska_albers_scores_full(self) -> None:
        from georag_geoparsers.raster_parser import _score_crs_confidence

        assert _score_crs_confidence("EPSG:3338", (-147.8, 64.8, -147.6, 64.9)) == 1.0

    def test_a_raster_that_really_is_outside_still_scores_zero(self) -> None:
        from georag_geoparsers.raster_parser import _score_crs_confidence

        assert _score_crs_confidence("EPSG:4269", (9.9, 49.9, 10.1, 50.1)) == 0.0

    def test_partial_overlap_is_still_half(self) -> None:
        from georag_geoparsers.raster_parser import _score_crs_confidence

        assert _score_crs_confidence("EPSG:26913", (-110.0, 57.0, -104.0, 58.0)) == 0.5

    def test_missing_crs_or_bounds_is_zero(self) -> None:
        from georag_geoparsers.raster_parser import _score_crs_confidence

        assert _score_crs_confidence(None, (0, 0, 1, 1)) == 0.0
        assert _score_crs_confidence("EPSG:4269", None) == 0.0
