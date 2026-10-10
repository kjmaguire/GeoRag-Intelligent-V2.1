"""Structural measurements: the parser, and telling them from a survey.

WHY THIS FILE EXISTS
    silver.structure existed and promote_silver_to_gold read it, but nothing
    wrote it: a delivery's structure logs (oriented core, fault/vein/joint
    lists) had no ingest path. The parser targets the silver.structure
    COLUMNS (depth, structure_type, alpha_angle, beta_angle, true_dip,
    true_dip_dir, roughness, infill, notes).

    The dangerous part is not the parser, it is the classifier. A survey has
    hole + depth + azimuth + dip, and so does half a structure log - so a
    structure table read as a survey lands as downhole survey stations and
    bends the hole. The rule under test: structure needs EXPLICIT structural
    evidence (a structure-type name, alpha, beta, a dip-DIRECTION column) AND
    an orientation column, and survey keeps priority wherever it is
    ambiguous.
"""

from __future__ import annotations

import io

import pytest

from georag_geoparsers import parse_csv_structures
from georag_geoparsers._drill_schema import schemas
from georag_geoparsers._sheet_classifier import classify_sheet_type
from georag_geoparsers.csv_structure import map_structure_type


def _codes(result) -> set[str]:
    return {w["code"] for w in result.warnings}


def _warning(result, code):
    return next(w for w in result.warnings if w["code"] == code)


# ---------------------------------------------------------------------------
# Classifier: structure vs survey, in both directions
# ---------------------------------------------------------------------------

class TestStructureVersusSurvey:
    @pytest.mark.parametrize(
        "headers",
        [
            ["HOLE_ID", "DEPTH", "AZIMUTH", "DIP"],
            ["HoleID", "Depth_m", "Azi", "Dip"],
            # A bare TYPE is a weak spelling: it is what the collar, sample
            # and half the drill vocabulary call their category.
            ["HOLE_ID", "DEPTH", "TYPE", "AZIMUTH", "DIP"],
            # Dip and azimuth at a depth with only a bare Type is ambiguous;
            # survey keeps priority.
            ["HOLE_ID", "DEPTH", "TYPE", "DIP"],
        ],
    )
    def test_ambiguous_tables_stay_surveys(self, headers) -> None:
        sheet_type, _conf = classify_sheet_type(headers)
        assert sheet_type == "survey"

    @pytest.mark.parametrize(
        "headers",
        [
            ["HOLE_ID", "DEPTH", "STRUCT_TYPE", "DIP", "DIP_DIR"],
            ["HOLE_ID", "DEPTH", "DIP", "DIPDIR"],
            ["HOLE_ID", "DEPTH", "ALPHA", "BETA"],
            ["BHID", "DEPTH", "STRUCTURE", "ALPHA", "BETA", "DIP", "AZIMUTH"],
            # Full oriented-core log: survey is 100% covered too (hole, depth,
            # azimuth, dip) - alpha/beta/struct-type break the tie.
            ["HOLE_ID", "DEPTH", "STRUCT_TYPE", "ALPHA", "BETA", "DIP", "AZIMUTH"],
            # From/To instead of a depth.
            ["BHID", "FROM", "TO", "TYPE", "ALPHA", "BETA"],
        ],
    )
    def test_explicit_structure_columns_win(self, headers) -> None:
        sheet_type, confidence = classify_sheet_type(headers)
        assert sheet_type == "structure"
        assert confidence > 0.0

    def test_a_survey_method_column_keeps_it_a_survey(self) -> None:
        """Method/Instrument/Tool is a survey's own fingerprint."""
        sheet_type, _ = classify_sheet_type(
            ["HOLE_ID", "DEPTH", "AZIMUTH", "DIP", "METHOD", "DIPDIR"]
        )
        assert sheet_type == "survey"

    def test_a_lithology_log_with_a_structure_column_stays_lithology(self) -> None:
        """'Structure' is a texture descriptor in many logs."""
        sheet_type, _ = classify_sheet_type(
            ["HoleID", "From", "To", "Lithology", "Structure", "Dip"]
        )
        assert sheet_type == "lithology"

    def test_structure_needs_an_orientation_column(self) -> None:
        """hole + depth + a Structure column and no angle at all is not
        enough evidence; it is left to whatever else claims it."""
        sheet_type, _ = classify_sheet_type(
            ["HOLE_ID", "DEPTH", "STRUCTURE", "COMMENTS"]
        )
        assert sheet_type != "structure"

    def test_a_collar_table_is_still_a_collar(self) -> None:
        sheet_type, _ = classify_sheet_type(
            ["HoleID", "Easting", "Northing", "Elevation", "Dip", "Azimuth"]
        )
        assert sheet_type == "collar"

    def test_a_user_confirmed_mapping_counts_as_evidence(self) -> None:
        """A bare Type is weak - unless the user named it a structure type."""
        headers = ["HOLE_ID", "DEPTH", "TYPE", "AZIMUTH", "DIP"]
        assert classify_sheet_type(headers)[0] == "survey"
        sheet_type, _ = classify_sheet_type(
            headers, column_map={"structure": {"structure_type": "TYPE"}},
        )
        assert sheet_type == "structure"

    def test_survey_wins_when_structure_has_no_evidence_and_a_tie_on_coverage(
        self,
    ) -> None:
        aliases = schemas()
        # Sanity on the wiring the assertions above rely on: structure is
        # registered LAST so survey wins ties by iteration order too.
        assert list(aliases)[-1] == "structure"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_LOG = (
    "HOLE_ID,DEPTH,STRUCT_TYPE,ALPHA,BETA,DIP,DIP_DIR,COMMENTS\n"
    "D-01,10.5,Fault,45,120,60,210,gouge\n"
    "D-01,12,Quartz Vein,30,10,70,100,\n"
    "D-01,14,wobble,,,,,\n"
    "D-02,3,joint,95,10,,,bad alpha\n"          # alpha out of range: rejected
    "D-02,4,shear vein,,,20,20,\n"              # two types named: 'other'
    "D-02,5,bedding,,,-30,20,\n"                # negative dip: rejected
    "D-02,6,foliation,,,30,400,\n"              # dip direction > 360: rejected
    ",7,joint,,,30,20,\n"                       # no hole: rejected
)


class TestParser:
    def test_maps_the_silver_structure_columns(self) -> None:
        result = parse_csv_structures(io.StringIO(_LOG))

        assert result.column_map == {
            "hole_id": "HOLE_ID",
            "depth": "DEPTH",
            "structure_type": "STRUCT_TYPE",
            "alpha_angle": "ALPHA",
            "beta_angle": "BETA",
            "true_dip": "DIP",
            "true_dip_dir": "DIP_DIR",
            "notes": "COMMENTS",
        }
        first = result.records[0]
        assert first["hole_id"] == "D-01"
        assert first["hole_id_canonical"] == "D01"
        assert (first["depth"], first["structure_type"]) == (10.5, "fault")
        assert (first["alpha_angle"], first["beta_angle"]) == (45.0, 120.0)
        assert (first["true_dip"], first["true_dip_dir"]) == (60.0, 210.0)
        assert first["notes"] == "gouge"

    def test_range_violations_and_missing_required_go_to_skipped_details(
        self,
    ) -> None:
        result = parse_csv_structures(io.StringIO(_LOG))

        assert result.total_rows == 8
        assert result.valid_rows == 4
        assert result.skipped_rows == 4
        by_row = {d["row"]: d for d in result.skipped_details}
        # Row numbers are CSV line numbers (header is line 1).
        assert by_row[5]["code"] == "range_check_failed"      # alpha 95
        assert "alpha_angle" in by_row[5]["reason"]
        assert by_row[7]["code"] == "range_check_failed"      # dip -30
        assert by_row[8]["code"] == "range_check_failed"      # dip dir 400
        assert by_row[9]["code"] == "missing_required"        # no hole

    def test_types_map_to_the_gold_vocabulary_and_the_rest_are_reported(
        self,
    ) -> None:
        result = parse_csv_structures(io.StringIO(_LOG))

        assert [r["structure_type"] for r in result.records] == [
            "fault", "vein", "other", "other",
        ]
        # The raw text is kept, not lost, when it cannot be mapped.
        assert result.records[2]["notes"] == "type: wobble"
        assert result.records[3]["notes"] == "type: shear vein"
        warning = _warning(result, "structure_type_unmapped")
        assert warning["fields"]["structure_type"]["count"] == 2
        assert "wobble" in warning["detail"]

    def test_rows_without_any_orientation_are_kept_and_counted(self) -> None:
        result = parse_csv_structures(io.StringIO(_LOG))

        assert result.records[2]["true_dip"] is None
        assert "1 of 4" in _warning(result, "structure_no_orientation")["message"]

    def test_hole_and_depth_are_required(self) -> None:
        result = parse_csv_structures(io.StringIO("STRUCT_TYPE,DIP,DIP_DIR\nfault,45,90\n"))

        assert result.valid_rows == 0
        assert result.skipped_details[0]["row"] is None
        assert result.skipped_details[0]["code"] == "missing_required"

    def test_from_to_collapse_to_the_top_and_are_reported(self) -> None:
        result = parse_csv_structures(io.StringIO(
            "BHID,FROM,TO,STRUCTURE,DIP,DIPDIR\n"
            "D1,10.2,10.8,fault,50,90\n"
            "D1,20,20,joint,50,90\n"      # zero-length: a point, no note
        ))

        assert result.column_map["depth"] == "FROM"
        first, second = result.records
        assert first["depth"] == 10.2
        assert "logged over 10.2-10.8 m" in first["notes"]
        assert second["notes"] is None
        assert "1 structure row(s)" in _warning(
            result, "structure_interval_collapsed",
        )["message"]

    def test_plain_depth_wins_over_from_to(self) -> None:
        result = parse_csv_structures(io.StringIO(
            "HOLE_ID,DEPTH,FROM,TO,STRUCT_TYPE,DIP,DIPDIR\nD1,5,4,6,fault,50,90\n"
        ))
        assert result.column_map["depth"] == "DEPTH"
        assert result.records[0]["depth"] == 5.0

    def test_the_optional_extras_map_to_their_columns(self) -> None:
        result = parse_csv_structures(io.StringIO(
            "HOLE_ID,DEPTH,STRUCT_TYPE,DIP,DIPDIR,ROUGHNESS,INFILL\n"
            "D1,5,joint,50,90,rough,chlorite\n"
        ))
        rec = result.records[0]
        assert (rec["roughness"], rec["infill"]) == ("rough", "chlorite")

    def test_a_text_cell_in_an_optional_angle_blanks_it_and_keeps_the_row(
        self,
    ) -> None:
        result = parse_csv_structures(io.StringIO(
            "HOLE_ID,DEPTH,STRUCT_TYPE,ALPHA,BETA\nD1,5,fault,?,30\n"
        ))
        assert result.valid_rows == 1
        assert result.records[0]["alpha_angle"] is None
        assert result.records[0]["beta_angle"] == 30.0
        assert _warning(result, "optional_values_blanked")["fields"] == {
            "alpha_angle": {"count": 1, "examples": ["?"]},
        }


class TestStrike:
    def test_a_bare_strike_is_not_converted(self) -> None:
        """Right-hand rule, left-hand rule and quadrants all exist; a
        'Strike' header states none of them, so nothing is guessed."""
        result = parse_csv_structures(io.StringIO(
            "HOLE_ID,DEPTH,STRUCT_TYPE,DIP,STRIKE\nD1,5,fault,45,120\n"
        ))
        rec = result.records[0]
        assert rec["true_dip"] == 45.0
        assert rec["true_dip_dir"] is None
        assert "strike 120" in rec["notes"]      # not lost
        warning = _warning(result, "structure_strike_not_converted")
        assert "1 structure row(s)" in warning["message"]
        assert "convention" in warning["detail"]

    def test_a_column_that_names_the_rule_is_converted_and_says_so(self) -> None:
        result = parse_csv_structures(io.StringIO(
            "HOLE_ID,DEPTH,STRUCT_TYPE,DIP,STRIKE_RHR\n"
            "D1,5,fault,45,300\n"
            "D1,6,fault,45,10\n"
        ))
        assert [r["true_dip_dir"] for r in result.records] == [30.0, 100.0]
        assert "structure_strike_converted" in _codes(result)
        assert "structure_strike_not_converted" not in _codes(result)

    def test_an_explicit_dip_direction_beats_strike(self) -> None:
        result = parse_csv_structures(io.StringIO(
            "HOLE_ID,DEPTH,STRUCT_TYPE,DIP,DIPDIR,STRIKE\nD1,5,fault,45,200,120\n"
        ))
        assert result.records[0]["true_dip_dir"] == 200.0
        assert not (_codes(result) & {
            "structure_strike_not_converted", "structure_strike_converted",
        })


class TestAzimuthAsDipDirection:
    def test_azimuth_is_accepted_but_flagged(self) -> None:
        result = parse_csv_structures(io.StringIO(
            "HOLE_ID,DEPTH,STRUCT_TYPE,DIP,AZIMUTH\nD1,5,fault,45,200\n"
        ))
        assert result.column_map["true_dip_dir"] == "AZIMUTH"
        assert result.records[0]["true_dip_dir"] == 200.0
        assert "structure_dip_direction_from_azimuth" in _codes(result)

    def test_an_explicit_dip_direction_column_is_not_flagged(self) -> None:
        result = parse_csv_structures(io.StringIO(
            "HOLE_ID,DEPTH,STRUCT_TYPE,DIP,DIP_DIR\nD1,5,fault,45,200\n"
        ))
        assert "structure_dip_direction_from_azimuth" not in _codes(result)


class TestTypeMapping:
    @pytest.mark.parametrize(
        ("raw", "expected", "mapped"),
        [
            ("Fault", "fault", True),
            ("FLT", "fault", True),
            ("Quartz vein", "vein", True),
            ("Shear Zone", "shear", True),
            ("fold axis", "fold_axis", True),
            ("Bedding", "bedding", True),
            ("lithological contact", "contact", True),
            ("shear vein", "other", False),      # two types: refuse to pick
            ("fold", "other", False),            # axis or axial plane?
            ("wobble", "other", False),
            ("", "other", False),
            (None, "other", False),
            ("other", "other", True),
        ],
    )
    def test_conservative_synonyms_only(self, raw, expected, mapped) -> None:
        assert map_structure_type(raw) == (expected, mapped)


class TestWorkbookDispatch:
    """A worksheet is materialised to CSV and handed to this same parser."""

    @staticmethod
    def _sheet(tmp_path, monkeypatch, frame_columns):
        # A real workbook, written with openpyxl: the .xlsx path reads it
        # through openpyxl too, so nothing about the read needs stubbing.
        import openpyxl

        from georag_geoparsers import xlsx_parser

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Struct"
        ws.append(list(frame_columns))
        for row in zip(*frame_columns.values(), strict=True):
            ws.append(list(row))
        path = tmp_path / "structures.xlsx"
        wb.save(path)
        return xlsx_parser.parse_xlsx_sheet(str(path), "Struct", "structure")

    def test_xlsx_sheet_type_structure_is_dispatched(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._sheet(tmp_path, monkeypatch, {
            "HOLE_ID": ["D1", "D1"], "DEPTH": ["5", "6"],
            "STRUCT_TYPE": ["fault", "vein"], "DIP": ["45", "50"],
            "DIP_DIR": ["90", "120"],
        })

        assert result.sheet_type == "structure"
        assert result.valid_rows == 2
        assert [r["structure_type"] for r in result.records] == ["fault", "vein"]

    def test_structure_warnings_are_forwarded_from_a_worksheet(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._sheet(tmp_path, monkeypatch, {
            "HOLE_ID": ["D1"], "DEPTH": ["5"], "STRUCT_TYPE": ["fault"],
            "DIP": ["45"], "STRIKE": ["120"],
        })

        assert "structure_strike_not_converted" in {
            w["code"] for w in result.warnings
        }
