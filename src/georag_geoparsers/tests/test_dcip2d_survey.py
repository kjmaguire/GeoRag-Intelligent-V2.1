"""Tests for assembling a DCIP2D export into one persistable survey record.

Two kinds of claim are asserted here and they fail differently.

The MEASURED ones were taken by hand from the Centennial L3750N export and the
grid's station file before ``dcip2d_survey`` was written. The load-bearing one
is that the station join does NOT land: the station file carries 24 rows and
every one of them is on line 4250 N, so a survey of line 3750 N joins to
nothing. That is asserted three ways rather than commented, because the failure
mode it guards is a future refactor "fixing" the join by relaxing the line
match — at which point 24 stations from a line 500 m away would silently become
this survey's coordinates and every section would be drawn in the wrong place.

The CONTRACT ones read the destination's own SQL and the migration that owns
its CHECK constraint. ``test_payload_matches_the_writers_sql_parameters`` is
the one that matters: the payload this module emits is consumed by
``silver_geophysics.INSERT_SURVEY_SQL`` and neither side imports the other, so
a key renamed on either side would otherwise surface as a NULL column in
production rather than as a red test.

The synthetic cases at the bottom cover malformed exports, which the delivery
does not contain and which therefore cannot be measured from it.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np
import pytest

from georag_geoparsers.dcip2d_parser import read_dcip2d_model
from georag_geoparsers.dcip2d_survey import (
    PROJECTION_EPSG,
    QUANTITY_UNKNOWN,
    SURVEY_TYPE,
    read_dcip2d_stations,
    read_dcip2d_survey,
    stations_from_rows,
)

REDSTAR = Path("C:/Users/GeoRAG/Desktop/RedStar/Centennial/Geophysics/IP/June 19")
EXPORT_DIR = REDSTAR / "L3750N" / "export"
STATION_FILE = REDSTAR / "Locations" / "export_UTM.xls"

#: Applied to the classes that read the delivery, NOT to the whole module — the
#: synthetic and contract tests are pure and must keep running on a machine
#: that does not have the RedStar hand-off mounted.
needs_export = pytest.mark.skipif(
    not (EXPORT_DIR.is_dir() and STATION_FILE.is_file()),
    reason=f"RedStar DCIP2D delivery not present under {REDSTAR}",
)

#: The Laravel/Dagster trees, resolved relative to this file. Skipped rather
#: than failed when absent, because georag_geoparsers is installable alone.
REPO_ROOT = Path(__file__).resolve().parents[3]
WRITER = REPO_ROOT / "src" / "dagster" / "georag_dagster" / "assets" / "silver_geophysics.py"
MIGRATION = (
    REPO_ROOT / "database" / "migrations"
    / "2026_05_21_030000_create_silver_geophysics_surveys.php"
)

#: Supplied by the Dagster asset itself (from its Config and a uuid4), never by
#: the payload, so they are excluded when the two sides are compared.
WRITER_SUPPLIED_PARAMS = frozenset({"survey_id", "workspace_id", "project_id"})


# ---------------------------------------------------------------------------
# Synthetic-export helpers
# ---------------------------------------------------------------------------

STATION_HEADER = (
    "Grids_Name", "LineNumber", "Series", "StationNumber", "Projections_Name",
    "X", "Y", "Z", "K1", "LineType", "LineCount", "StationName",
)


def _write_observed(path: Path, title: str, array_type: str, rows: list[str]) -> None:
    path.write_text("\n".join([title, array_type, *rows]) + "\n", encoding="ascii")


def _write_model(path: Path, nx: int, nz: int, values: list[float]) -> None:
    body = " ".join(f"{v:.6g}" for v in values)
    path.write_text(f"  {nx}   {nz}\n{body}\n", encoding="ascii")


def _station_rows(*rows: tuple) -> list[dict]:
    """Station rows in the shape ``read_sheet_rows`` produces.

    Fed to ``stations_from_rows`` rather than written to a file. Nothing in
    this dependency set can WRITE a legacy .xls (xlrd only reads), and the
    .xlsx branch of ``read_sheet_rows`` goes through ``pl.read_excel``, which
    on polars 1.40 needs the calamine engine (``fastexcel``) that is not
    installed here — so a synthetic spreadsheet would test the host's package
    list, not this module. The file path itself is covered end to end by
    ``TestStationFile`` against the real .xls delivery.

    Values are strings because that is what the .xls loader returns for EVERY
    cell — the coercion trap ``_as_float`` exists for.
    """
    return [
        dict(zip(
            STATION_HEADER,
            [None if v is None else str(v) for v in row],
            strict=True,   # a short row would silently drop trailing columns
        ))
        for row in rows
    ]


def _minimal_export(root: Path, *, title: str = "Vp - Line 1200 N", dirname: str = "L1200N") -> Path:
    """A two-reading, one-model export that parses; the caller then breaks it."""
    export = root / dirname / "export"
    export.mkdir(parents=True)
    _write_observed(
        export / "SYN_Vp_XYZ.rdtmd",
        title,
        "Pole-Dipole",
        ["  100.0  100.0  150.0  200.0  0.5", "  150.0  150.0  200.0  250.0  0.25"],
    )
    _write_model(export / "dcinv2d.010", 2, 2, [0.01, 0.02, 0.03, 0.04])
    return export


# ---------------------------------------------------------------------------
# The delivery: what the export actually holds
# ---------------------------------------------------------------------------

@needs_export
class TestExportAssembly:
    @pytest.fixture(scope="class")
    def survey(self):
        return read_dcip2d_survey(EXPORT_DIR, station_file=STATION_FILE)

    def test_line_identity_comes_from_the_file_titles(self, survey):
        assert survey.line_id == "L3750N"
        assert survey.line_number == 3750.0
        assert survey.series == "N"

    def test_array_type_is_unanimous_across_all_four_splits(self, survey):
        assert survey.array_type == "Pole-Dipole"
        assert {split.array_type for split in survey.observed} == {"Pole-Dipole"}

    def test_one_of_four_splits_carries_all_96_readings(self, survey):
        assert len(survey.observed) == 4
        assert survey.observation_count == 96
        assert survey.stub_count == 3
        carriers = [s for s in survey.observed if not s.is_stub]
        assert [s.filename for s in carriers] == ["CEN_L3750_Vp_XYZ.rdtmd"]

    def test_quantity_is_read_from_the_title_not_the_filename(self, survey):
        """``_Vp_`` and ``_Mx_`` are in the names; the titles are what is read."""
        quantities = {s.filename: s.quantity for s in survey.observed}
        assert quantities["CEN_L3750_Vp_XYZ.rdtmd"] == "normalized_potential"
        assert quantities["CEN_L3750_Mx_XYZ.rdtmm"] == "chargeability"
        assert QUANTITY_UNKNOWN not in quantities.values()

    def test_seven_models_on_one_mesh(self, survey):
        assert len(survey.models) == 7
        assert {(m.model.nx, m.model.nz) for m in survey.models} == {(55, 20)}
        assert {m.family for m in survey.models} == {"dcinv2d", "ipinv2d"}

    def test_the_two_final_models_are_the_run_products(self, survey):
        assert survey.final_conductivity.filename == "dcinv2d.030"
        assert survey.final_chargeability.filename == "ipinv2d.chg"

    def test_chg_is_not_the_last_numbered_iteration(self, survey):
        """Why ``.chg`` is preferred explicitly instead of taking max(iteration).

        ``ipinv2d.016`` is the highest-numbered ipinv2d file in the export, so
        a max-by-iteration rule would return it — and it is a DIFFERENT model.
        Reporting it as the final chargeability would understate the strongest
        anomaly on the line by 9 mV/V.
        """
        numbered = [m for m in survey.models if m.family == "ipinv2d" and m.iteration is not None]
        assert max(m.iteration for m in numbered) == 16
        assert survey.final_chargeability.iteration is None
        chg = survey.final_chargeability.model
        last_numbered = read_dcip2d_model(EXPORT_DIR / "ipinv2d.016")
        assert not np.array_equal(chg.values, last_numbered.values)
        assert chg.values[~chg.air_mask].max() == pytest.approx(81.8788)
        assert last_numbered.values[~last_numbered.air_mask].max() == pytest.approx(72.7252)

    def test_the_control_file_is_read(self, survey):
        assert survey.manifest["mesh"] == "dcinv2d.msh"
        assert survey.manifest["conductivity file"] == "dcinv2d.con"


# ---------------------------------------------------------------------------
# The station file — and the join that does not land
# ---------------------------------------------------------------------------

@needs_export
class TestStationFile:
    @pytest.fixture(scope="class")
    def stations(self):
        return read_dcip2d_stations(STATION_FILE)

    def test_twenty_four_stations_all_read(self, stations):
        assert len(stations) == 24

    def test_every_station_is_on_line_4250_not_3750(self, stations):
        """THE finding. The delivery pairs a line 3750 N survey with a line
        4250 N station list, and nothing in either file says so."""
        assert {s.line_number for s in stations} == {4250.0}
        assert 3750.0 not in {s.line_number for s in stations}

    def test_stations_are_a_fifty_metre_picket_grid(self, stations):
        numbers = sorted(s.station_number for s in stations)
        assert numbers[0] == 4600.0
        assert numbers[-1] == 5750.0
        # strict=False: pairing a list with its own tail is one shorter by
        # construction, which is the point — 24 stations give 23 gaps.
        assert {b - a for a, b in zip(numbers, numbers[1:], strict=False)} == {50.0}

    def test_coordinates_are_real_and_carry_elevation(self, stations):
        first = stations[0]
        assert (first.easting, first.northing) == (404512.845, 6130168.758)
        assert first.elevation == pytest.approx(73.18555)
        assert first.station_name == "4250N 4600E"
        assert all(s.elevation is not None for s in stations)

    def test_the_projection_names_no_zone_and_no_datum(self, stations):
        """Blocker 2: 'Trivial UTM' is a family, not a coordinate system."""
        assert {s.projection_name for s in stations} == {"Trivial UTM"}
        assert PROJECTION_EPSG == {}, (
            "PROJECTION_EPSG gained an entry. Every value in it turns an "
            "easting/northing pair into a place on Earth, so an entry added "
            "without a delivery that carries a zone AND a datum is a guess "
            "that will be drawn on a map as fact."
        )


@needs_export
class TestChainageJoin:
    @pytest.fixture(scope="class")
    def survey(self):
        return read_dcip2d_survey(EXPORT_DIR, station_file=STATION_FILE)

    def test_the_survey_is_not_georeferenced(self, survey):
        assert survey.is_georeferenced is False
        assert survey.join.resolved is False
        assert survey.mesh.resolved is False

    def test_the_join_reports_all_three_blockers_not_just_the_first(self, survey):
        """A caller told only the first would fix it and hit the next on re-run."""
        reasons = " | ".join(survey.join.unresolved_reasons)
        assert len(survey.join.unresolved_reasons) == 3
        assert "no rows for line 3750" in reasons
        assert "4250" in reasons
        assert "Trivial UTM" in reasons and "no EPSG code" in reasons
        assert "0 of 22 electrode chainages" in reasons

    def test_no_station_is_on_the_surveyed_line(self, survey):
        assert survey.join.stations_read == 24
        assert survey.join.stations_on_line == ()
        assert survey.join.lines_in_station_file == (4250.0,)

    def test_no_electrode_chainage_is_a_station_number(self, survey):
        """Blocker 3: the two axes do not share values, so this is not a lookup."""
        assert len(survey.join.chainages) == 22
        assert survey.join.exact_matches == ()
        assert set(survey.join.chainages).isdisjoint(survey.join.grid_station_numbers)

    def test_chainage_spacings_are_not_the_picket_interval(self, survey):
        spacings = np.diff(np.asarray(survey.join.chainages))
        assert spacings.min() == pytest.approx(34.89)
        assert spacings.max() == pytest.approx(57.58)
        assert 50.0 not in set(np.round(spacings, 2))

    def test_three_chainages_fall_below_the_lowest_picket(self, survey):
        """Even an interpolation would have to EXTRAPOLATE for these three."""
        lowest_picket = min(survey.join.grid_station_numbers)
        assert lowest_picket == 4600.0
        below = [c for c in survey.join.chainages if c < lowest_picket]
        assert below == [4500.00, 4547.55, 4596.62]

    def test_no_epsg_is_invented(self, survey):
        assert survey.join.crs_epsg is None
        assert survey.join.projection_names == ("Trivial UTM",)

    def test_the_mesh_files_are_named_but_absent(self, survey):
        """Blocker 4: the models cannot be placed even on the chainage axis."""
        assert survey.mesh.mesh_file == "dcinv2d.msh"
        assert survey.mesh.mesh_delivered is False
        assert survey.mesh.topography_file.endswith("L3750dz.txt")
        assert survey.mesh.topography_delivered is False
        assert len(survey.mesh.unresolved_reasons) == 2


# ---------------------------------------------------------------------------
# The destination
# ---------------------------------------------------------------------------

@needs_export
class TestGeophysicsSurveyPayload:
    @pytest.fixture(scope="class")
    def payload(self):
        survey = read_dcip2d_survey(EXPORT_DIR, station_file=STATION_FILE)
        return survey.to_geophysics_survey_payload(
            "Centennial IP L3750N DCIP2D",
            contractor="AES",
            acquisition_date="2005-06-19",
        )

    def test_survey_type_is_the_one_the_check_constraint_allows(self, payload):
        assert payload["survey_type"] == SURVEY_TYPE == "IP"

    def test_the_line_is_carried_in_line_ids(self, payload):
        assert payload["line_ids"] == ["L3750N"]

    def test_the_georeference_is_left_null_rather_than_guessed(self, payload):
        assert payload["aoi_wkt"] is None
        assert payload["crs_epsg"] is None

    def test_processing_notes_says_WHY_the_georeference_is_null(self, payload):
        """A NULL aoi_geom is indistinguishable from nobody having tried.

        These two columns are the only place a person finds out that the
        survey was read completely and still cannot be placed.
        """
        notes = payload["processing_notes"]
        assert "Georeference: NOT RESOLVED" in notes
        assert "Mesh: NOT RESOLVED" in notes
        assert "no rows for line 3750" in notes

    def test_processing_notes_carries_the_measured_shape(self, payload):
        notes = payload["processing_notes"]
        assert "line L3750N, Pole-Dipole array" in notes
        assert "96 readings in 1 of 4 geometry splits" in notes
        assert "3 split(s) exported with zero readings" in notes
        assert "22 positions on a 1-D chainage 4500.00-5494.88 m" in notes
        assert "7 files on mesh 55x20 (one shared mesh), 98 air cell(s)" in notes

    def test_anomaly_summary_excludes_the_air_padding(self, payload):
        """Air is -1e30 in the ip models and a pinned near-zero in the dc ones.

        Either one left in wrecks the range silently, which is the whole
        reason ``air_mask`` is carried this far.
        """
        summary = payload["anomaly_summary"]
        assert "1002 earth cells of 1100" in summary
        assert "0.000-81.879 mV/V, median 6.286" in summary
        assert "81.6-3381.7 ohm-m, median 249.9" in summary
        assert "e+30" not in summary and "1e30" not in summary

    def test_anomaly_summary_refuses_to_present_an_unplaceable_section(self, payload):
        """A chargeability high with a number on it reads as a drill target."""
        assert "NOT georeferenced" in payload["anomaly_summary"]

    def test_caller_supplied_fields_are_not_invented(self, payload):
        """contractor and acquisition_date are passed in, never derived.

        The only date in the export is a folder called 'June 19' and a year
        inside a Windows job path in IP.inp — which records when the INVERSION
        was run, not when the crew was on the ground.
        """
        assert payload["contractor"] == "AES"
        assert payload["acquisition_date"] == "2005-06-19"
        bare = read_dcip2d_survey(EXPORT_DIR).to_geophysics_survey_payload("x")
        assert bare["contractor"] is None
        assert bare["acquisition_date"] is None


class TestWriterContract:
    """The payload against the SQL and the CHECK that will actually receive it."""

    @pytest.mark.skipif(not WRITER.is_file(), reason=f"{WRITER} not present")
    def test_payload_matches_the_writers_sql_parameters(self, tmp_path):
        """Every ``%(name)s`` the writer binds must be a key this module emits.

        Neither module imports the other, so a rename on either side is
        invisible until a column lands NULL in production. Both directions are
        checked: a key the writer does not bind is dead weight, and a parameter
        the payload does not supply is a KeyError at insert time.
        """
        bound = set(re.findall(r"%\((\w+)\)s", WRITER.read_text(encoding="utf-8")))
        assert bound, "found no bound parameters in the writer's SQL"

        export = _minimal_export(tmp_path)
        payload = read_dcip2d_survey(export).to_geophysics_survey_payload("synthetic")

        assert bound - WRITER_SUPPLIED_PARAMS == set(payload)

    @pytest.mark.skipif(not MIGRATION.is_file(), reason=f"{MIGRATION} not present")
    def test_survey_type_passes_the_check_constraint(self):
        """``chk_geophysics_surveys_type`` rejects the whole INSERT otherwise."""
        source = MIGRATION.read_text(encoding="utf-8")
        match = re.search(
            r"CHECK \(survey_type IN \((.*?)\)\)", source, re.DOTALL,
        )
        assert match is not None, (
            "could not find the survey_type CHECK in the migration; the "
            "constraint moved and this test no longer proves anything."
        )
        allowed = set(re.findall(r"'([^']+)'", match.group(1)))
        assert SURVEY_TYPE in allowed, f"{SURVEY_TYPE!r} not in {sorted(allowed)}"


# ---------------------------------------------------------------------------
# Synthetic — malformed and ambiguous exports
# ---------------------------------------------------------------------------

class TestSyntheticExports:
    def test_input_models_are_not_mistaken_for_results(self, tmp_path):
        """``dcinv2d.con`` is an INPUT in the identical format to a result.

        It is what IP.inp names as the conductivity the ip inversion started
        from. Reading it as a seventh model would report an inversion's input
        as its output, and nothing about the file says which it is — only the
        stage in its name does.
        """
        export = _minimal_export(tmp_path)
        _write_model(export / "dcinv2d.con", 2, 2, [0.9, 0.9, 0.9, 0.9])
        (export / "dcinv2d.msh").write_text("2 2\n0 1 2\n0 1 2\n", encoding="ascii")

        survey = read_dcip2d_survey(export)
        assert [m.filename for m in survey.models] == ["dcinv2d.010"]

    def test_directory_and_titles_naming_different_lines_raises(self, tmp_path):
        """Filing a survey under a line it is not on joins it to wrong stations."""
        export = _minimal_export(tmp_path, title="Vp - Line 1200 N", dirname="L9999N")
        with pytest.raises(ValueError, match="directory is named for line L9999N"):
            read_dcip2d_survey(export)

    def test_directory_that_is_not_a_line_name_is_not_cross_checked(self, tmp_path):
        """An export under a plain project folder asserts nothing about a line."""
        export = _minimal_export(tmp_path, dirname="Centennial")
        assert read_dcip2d_survey(export).line_id == "L1200N"

    def test_line_falls_back_to_the_directory_when_no_title_carries_one(self, tmp_path):
        export = _minimal_export(tmp_path, title="Normalized Potential", dirname="L1200N")
        survey = read_dcip2d_survey(export)
        assert survey.line_id == "L1200N"
        assert survey.observed[0].quantity == "normalized_potential"

    def test_no_line_anywhere_raises(self, tmp_path):
        export = _minimal_export(tmp_path, title="Normalized Potential", dirname="export_v2")
        with pytest.raises(ValueError, match="no line identity"):
            read_dcip2d_survey(export)

    def test_export_without_observed_data_raises(self, tmp_path):
        export = tmp_path / "L1200N" / "export"
        export.mkdir(parents=True)
        _write_model(export / "dcinv2d.010", 2, 2, [1.0, 2.0, 3.0, 4.0])
        with pytest.raises(ValueError, match="holds no observed-data file"):
            read_dcip2d_survey(export)

    def test_splits_disagreeing_about_the_array_type_raises(self, tmp_path):
        """The array decides how the four electrode columns are read."""
        export = _minimal_export(tmp_path)
        _write_observed(
            export / "SYN_Mx_XYZ.rdtmm", "Mx - Line 1200 N", "Dipole-Dipole", [],
        )
        with pytest.raises(ValueError, match="disagree about the array type"):
            read_dcip2d_survey(export)

    def test_two_control_files_and_no_IP_inp_raises(self, tmp_path):
        export = _minimal_export(tmp_path)
        (export / "run_a.inp").write_text("a.obs ! obs file\n", encoding="ascii")
        (export / "run_b.inp").write_text("b.obs ! obs file\n", encoding="ascii")
        with pytest.raises(ValueError, match="control files"):
            read_dcip2d_survey(export)

    def test_missing_export_directory_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_dcip2d_survey(tmp_path / "absent")

    def test_an_unrecognised_quantity_is_recorded_not_dropped(self, tmp_path):
        export = _minimal_export(tmp_path, title="Self Potential - Line 1200 N")
        survey = read_dcip2d_survey(export)
        assert survey.observed[0].quantity == QUANTITY_UNKNOWN
        assert survey.observation_count == 2

    def test_mesh_is_resolved_when_both_files_are_delivered(self, tmp_path):
        """The mesh verdict is a real check, not a constant that always fails."""
        export = _minimal_export(tmp_path)
        (export / "IP.inp").write_text(
            "dcinv2d.msh  ! mesh\ntopo.txt  ! topography\nNULL  ! w.dat\n",
            encoding="ascii",
        )
        (export / "dcinv2d.msh").write_text("2 2\n", encoding="ascii")
        (export / "topo.txt").write_text("0 0\n", encoding="ascii")

        survey = read_dcip2d_survey(export)
        assert survey.mesh.resolved is True
        assert survey.mesh.unresolved_reasons == ()
        assert "Mesh: resolved." in survey.processing_notes

    def test_a_null_topography_line_is_not_reported_as_missing(self, tmp_path):
        """``NULL`` means the operator supplied none, not that one was lost."""
        export = _minimal_export(tmp_path)
        (export / "IP.inp").write_text(
            "dcinv2d.msh  ! mesh\nNULL  ! topography\n", encoding="ascii",
        )
        (export / "dcinv2d.msh").write_text("2 2\n", encoding="ascii")

        survey = read_dcip2d_survey(export)
        assert survey.mesh.topography_file is None
        assert survey.mesh.resolved is True


class TestNoStationFile:
    def test_omitting_the_station_file_gives_one_honest_reason(self, tmp_path):
        """Not auto-discovered: a station file found by convention and
        belonging to another grid is a confident, wrong georeference."""
        survey = read_dcip2d_survey(_minimal_export(tmp_path))
        assert survey.join.station_file is None
        assert survey.join.unresolved_reasons == ("no station file was supplied",)
        assert survey.join.stations_read == 0
        assert survey.join.chainages == (100.0, 150.0, 200.0, 250.0)


class TestStationRowRejection:
    def test_missing_a_required_column_raises(self):
        """Guessing which column meant 'line' files a survey under the wrong one."""
        rows = [{"Grids_Name": "1", "StationNumber": "4600", "X": "404512.845",
                 "Y": "6130168.758"}]
        with pytest.raises(ValueError, match=r"missing required column\(s\) \['LineNumber'\]"):
            stations_from_rows(rows, "no_line.xls")

    def test_no_rows_at_all_raises(self):
        with pytest.raises(ValueError, match="has no rows"):
            stations_from_rows([], "empty.xls")

    def test_a_non_numeric_coordinate_is_rejected_loudly_not_dropped(self, caplog):
        rows = _station_rows(
            ("1", 3750.0, "N", 4600.0, "UTM", 404512.845, 6130168.758,
             73.2, 4.0, "Cross Line", 1.0, "3750N 4600E"),
            ("1", 3750.0, "N", 4650.0, "UTM", "n/a", 6130172.774,
             80.9, 4.0, "Cross Line", 1.0, "3750N 4650E"),
        )
        with caplog.at_level(logging.WARNING, logger="georag_geoparsers.dcip2d_survey"):
            stations = stations_from_rows(rows, "one_bad.xls")

        assert len(stations) == 1
        assert stations[0].station_number == 4600.0
        assert "row 2 rejected" in caplog.text
        assert "'X'" in caplog.text
        assert "n/a" in caplog.text

    def test_a_station_without_an_elevation_is_still_a_station(self):
        """Z is not required: dropping a position over a missing height is loss."""
        rows = _station_rows(
            ("1", 3750.0, "N", 4600.0, "UTM", 404512.845, 6130168.758,
             None, 4.0, "Cross Line", 1.0, "3750N 4600E"),
        )
        stations = stations_from_rows(rows, "no_z.xls")
        assert len(stations) == 1
        assert stations[0].elevation is None
        assert stations[0].easting == 404512.845

    def test_the_loader_hands_back_strings_and_they_still_compare(self):
        """'4250.0' == 4250 is False and int('4250.0') raises.

        Every cell of a legacy .xls arrives as text, so a line match written
        without coercion silently finds nothing in a file that is entirely
        that line — the same shape as the real join failure, from a bug.
        """
        stations = stations_from_rows(
            _station_rows(("1", 4250.0, "N", 4600.0, "UTM", 1.0, 2.0,
                           3.0, 4.0, "Cross Line", 1.0, "4250N 4600E")),
            "strings.xls",
        )
        assert stations[0].line_number == 4250.0
        assert isinstance(stations[0].line_number, float)


@needs_export
class TestJoinLandsWhenTheDataAllows:
    """The join is not hard-coded to fail — it fails on THIS delivery.

    Without these, every unresolved-reason assertion above would also pass
    against a function that returned the same three strings unconditionally.
    Both cases point a synthetic export at the REAL station file, so what
    clears the reasons is the delivered data agreeing, not a stub.
    """

    def test_a_survey_of_the_line_the_file_covers_clears_two_of_three_reasons(self, tmp_path):
        """Line 4250 N with electrodes ON the pickets: only the CRS blocks it."""
        export = tmp_path / "L4250N" / "export"
        export.mkdir(parents=True)
        _write_observed(
            export / "SYN_Vp_XYZ.rdtmd",
            "Normalized Potential - Line 4250 N",
            "Pole-Dipole",
            ["  4600.0  4600.0  4650.0  4700.0  0.5",
             "  4650.0  4650.0  4700.0  4750.0  0.25"],
        )
        survey = read_dcip2d_survey(export, station_file=STATION_FILE)

        assert len(survey.join.stations_on_line) == 24
        assert survey.join.exact_matches == (4600.0, 4650.0, 4700.0, 4750.0)
        assert len(survey.join.unresolved_reasons) == 1
        assert "no EPSG code" in survey.join.unresolved_reasons[0]
        assert survey.join.resolved is False

    def test_a_partial_chainage_match_still_blocks_the_join(self, tmp_path):
        """Interpolating the rest needs the per-line picket-to-metres relation.

        On line 4250 N that relation is not a constant: a nominal 50 m picket
        step measures between 37.97 m and 64.19 m of real ground.
        """
        export = tmp_path / "L4250N" / "export"
        export.mkdir(parents=True)
        _write_observed(
            export / "SYN_Vp_XYZ.rdtmd",
            "Normalized Potential - Line 4250 N",
            "Pole-Dipole",
            ["  4600.0  4600.0  4638.4  4700.0  0.5"],
        )
        survey = read_dcip2d_survey(export, station_file=STATION_FILE)
        assert survey.join.exact_matches == (4600.0, 4700.0)
        reasons = " | ".join(survey.join.unresolved_reasons)
        assert "2 of 3 electrode chainages" in reasons
