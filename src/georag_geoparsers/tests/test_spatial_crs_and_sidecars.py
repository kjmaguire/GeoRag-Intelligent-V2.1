"""Real-GDAL tests for the spatial parser's CRS decision and sidecar handling.

Every test in this file performs an ACTUAL pyogrio/GDAL read of an actual
file on disk. That is the point of it. The two spatial test files that
existed before this one build their parse results from SimpleNamespace and a
hand-rolled stub and never open anything, so every CRS assertion in them is
an assertion about the classifier rather than about what GDAL returns — and
they kept passing throughout the period in which this parser was converting
correctly-georeferenced Alaskan shapefiles into off-planet coordinates.

The fixtures are written with geopandas rather than committed as binaries,
and they are shaped after the RedStar hand-off, which is where each of these
defects was found:

  * drobeck_shumagin_veins.shp — 56 records, 2 of them ESRI Null shapes, no
    .shx, no .dbf, and its .prj delivered as Drobeck_Shumagin_Veins.prj.
    Four separate faults in one file.
  * every geometry sits on Unga Island, Alaska, in EPSG:26904 (NAD83 / UTM
    zone 4N): easting ~400,798, northing ~6,117,306 → 160.56°W, 55.19°N.
    Read as EPSG:4326 those same numbers are longitude 400,798 degrees.

Run with:  pytest tests/test_spatial_crs_and_sidecars.py -v
"""

from __future__ import annotations

import os

import pytest

geopandas = pytest.importorskip("geopandas", reason="geopandas not installed")
shapely = pytest.importorskip("shapely", reason="shapely not installed")

# The importorskip calls above MUST run before these imports -- that is the
# whole point of them, and it is what E402 flags. Skipping the module is the
# correct outcome on a machine without the geospatial stack; moving the
# imports up would turn the skip into a collection error.
import geopandas as gpd  # noqa: E402
import pandas as pd  # noqa: E402
from shapely.geometry import LineString, Point  # noqa: E402

from georag_geoparsers.spatial_parser import (  # noqa: E402
    _NO_CRS_EXTENSIONS,
    _VECTOR_EXTENSIONS,
    _extract_features,
    _is_null_geometry,
    _resolve_crs,
    _validate_source_epsg,
    parse_spatial_file,
)

# Unga Island, Alaska, in EPSG:26904 (NAD83 / UTM zone 4N).
UNGA_EPSG = 26904
UNGA_LINES = [
    LineString([(400798, 6117306), (400900, 6117400)]),
    LineString([(401500, 6118000), (401600, 6118100)]),
]
UNGA_LON = -160.56
UNGA_LAT = 55.19


def _codes(result) -> list[str]:
    return [w["code"] for w in result.warnings]


def _warning(result, code: str) -> dict:
    matches = [w for w in result.warnings if w["code"] == code]
    assert matches, f"expected a {code!r} warning; got {_codes(result)}"
    return matches[0]


def _write_shapefile(directory, stem: str, *, with_nulls: bool = False) -> str:
    """Write a real EPSG:26904 shapefile and return the .shp path.

    The attribute column is a string, deliberately: with a str-dtype column
    and nothing else, ``DataFrame.iterrows()`` rebuilds a null geometry as
    NaN rather than None — which is the exact shape of the RedStar crash.
    """
    geoms = list(UNGA_LINES)
    fids = ["a", "b"]
    if with_nulls:
        geoms = [UNGA_LINES[0], None, UNGA_LINES[1]]
        fids = ["a", "b", "c"]
    gdf = gpd.GeoDataFrame({"FID": fids}, geometry=geoms, crs=f"EPSG:{UNGA_EPSG}")
    path = os.path.join(str(directory), f"{stem}.shp")
    gdf.to_file(path)
    return path


def _drop_sidecar(shp_path: str, ext: str) -> None:
    os.remove(os.path.splitext(shp_path)[0] + ext)


# ---------------------------------------------------------------------------
# Requirement 7 — an ESRI Null shape must not take the layer down with it
# ---------------------------------------------------------------------------

class TestNullShapes:
    """`geom is None or geom.is_empty` lost 56 features because 2 were null."""

    def test_nan_geometry_is_recognised_as_null(self):
        """The predicate, directly. NaN is what iterrows() hands back."""
        assert _is_null_geometry(float("nan")) is True
        assert _is_null_geometry(None) is True
        assert _is_null_geometry(LineString([(0, 0), (1, 1)])) is False

    def test_nan_geometry_does_not_abort_the_row_loop(self):
        """The regression, reproduced at the exact frame the parser walks.

        Before the fix this raised
        ``AttributeError: 'float' object has no attribute 'is_empty'`` out of
        ``_extract_features``, losing every feature in the file rather than
        the one that was null.
        """
        frame = pd.DataFrame({
            "FID": ["a", "b", "c"],
            "geometry": [UNGA_LINES[0], float("nan"), UNGA_LINES[1]],
        })
        features, empty_skipped, skipped_details = _extract_features(
            frame, "veins.shp", None
        )
        assert len(features) == 2
        assert empty_skipped == 1
        assert skipped_details[0]["reason"] == "null or empty geometry"

    def test_shapefile_with_null_shapes_parses_end_to_end(self, tmp_path):
        """Whole-file read: the nulls are counted, the rest survive."""
        shp = _write_shapefile(tmp_path, "veins", with_nulls=True)
        result = parse_spatial_file(shp)
        assert result.feature_count == 2
        assert result.empty_geom_skipped == 1
        assert result.source_crs == f"EPSG:{UNGA_EPSG}"


# ---------------------------------------------------------------------------
# Requirement 1 — no CRS is a refusal, not an assumption of EPSG:4326
# ---------------------------------------------------------------------------

class TestMissingCrsIsRefused:
    def test_shapefile_without_prj_is_flagged_not_assumed_4326(self, tmp_path):
        """The corruption, at its source.

        A .prj-less shapefile used to come back as EPSG:4326 with its metre
        eastings untouched, so ingest_spatial wrote POINT(400798 6117306) as
        SRID 4326 — longitude 400,798 degrees. PostGIS does not range-check,
        so it landed and the run was reported as having succeeded.
        """
        shp = _write_shapefile(tmp_path, "noprj")
        _drop_sidecar(shp, ".prj")

        result = parse_spatial_file(shp)

        assert result.crs_missing is True
        assert result.source_crs == ""
        assert "crs_required" in _codes(result)
        assert "prj_missing" in _codes(result)

    def test_refusal_warning_carries_a_detail_the_ui_can_render(self, tmp_path):
        """IngestionRuns.tsx reads detail -> code; a code-only warning renders bare."""
        shp = _write_shapefile(tmp_path, "noprj")
        _drop_sidecar(shp, ".prj")

        warning = _warning(parse_spatial_file(shp), "crs_required")

        assert warning["message"]
        assert "coordinate system" in warning["detail"]
        assert "EPSG" in warning["detail"]

    def test_features_are_left_in_native_units_when_the_crs_is_unknown(self, tmp_path):
        """Not reprojected, because there is nothing to reproject FROM.

        The features are still returned so the caller can report how much
        was refused; ``crs_missing`` is what stops them being written.
        """
        shp = _write_shapefile(tmp_path, "noprj")
        _drop_sidecar(shp, ".prj")

        result = parse_spatial_file(shp)

        assert result.features
        assert "400798" in result.features[0].geometry_wkt

    def test_declared_crs_is_kept_and_reprojected(self, tmp_path):
        """The control: a complete shapefile still lands on Unga Island."""
        result = parse_spatial_file(_write_shapefile(tmp_path, "veins"))

        assert result.crs_missing is False
        assert result.source_crs == f"EPSG:{UNGA_EPSG}"
        assert result.crs_confidence == 1.0
        lon, lat = result.features[0].geometry_wkt.split("(")[1].split(",")[0].split()
        assert float(lon) == pytest.approx(UNGA_LON, abs=0.01)
        assert float(lat) == pytest.approx(UNGA_LAT, abs=0.01)

    def test_empty_frame_does_not_claim_4326_either(self, tmp_path):
        """The fourth exit.

        The empty-frame early return hard-coded EPSG:4326 and dropped
        crs_confidence, so an empty .prj-less shapefile was indistinguishable
        from a WGS84 one. It now reaches the same decision as every other
        exit.
        """
        gdf = gpd.GeoDataFrame(
            {"FID": pd.Series([], dtype="object")},
            geometry=gpd.GeoSeries([], crs=f"EPSG:{UNGA_EPSG}"),
        )
        shp = os.path.join(str(tmp_path), "empty.shp")
        gdf.to_file(shp)
        _drop_sidecar(shp, ".prj")

        result = parse_spatial_file(shp)

        assert result.feature_count == 0
        assert result.crs_missing is True
        assert result.source_crs == ""


class TestNoCrsAllowlist:
    """Formats that legitimately carry no CRS must keep working."""

    def test_allowlist_membership(self):
        assert set(_NO_CRS_EXTENSIONS) == {".dxf", ".dgn", ".geojson", ".json"}

    def test_geojson_without_a_crs_member_is_wgs84_per_rfc_7946(self, tmp_path):
        path = tmp_path / "points.geojson"
        path.write_text(
            '{"type":"FeatureCollection","features":[{"type":"Feature",'
            '"properties":{"name":"olgen"},"geometry":{"type":"Point",'
            '"coordinates":[-160.5583,55.1924]}}]}',
            encoding="utf-8",
        )

        result = parse_spatial_file(str(path))

        assert result.crs_missing is False
        assert result.source_crs == "EPSG:4326"
        assert result.feature_count == 1

    def test_dgn_gets_4326_and_a_warning_rather_than_a_refusal(self):
        """MicroStation has no CRS concept, so absence is not a defect.

        .dgn was the format nobody had named: in _VECTOR_EXTENSIONS, in the
        Laravel categories, and with no CRS exemption — so it took the same
        arm as a .prj-less shapefile and suffered the identical corruption.
        Exercised through _resolve_crs because no .dgn writer exists.
        """
        frame = gpd.GeoDataFrame({"n": ["a"]}, geometry=[Point(1, 2)], crs=None)
        warnings_out: list[dict] = []

        _, decision = _resolve_crs(frame, ".dgn", "site.dgn", None, warnings_out)

        assert decision.missing is False
        assert decision.source_crs == "EPSG:4326"
        assert decision.confidence == 0.0
        assert [w["code"] for w in warnings_out] == ["dgn_no_crs"]
        assert warnings_out[0]["detail"]

    def test_dxf_keeps_its_placeholder_and_its_warning(self):
        """Pinned by test_dxf_blocks.py's integration class — do not change."""
        frame = gpd.GeoDataFrame({"n": ["a"]}, geometry=[Point(1, 2)], crs=None)
        warnings_out: list[dict] = []

        _, decision = _resolve_crs(frame, ".dxf", "plan.dxf", None, warnings_out)

        assert decision.missing is False
        assert decision.source_crs == "EPSG:4326"
        assert decision.confidence == 0.0
        assert [w["code"] for w in warnings_out] == ["dxf_no_crs"]
        assert warnings_out[0]["message"] == (
            "DXF files have no CRS; caller must georeference."
        )


# ---------------------------------------------------------------------------
# Requirement 2 — the source_epsg override
# ---------------------------------------------------------------------------

class TestSourceEpsgOverride:
    def test_override_is_applied_when_the_file_declares_nothing(self, tmp_path):
        shp = _write_shapefile(tmp_path, "noprj")
        _drop_sidecar(shp, ".prj")

        result = parse_spatial_file(shp, source_epsg=UNGA_EPSG)

        assert result.crs_missing is False
        assert result.crs_override_applied is True
        assert result.crs_override_epsg == UNGA_EPSG
        assert result.source_crs == f"EPSG:{UNGA_EPSG}"
        lon, lat = result.features[0].geometry_wkt.split("(")[1].split(",")[0].split()
        assert float(lon) == pytest.approx(UNGA_LON, abs=0.01)
        assert float(lat) == pytest.approx(UNGA_LAT, abs=0.01)

    def test_dxf_honours_the_override_like_every_other_format(self):
        """The DXF arm used to return before the source_epsg check — the
        wizard rendered an EPSG field on DXF rows, the API accepted the
        code, and the parser ignored it. A supplied override now takes the
        same declares-nothing path as a .prj-less shapefile: applied, with
        a measured fit, and no 'stored as assumed' warning."""
        # Unga Island (~UTM zone 4N metres), so the fit measures as real.
        frame = gpd.GeoDataFrame(
            {"n": ["a"]}, geometry=[Point(438000, 6120000)], crs=None,
        )
        warnings_out: list[dict] = []

        _, decision = _resolve_crs(
            frame, ".dxf", "plan.dxf", UNGA_EPSG, warnings_out,
        )

        assert decision.missing is False
        assert decision.source_crs == f"EPSG:{UNGA_EPSG}"
        assert decision.override_applied is True
        assert decision.override_epsg == UNGA_EPSG
        assert "dxf_no_crs" not in [w["code"] for w in warnings_out]

    def test_override_confidence_is_measured_not_asserted(self, tmp_path):
        """The human's claim is checked against the data, not trusted.

        EPSG:27700 is the British National Grid. The override is applied —
        the geologist asked for it — but the coordinates fall nowhere near
        Britain, so the confidence stored against it is the measured 0.0 and
        the low-confidence warning fires. Recording a flat 1.0 because a
        human typed the number would put the map's uncertainty ring on a
        position nobody has verified.
        """
        shp = _write_shapefile(tmp_path, "noprj")
        _drop_sidecar(shp, ".prj")

        result = parse_spatial_file(shp, source_epsg=27700)

        assert result.crs_override_applied is True
        assert result.crs_confidence == 0.0
        assert "crs_low_confidence" in _codes(result)

    def test_a_declared_crs_always_wins(self, tmp_path):
        """Precedence C3: never silently override a CRS the file states."""
        shp = _write_shapefile(tmp_path, "veins")

        result = parse_spatial_file(shp, source_epsg=32613)

        assert result.source_crs == f"EPSG:{UNGA_EPSG}"
        assert result.crs_override_applied is False
        assert result.crs_override_epsg is None
        ignored = _warning(result, "crs_override_ignored")
        assert "32613" in ignored["detail"]
        assert ignored["detail"]

    def test_an_override_agreeing_with_the_file_is_not_nagged_about(self, tmp_path):
        """A project-wide default EPSG must not make every good file 'partial'."""
        shp = _write_shapefile(tmp_path, "veins")

        result = parse_spatial_file(shp, source_epsg=UNGA_EPSG)

        assert "crs_override_ignored" not in _codes(result)

    @pytest.mark.parametrize("bad", [0, 1023, 32768, 999999, -26904])
    def test_out_of_range_epsg_is_refused_before_the_read(self, bad):
        """crs_epsg_native is CHECK-constrained to 1024..32767.

        An out-of-range code that survived the parse would fail the INSERT
        for every feature in the file — the whole-batch failure mode that a
        bad feature_type produced on 2026-08-20.
        """
        with pytest.raises(ValueError, match="1024"):
            _validate_source_epsg(bad)

    @pytest.mark.parametrize("bad", ["EPSG:26904", "26904", 26904.0, True])
    def test_a_crs_string_is_never_accepted(self, bad):
        with pytest.raises(ValueError, match="integer EPSG code"):
            _validate_source_epsg(bad)

    def test_valid_codes_pass_through(self):
        assert _validate_source_epsg(None) is None
        assert _validate_source_epsg(UNGA_EPSG) == UNGA_EPSG
        assert _validate_source_epsg(1024) == 1024
        assert _validate_source_epsg(32767) == 32767

    def test_a_bad_override_is_rejected_before_the_file_is_opened(self, tmp_path):
        """Validation must not depend on the file being readable."""
        with pytest.raises(ValueError):
            parse_spatial_file(str(tmp_path / "does-not-exist.shp"), source_epsg=1)


# ---------------------------------------------------------------------------
# Requirement 3 — SHAPE_RESTORE_SHX
# ---------------------------------------------------------------------------

class TestMissingShx:
    def test_a_shapefile_without_its_index_still_reads(self, tmp_path):
        """Set once at module import; asserted by behaviour, not by config read.

        Without SHAPE_RESTORE_SHX this raises
        ``DataSourceError: Unable to open …shx``. GDAL rebuilds the index
        from the .shp itself, so the sidecar is recoverable and refusing the
        file over it would reject data the pipeline can read.
        """
        shp = _write_shapefile(tmp_path, "noshx")
        _drop_sidecar(shp, ".shx")

        result = parse_spatial_file(shp)

        assert result.feature_count == 2
        assert result.source_crs == f"EPSG:{UNGA_EPSG}"

    def test_gdal_wrote_the_index_back(self, tmp_path):
        shp = _write_shapefile(tmp_path, "noshx")
        _drop_sidecar(shp, ".shx")

        parse_spatial_file(shp)

        assert os.path.isfile(os.path.splitext(shp)[0] + ".shx")


# ---------------------------------------------------------------------------
# C8 — case-insensitive sidecar resolution
# ---------------------------------------------------------------------------

class TestSidecarCaseRepair:
    def test_a_mis_cased_prj_is_found(self, tmp_path):
        """The RedStar delivery, exactly: lower-case .shp, Title-Case .prj.

        GDAL on Linux resolves sidecars case-sensitively, so without this the
        CRS refusal above would hard-reject a file whose coordinate system is
        sitting right there on disk.
        """
        shp = _write_shapefile(tmp_path, "drobeck_shumagin_veins")
        stem = os.path.splitext(shp)[0]
        os.rename(stem + ".prj", os.path.join(str(tmp_path), "Drobeck_Shumagin_Veins.prj"))

        result = parse_spatial_file(shp)

        assert result.crs_missing is False
        assert result.source_crs == f"EPSG:{UNGA_EPSG}"
        assert "crs_required" not in _codes(result)

    def test_the_repair_is_recorded_in_provenance(self, tmp_path):
        shp = _write_shapefile(tmp_path, "drobeck_shumagin_veins")
        stem = os.path.splitext(shp)[0]
        os.rename(stem + ".prj", os.path.join(str(tmp_path), "Drobeck_Shumagin_Veins.prj"))

        result = parse_spatial_file(shp)

        assert result.provenance["sidecars_case_repaired"] == [
            "drobeck_shumagin_veins.prj"
        ]

    def test_the_original_sidecar_is_copied_not_renamed(self, tmp_path):
        """A rename would destroy the delivery if anything else referenced it."""
        shp = _write_shapefile(tmp_path, "veins")
        stem = os.path.splitext(shp)[0]
        os.rename(stem + ".prj", os.path.join(str(tmp_path), "VEINS.PRJ"))

        parse_spatial_file(shp)

        assert os.path.isfile(os.path.join(str(tmp_path), "VEINS.PRJ"))
        assert os.path.isfile(stem + ".prj")

    def test_the_whole_redstar_delivery_parses(self, tmp_path):
        """Four faults in one file, which is how it arrived.

        No .shx, no .dbf, a Title-Case .prj, and two ESRI Null shapes. Before
        this change set the read raised on the missing .shx; with the .shx
        restored it read as EPSG:4326 and put the veins at longitude 400,798;
        and with the CRS recovered it still died on the null shapes.
        """
        shp = _write_shapefile(tmp_path, "drobeck_shumagin_veins", with_nulls=True)
        stem = os.path.splitext(shp)[0]
        _drop_sidecar(shp, ".shx")
        _drop_sidecar(shp, ".dbf")
        os.rename(stem + ".prj", os.path.join(str(tmp_path), "Drobeck_Shumagin_Veins.prj"))

        result = parse_spatial_file(shp)

        assert result.crs_missing is False
        assert result.source_crs == f"EPSG:{UNGA_EPSG}"
        assert result.feature_count == 2
        assert result.empty_geom_skipped == 1
        assert _codes(result) == ["dbf_missing"]
        lon, lat = result.features[0].geometry_wkt.split("(")[1].split(",")[0].split()
        assert float(lon) == pytest.approx(UNGA_LON, abs=0.01)
        assert float(lat) == pytest.approx(UNGA_LAT, abs=0.01)


# ---------------------------------------------------------------------------
# C9 — a missing .dbf is a degraded ingest, not a clean one
# ---------------------------------------------------------------------------

class TestMissingDbf:
    def test_missing_dbf_is_reported(self, tmp_path):
        """GDAL reads the file happily as columns ['geometry'] and says nothing.

        No in-band signal can distinguish "no .dbf" from "a .dbf with one
        useless column" — a dBASE table always has at least one field — so
        the discriminator is a sidecar stat, mirroring the .prj check.
        """
        shp = _write_shapefile(tmp_path, "veins")
        _drop_sidecar(shp, ".dbf")

        result = parse_spatial_file(shp)

        assert "dbf_missing" in _codes(result)
        assert result.feature_count == 2

    def test_the_warning_carries_both_message_and_detail(self, tmp_path):
        shp = _write_shapefile(tmp_path, "veins")
        _drop_sidecar(shp, ".dbf")

        warning = _warning(parse_spatial_file(shp), "dbf_missing")

        assert "veins.shp" in warning["message"]
        assert "attribute" in warning["detail"]
        assert warning["context"]["expected_dbf"].endswith("veins.dbf")

    def test_a_complete_shapefile_is_not_flagged(self, tmp_path):
        result = parse_spatial_file(_write_shapefile(tmp_path, "veins"))
        assert "dbf_missing" not in _codes(result)

    def test_a_mis_cased_dbf_counts_as_present(self, tmp_path):
        """Case repair runs before the stat, or the warning would be a lie."""
        shp = _write_shapefile(tmp_path, "veins")
        stem = os.path.splitext(shp)[0]
        os.rename(stem + ".dbf", os.path.join(str(tmp_path), "VEINS.DBF"))

        result = parse_spatial_file(shp)

        assert "dbf_missing" not in _codes(result)


# ---------------------------------------------------------------------------
# Requirement 5 — MapInfo
# ---------------------------------------------------------------------------

def _mapinfo_available() -> bool:
    import pyogrio

    return "MapInfo File" in pyogrio.list_drivers()


mapinfo = pytest.mark.skipif(
    not _mapinfo_available(), reason="GDAL built without the MapInfo File driver"
)


@mapinfo
class TestMapInfo:
    def _write_tab(self, directory, stem="survey"):
        gdf = gpd.GeoDataFrame(
            {"name": ["olgen", "sitka"]},
            geometry=[Point(400798, 6117306), Point(401500, 6118000)],
            crs=f"EPSG:{UNGA_EPSG}",
        )
        path = os.path.join(str(directory), f"{stem}.tab")
        gdf.to_file(path, driver="MapInfo File")
        return path

    def _write_mif(self, directory, stem="survey"):
        gdf = gpd.GeoDataFrame(
            {"name": ["olgen", "sitka"]},
            geometry=[Point(400798, 6117306), Point(401500, 6118000)],
            crs=f"EPSG:{UNGA_EPSG}",
        )
        path = os.path.join(str(directory), f"{stem}.mif")
        gdf.to_file(path, driver="MapInfo File")
        return path

    def test_only_tab_and_mif_are_entry_points(self):
        """.dat/.map/.id/.ind/.mid are sidecars.

        Opening a .mid directly SUCCEEDS, so listing it as an entry point
        would ingest a MIF/MID pair twice — the double-ingest the shapefile
        sidecar exclusion already exists to prevent. And .dat is claimed by
        the retired 'xyz' category upstream, so routing it here would send a
        stray XYZ grid to the spatial parser.
        """
        assert _VECTOR_EXTENSIONS[".tab"] == "MapInfo File"
        assert _VECTOR_EXTENSIONS[".mif"] == "MapInfo File"
        for sidecar in (".dat", ".map", ".id", ".ind", ".mid"):
            assert sidecar not in _VECTOR_EXTENSIONS

    def test_a_complete_tab_parses_with_its_crs(self, tmp_path):
        result = parse_spatial_file(self._write_tab(tmp_path))

        assert result.source_format == "mapinfo_tab"
        assert result.crs_missing is False
        assert result.source_crs.startswith("EPSG:") or "UTM" in result.source_crs
        assert result.feature_count == 2

    def test_a_complete_mif_parses_with_its_attributes(self, tmp_path):
        result = parse_spatial_file(self._write_mif(tmp_path))

        assert result.source_format == "mapinfo_mif"
        assert result.feature_count == 2
        assert "mid_missing" not in _codes(result)
        assert result.features[0].properties.get("name") in {"olgen", "sitka"}

    def test_a_mif_without_its_mid_is_flagged(self, tmp_path):
        """Measured: it does NOT fail. It reads with every attribute None.

        Same silent-degradation class as a .shp without its .dbf, and just
        as invisible in band.
        """
        mif = self._write_mif(tmp_path)
        os.remove(os.path.splitext(mif)[0] + ".mid")

        result = parse_spatial_file(mif)

        warning = _warning(result, "mid_missing")
        assert warning["detail"]
        assert result.feature_count == 2

    def test_a_tab_without_its_map_and_dat_says_so(self, tmp_path):
        """GDAL raises DataSourceError; the geologist gets a stack trace.

        A NATIVE .tab header carries no CoordSys of its own — the CRS lives
        in the .map — so this is a CRS loss as well as a data loss.
        """
        tab = self._write_tab(tmp_path)
        for ext in (".map", ".dat", ".id"):
            sidecar = os.path.splitext(tab)[0] + ext
            if os.path.isfile(sidecar):
                os.remove(sidecar)

        with pytest.raises(FileNotFoundError, match=r"\.map"):
            parse_spatial_file(tab)

    def test_a_raster_tab_is_refused_by_name(self, tmp_path):
        """RedStar ships one: a georeferencing header for a .tif, not vectors."""
        tab = tmp_path / "BMGC_UngaIsSouth_Geology_1990.tab"
        tab.write_text(
            '!table\n!version 300\n!charset WindowsLatin1\n\n'
            'Definition Table\n  File "geology.tif"\n  Type "RASTER"\n'
            '  (400000, 6100000) (0, 0) Label "Pt 1",\n'
            '  CoordSys Earth Projection 8, 74, "m", -159, 0, 0.9996, 500000, 0\n',
            encoding="utf-8",
        )

        with pytest.raises(NotImplementedError, match="RASTER"):
            parse_spatial_file(str(tab))

    def test_mis_cased_mapinfo_sidecars_are_repaired(self, tmp_path):
        """RedStar's MapInfo tables ship upper-case .DAT / .MAP siblings."""
        tab = self._write_tab(tmp_path)
        stem = os.path.splitext(tab)[0]
        for ext in (".dat", ".map", ".id"):
            if os.path.isfile(stem + ext):
                os.rename(stem + ext, stem + ext.upper())

        result = parse_spatial_file(tab)

        assert result.feature_count == 2
        assert sorted(result.provenance["sidecars_case_repaired"]) == [
            "survey.dat", "survey.id", "survey.map",
        ]


# ---------------------------------------------------------------------------
# The helper the four exits share
# ---------------------------------------------------------------------------

class TestResolveCrsHelper:
    """One decision point. There used to be four, and they disagreed."""

    def test_a_declared_crs_is_returned_untouched(self):
        frame = gpd.GeoDataFrame(
            {"n": ["a"]}, geometry=[Point(400798, 6117306)], crs=f"EPSG:{UNGA_EPSG}"
        )
        _, decision = _resolve_crs(frame, ".shp", "veins.shp", None, [])

        assert decision.source_crs == f"EPSG:{UNGA_EPSG}"
        assert decision.missing is False
        assert decision.override_applied is False

    def test_an_unknown_crs_on_an_unlisted_format_is_missing(self):
        frame = gpd.GeoDataFrame({"n": ["a"]}, geometry=[Point(1, 2)], crs=None)
        warnings_out: list[dict] = []

        gdf, decision = _resolve_crs(frame, ".gpkg", "site.gpkg", None, warnings_out)

        assert decision.missing is True
        assert decision.source_crs == ""
        assert gdf.crs is None
        assert [w["code"] for w in warnings_out] == ["crs_required"]

    def test_the_override_arm_assigns_the_crs_to_the_frame(self):
        frame = gpd.GeoDataFrame(
            {"n": ["a"]}, geometry=[Point(400798, 6117306)], crs=None
        )
        gdf, decision = _resolve_crs(frame, ".shp", "veins.shp", UNGA_EPSG, [])

        assert gdf.crs.to_epsg() == UNGA_EPSG
        assert decision.override_applied is True
        assert decision.confidence == 1.0


# ---------------------------------------------------------------------------
# Sanity: the fixtures are what the tests claim they are
# ---------------------------------------------------------------------------

def test_the_fixture_really_is_projected_utm(tmp_path):
    """If this ever reads as degrees the rest of the file proves nothing."""
    shp = _write_shapefile(tmp_path, "veins")
    raw = gpd.read_file(shp)

    assert raw.crs.to_epsg() == UNGA_EPSG
    minx, miny, _, _ = raw.total_bounds
    assert minx > 180
    assert miny > 90
