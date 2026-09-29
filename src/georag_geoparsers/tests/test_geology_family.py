"""Alteration and mineralization: header reading, classification, both parsers.

WHY THIS FILE EXISTS
    silver.alteration and silver.mineralization existed (2026_05_20_060400) and
    nothing wrote them, so a geology log's alteration and mineralization
    columns - the minerals and the intensity a geologist actually logs - were
    dropped by the lithology parser as "unmapped". These tests pin the rules
    that make one geology log feed several silver tables WITHOUT guessing:

    * the header grammar keeps ``Mineral1`` and ``Mineral1_Pct`` apart (the
      shared header normaliser folds both to the same skeleton);
    * only a column that NAMES the family is evidence, and a lithology log
      that also carries alteration columns stays a lithology log;
    * values are stored as typed; a percentage that is not a plain number is
      kept in notes with a warning, never converted.
"""

from __future__ import annotations

import io

import pytest

from georag_geoparsers import (
    parse_csv_alteration,
    parse_csv_lithology,
    parse_csv_mineralization,
)
from georag_geoparsers._drill_schema import (
    ALTERATION_ALIASES,
    MINERALIZATION_ALIASES,
    schemas,
)
from georag_geoparsers._geology_columns import (
    FAMILY_ALTERATION,
    FAMILY_MINERALIZATION,
    classify_header,
    has_family_evidence,
)
from georag_geoparsers._sheet_classifier import classify_sheet_type


def _csv(text: str) -> io.StringIO:
    return io.StringIO(text)


def _codes(result) -> set[str]:
    return {w["code"] for w in result.warnings}


def _warning(result, code):
    return next(w for w in result.warnings if w["code"] == code)


# ---------------------------------------------------------------------------
# Header grammar
# ---------------------------------------------------------------------------

class TestHeaderGrammar:
    @pytest.mark.parametrize("header,role,slot", [
        ("Alteration", "name", 0),
        ("Alt", "name", 0),
        ("Alt_Type", "name", 0),
        ("Alt1", "name", 1),
        ("Alteration_2", "name", 2),
        ("Alt_Intensity", "intensity", 0),
        ("Alt1_Int", "intensity", 1),
        ("Alteration Minerals", "minerals", 0),
        ("Alt_Comments", "notes", 0),
        ("Alt_Style", "style", 0),
    ])
    def test_alteration_headers(self, header, role, slot) -> None:
        col = classify_header(header, FAMILY_ALTERATION)
        assert col is not None
        assert (col.role, col.slot) == (role, slot)

    @pytest.mark.parametrize("header", [
        "Altitude",          # not alteration
        "Alteration_Weathering",  # the lithology parser's weathering alias
        "Alt_2023",          # a number that is not a slot
        "Alt_Depth",
        "Intensity",         # weak: never strong evidence
    ])
    def test_headers_that_are_not_alteration(self, header) -> None:
        assert classify_header(header, FAMILY_ALTERATION) is None

    @pytest.mark.parametrize("header,role,slot", [
        ("Mineral", "name", 0),
        ("Mineral1", "name", 1),
        ("Min1", "name", 1),
        ("Min_2", "name", 2),
        ("Min1_%", "pct", 1),
        ("Mineral1_Pct", "pct", 1),
        ("Mineral_%", "pct", 0),
        ("Min3_Pct", "pct", 3),
        ("Min_Style", "form", 0),
        ("Mineralization_Style", "form", 0),
        ("Min1_Grain_Size", "grain", 1),
        ("Sulphide%", "group_pct", 0),
        ("Total_Sulphide_%", "group_pct", 0),
        ("Mineralization", "name", 0),
    ])
    def test_mineralization_headers(self, header, role, slot) -> None:
        col = classify_header(header, FAMILY_MINERALIZATION)
        assert col is not None
        assert (col.role, col.slot) == (role, slot)

    @pytest.mark.parametrize("header", [
        "Min",           # a minimum
        "Min_Depth",
        "Min_Pct",       # as likely a minimum percentage as a mineral's
        "Min_Size",
        "Min5",          # beyond the four slots
        "Sulphide",      # may hold text or a number: not read
        "Minerals",      # ambiguous with alteration minerals: weak only
    ])
    def test_headers_that_are_not_mineralization(self, header) -> None:
        assert classify_header(header, FAMILY_MINERALIZATION) is None

    def test_free_text_mineralization_column_is_flagged(self) -> None:
        col = classify_header("Mineralization", FAMILY_MINERALIZATION)
        assert col is not None and col.free_text

    def test_evidence_needs_a_name_not_just_an_attribute(self) -> None:
        assert has_family_evidence(["Alteration"], FAMILY_ALTERATION)
        assert not has_family_evidence(["Alt_Intensity"], FAMILY_ALTERATION)
        assert has_family_evidence(["Mineral1"], FAMILY_MINERALIZATION)
        assert has_family_evidence(["Sulphide%"], FAMILY_MINERALIZATION)
        assert not has_family_evidence(["Min1_%"], FAMILY_MINERALIZATION)

    def test_every_documented_spelling_is_recognised(self) -> None:
        """The alias lists (docs, mapping UI) and the grammar must not drift."""
        for spelling in ALTERATION_ALIASES["alteration_type"]:
            col = classify_header(spelling, FAMILY_ALTERATION)
            assert col is not None and col.role == "name", spelling
        for spelling in MINERALIZATION_ALIASES["mineral"]:
            col = classify_header(spelling, FAMILY_MINERALIZATION)
            assert col is not None and col.role == "name", spelling
        for spelling in MINERALIZATION_ALIASES["abundance_pct"]:
            col = classify_header(spelling, FAMILY_MINERALIZATION)
            assert col is not None and col.role in ("pct", "group_pct"), spelling


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class TestClassification:
    def test_the_schemas_are_registered_and_structure_stays_last(self) -> None:
        names = list(schemas())
        assert "alteration" in names and "mineralization" in names
        assert names[-1] == "structure"

    def test_a_geology_log_stays_a_lithology_log(self) -> None:
        headers = [
            "HoleID", "From", "To", "Lith", "Lith_Desc", "Alteration",
            "Alt_Intensity", "Mineral1", "Min1_%",
        ]
        assert classify_sheet_type(headers)[0] == "lithology"

    def test_a_standalone_alteration_log(self) -> None:
        sheet_type, conf = classify_sheet_type(
            ["HoleID", "From", "To", "Alteration", "Alt_Intensity", "Comments"]
        )
        assert (sheet_type, conf) == ("alteration", 1.0)

    def test_a_standalone_mineralization_log(self) -> None:
        assert classify_sheet_type(
            ["HoleID", "From", "To", "Mineral1", "Min1_%", "Min_Style"]
        )[0] == "mineralization"
        assert classify_sheet_type(
            ["HoleID", "From", "To", "Sulphide%"]
        )[0] == "mineralization"

    def test_alteration_wins_a_table_that_has_both_and_no_lithology(self) -> None:
        # The mineral columns are then read as a companion of the same rows.
        assert classify_sheet_type(
            ["HoleID", "From", "To", "Alteration", "Mineral1"]
        )[0] == "alteration"

    @pytest.mark.parametrize("headers", [
        # Attributes without a name: an intensity or a comment proves nothing.
        ["HoleID", "From", "To", "Intensity", "Comments"],
        ["HoleID", "From", "To", "Minerals"],
        ["HoleID", "From", "To", "Min1_%"],
    ])
    def test_weak_columns_do_not_make_the_family(self, headers) -> None:
        assert classify_sheet_type(headers)[0] not in ("alteration", "mineralization")

    def test_a_sample_table_with_an_alteration_column_is_still_a_sample(self) -> None:
        assert classify_sheet_type(
            ["HoleID", "From", "To", "SampleType", "Alteration"]
        )[0] == "sample"

    def test_alt_is_not_taken_from_a_collar_table(self) -> None:
        # "Alt" is altitude in a collar table, which has no depth interval.
        assert classify_sheet_type(
            ["HoleID", "Easting", "Northing", "Alt"]
        )[0] == "collar"

    def test_a_table_with_no_hole_is_neither(self) -> None:
        assert classify_sheet_type(
            ["From", "To", "Alteration"]
        )[0] not in ("alteration", "mineralization")

    def test_a_user_confirmed_mapping_counts_as_evidence(self) -> None:
        headers = ["HoleID", "From", "To", "Zone7"]
        assert classify_sheet_type(headers)[0] != "alteration"
        sheet_type, _ = classify_sheet_type(
            headers, column_map={"alteration": {"alteration_type": "Zone7"}},
        )
        assert sheet_type == "alteration"


# ---------------------------------------------------------------------------
# The header normaliser trap: a name column and its percentage column
# ---------------------------------------------------------------------------

class TestNameAndPercentageAreNotSwapped:
    def test_the_percentage_may_come_before_the_name(self) -> None:
        """normalize_header folds Mineral1_Pct to 'mineral1' - the NAME's skeleton.

        Alias matching would have paired the two by file order. The token
        grammar pairs them by meaning, whichever order the file lists them.
        """
        for header in ("HoleID,From,To,Min1_%,Mineral1", "HoleID,From,To,Mineral1,Min1_%"):
            names = header.split(",")
            row = ["H1", "0", "5"] + (["4", "Pyrite"] if names[3] == "Min1_%" else ["Pyrite", "4"])
            result = parse_csv_mineralization(_csv(header + "\n" + ",".join(row) + "\n"))
            (rec,) = result.records
            assert rec["mineral"] == "Pyrite", header
            assert rec["abundance_pct"] == 4.0, header


# ---------------------------------------------------------------------------
# Alteration parser
# ---------------------------------------------------------------------------

class TestAlterationParser:
    _LOG = (
        "HoleID,From,To,Alteration,Alt_Intensity,Alt_Minerals,Comments\n"
        "D1,0,5,Chlorite,Strong,\"chlorite; sericite\",pervasive\n"
        "D1,5,9,None,,,\n"                # says there is none: skipped
        "D1,9,12,Sericite,weak,,\n"
        ",,,,,,\n"                         # an empty row: skipped, no complaint
    )

    def test_standalone_columns_and_values_are_kept_as_typed(self) -> None:
        result = parse_csv_alteration(_csv(self._LOG))
        assert [r["alteration_type"] for r in result.records] == ["Chlorite", "Sericite"]
        first = result.records[0]
        assert first["intensity"] == "Strong"          # not normalised
        assert first["minerals"] == ["chlorite", "sericite"]
        assert first["notes"] == "pervasive"            # weak Comments read standalone
        assert result.skipped_rows == 0
        assert result.column_map["alteration_type"] == "Alteration"

    def test_none_is_not_an_alteration(self) -> None:
        result = parse_csv_alteration(_csv(self._LOG))
        assert all(r["alteration_type"].lower() != "none" for r in result.records)

    def test_several_alterations_in_one_interval_are_one_row_each(self) -> None:
        result = parse_csv_alteration(_csv(
            "HoleID,From,To,Alt1,Alt1_Int,Alt2,Alt2_Int\n"
            "D1,0,5,Chlorite,Strong,Sericite,Weak\n"
        ))
        assert [(r["alteration_type"], r["intensity"]) for r in result.records] == [
            ("Chlorite", "Strong"), ("Sericite", "Weak"),
        ]
        assert {(r["from_depth"], r["to_depth"]) for r in result.records} == {(0.0, 5.0)}

    def test_an_unnumbered_intensity_beside_two_alterations_is_not_guessed(self) -> None:
        result = parse_csv_alteration(_csv(
            "HoleID,From,To,Alt1,Alt2,Alt_Intensity\n"
            "D1,0,5,Chlorite,Sericite,Strong\n"
        ))
        assert [r["intensity"] for r in result.records] == [None, None]
        assert all("(not assigned to one alteration): Strong" in r["notes"] for r in result.records)
        assert "alteration_value_unassigned" in _codes(result)

    def test_style_has_no_column_and_is_kept_in_notes_with_a_warning(self) -> None:
        result = parse_csv_alteration(_csv(
            "HoleID,From,To,Alteration,Alt_Style\nD1,0,5,Chlorite,patchy\n"
        ))
        assert result.records[0]["notes"] == "style: patchy"
        assert "alteration_style_in_notes" in _codes(result)

    def test_a_row_with_an_intensity_but_no_type_is_rejected(self) -> None:
        result = parse_csv_alteration(_csv(
            "HoleID,From,To,Alteration,Alt_Intensity\nD1,0,5,,Strong\nD1,5,9,Chlorite,\n"
        ))
        assert [r["alteration_type"] for r in result.records] == ["Chlorite"]
        assert [d["code"] for d in result.skipped_details] == ["missing_required"]

    @pytest.mark.parametrize("row,code", [
        ("D1,9,5,Chlorite", "depth_order_invalid"),
        ("D1,-1,5,Chlorite", "depth_negative"),
        ("D1,x,5,Chlorite", "numeric_cast_failed"),
        (",0,5,Chlorite", "missing_required"),
    ])
    def test_unusable_intervals_are_rejected_with_a_reason(self, row, code) -> None:
        result = parse_csv_alteration(_csv(f"HoleID,From,To,Alteration\n{row}\n"))
        assert result.records == []
        assert [d["code"] for d in result.skipped_details] == [code]

    def test_no_type_column_is_a_refusal_naming_it(self) -> None:
        result = parse_csv_alteration(_csv("HoleID,From,To,Comments\nD1,0,5,x\n"))
        assert result.records == []
        (refusal,) = result.skipped_details
        assert refusal["row"] is None and "alteration_type" in refusal["reason"]

    def test_a_user_confirmed_mapping_is_honoured(self) -> None:
        result = parse_csv_alteration(
            _csv("HoleID,From,To,Zone7,Strength\nD1,0,5,Chlorite,Strong\n"),
            vendor_aliases={"alteration_type": ["Zone7"], "intensity": ["Strength"]},
        )
        assert (result.records[0]["alteration_type"], result.records[0]["intensity"]) == (
            "Chlorite", "Strong",
        )


# ---------------------------------------------------------------------------
# Mineralization parser
# ---------------------------------------------------------------------------

class TestMineralizationParser:
    def test_kyles_columns(self) -> None:
        result = parse_csv_mineralization(_csv(
            "HoleID,From,To,Mineral1,Min1_%,Min_Style,Sulphide%\n"
            "D1,0,5,Pyrite,3%,Disseminated,4\n"
        ))
        by_mineral = {r["mineral"]: r for r in result.records}
        assert by_mineral["Pyrite"]["abundance_pct"] == 3.0
        assert by_mineral["Pyrite"]["form"] == "Disseminated"
        assert by_mineral["Sulphide"]["abundance_pct"] == 4.0
        assert "group total" in by_mineral["Sulphide"]["notes"]

    @pytest.mark.parametrize("cell,expected", [
        ("3", 3.0), ("3%", 3.0), (" 3.5 % ", 3.5), ("0", 0.0), ("100", 100.0),
        ("3,5", 3.5),
    ])
    def test_plain_numbers_are_taken(self, cell, expected) -> None:
        result = parse_csv_mineralization(_csv(
            f"HoleID,From,To,Mineral1,Min1_%\nD1,0,5,Pyrite,\"{cell}\"\n"
        ))
        assert result.records[0]["abundance_pct"] == expected
        assert not {"mineralization_abundance_not_numeric",
                    "mineralization_abundance_out_of_range"} & _codes(result)

    @pytest.mark.parametrize("cell", ["trace", "<1", "3-5", "minor", "tr"])
    def test_a_percentage_that_is_not_a_number_is_kept_not_converted(self, cell) -> None:
        result = parse_csv_mineralization(_csv(
            f"HoleID,From,To,Mineral1,Min1_%\nD1,0,5,Pyrite,{cell}\n"
        ))
        (rec,) = result.records
        assert rec["abundance_pct"] is None       # no invented value; a range is not averaged
        assert rec["notes"] == f"abundance: {cell}"
        warning = _warning(result, "mineralization_abundance_not_numeric")
        assert warning["count"] == 1 and warning["examples"] == [cell]

    @pytest.mark.parametrize("cell", ["150", "-5"])
    def test_an_out_of_range_percentage_never_reaches_the_check_constraint(self, cell) -> None:
        result = parse_csv_mineralization(_csv(
            f"HoleID,From,To,Mineral1,Min1_%\nD1,0,5,Pyrite,{cell}\n"
        ))
        (rec,) = result.records
        assert rec["abundance_pct"] is None
        assert f"abundance: {cell}" == rec["notes"]
        assert "mineralization_abundance_out_of_range" in _codes(result)

    def test_several_minerals_pair_each_with_its_own_percentage(self) -> None:
        result = parse_csv_mineralization(_csv(
            "HoleID,From,To,Mineral1,Min1_%,Mineral2,Min2_%,Min_Style\n"
            "D1,0,5,Pyrite,3,Chalcopyrite,1,Stringer\n"
        ))
        assert [(r["mineral"], r["abundance_pct"], r["form"]) for r in result.records] == [
            ("Pyrite", 3.0, "Stringer"), ("Chalcopyrite", 1.0, "Stringer"),
        ]

    def test_an_unnumbered_percentage_beside_two_minerals_is_not_guessed(self) -> None:
        result = parse_csv_mineralization(_csv(
            "HoleID,From,To,Mineral1,Mineral2,Mineral_%\nD1,0,5,Pyrite,Chalcopyrite,5\n"
        ))
        assert [r["abundance_pct"] for r in result.records] == [None, None]
        assert all("(not assigned to one mineral): 5" in r["notes"] for r in result.records)
        assert "mineralization_value_unassigned" in _codes(result)

    def test_zero_sulphide_is_an_absence_not_an_occurrence(self) -> None:
        result = parse_csv_mineralization(_csv(
            "HoleID,From,To,Sulphide%\nD1,0,5,0\nD1,5,9,2\n"
        ))
        assert [(r["from_depth"], r["abundance_pct"]) for r in result.records] == [(5.0, 2.0)]

    def test_a_free_text_mineralization_column_is_stored_verbatim_and_reported(self) -> None:
        result = parse_csv_mineralization(_csv(
            "HoleID,From,To,Mineralization\n"
            "D1,0,5,disseminated pyrite + minor cpy\nD1,5,9,Nil\n"
        ))
        (rec,) = result.records                    # Nil is "nothing", not a mineral
        assert rec["mineral"] == "disseminated pyrite + minor cpy"
        assert "mineralization_text_unsplit" in _codes(result)

    def test_free_text_beside_a_named_mineral_is_kept_in_notes_not_read_as_style(self) -> None:
        result = parse_csv_mineralization(_csv(
            "HoleID,From,To,Mineral1,Mineralization\nD1,0,5,Pyrite,stockwork\n"
        ))
        (rec,) = result.records
        assert rec["mineral"] == "Pyrite"
        assert rec["form"] is None
        assert rec["notes"] == "Mineralization: stockwork"
        assert "mineralization_text_in_notes" in _codes(result)

    def test_intensity_has_no_column_and_is_kept_in_notes(self) -> None:
        result = parse_csv_mineralization(_csv(
            "HoleID,From,To,Mineral1,Mineralization_Intensity\nD1,0,5,Pyrite,strong\n"
        ))
        assert result.records[0]["notes"] == "intensity: strong"
        assert "mineralization_intensity_in_notes" in _codes(result)

    def test_minimum_columns_are_not_read_as_minerals(self) -> None:
        result = parse_csv_mineralization(_csv(
            "HoleID,From,To,Mineral1,Min,Min_Depth\nD1,0,5,Pyrite,1,2\n"
        ))
        assert result.unmapped_columns == ["Min", "Min_Depth"]

    def test_notes_are_read_standalone_only(self) -> None:
        text = "HoleID,From,To,Mineral1,Comments\nD1,0,5,Pyrite,vuggy\n"
        assert parse_csv_mineralization(_csv(text)).records[0]["notes"] == "vuggy"
        companion = parse_csv_mineralization(_csv(text), companion=True)
        assert companion.records[0]["notes"] is None      # Comments stay with the lithology


# ---------------------------------------------------------------------------
# One geology log, three tables
# ---------------------------------------------------------------------------

_GEOLOGY_LOG = (
    "HoleID,From,To,Lith,Lith_Desc,Colour,Grain,Alteration,Alt_Intensity,"
    "Mineral1,Min1_%,Min_Style,Sulphide%,Comments\n"
    "H1,0,5,GRN,Grey granite,grey,Fine,Chlorite,Strong,,,,,fresh\n"
    "H1,5,10,GRN,,dark grey,Coarse,None,,Pyrite,3%,Disseminated,4,\n"
    "H1,10,15,SST,Sandstone,red,Medium,Sericite,weak,Pyrite,trace,,0,\n"
    "H1,15,20,,,,,,,Chalcopyrite,1,,,\n"        # no lithology code
)


class TestOneLogThreeTables:
    def test_each_table_gets_only_the_rows_that_have_something_for_it(self) -> None:
        lith = parse_csv_lithology(_csv(_GEOLOGY_LOG))
        alt = parse_csv_alteration(_csv(_GEOLOGY_LOG), companion=True)
        mineral = parse_csv_mineralization(_csv(_GEOLOGY_LOG), companion=True)

        assert [r["from_depth"] for r in lith.records] == [0.0, 5.0, 10.0]
        assert [(r["from_depth"], r["alteration_type"]) for r in alt.records] == [
            (0.0, "Chlorite"), (10.0, "Sericite"),
        ]
        assert [(r["from_depth"], r["mineral"]) for r in mineral.records] == [
            (5.0, "Pyrite"), (5.0, "Sulphide"), (10.0, "Pyrite"), (15.0, "Chalcopyrite"),
        ]

    def test_a_row_the_lithology_parser_rejects_still_feeds_mineralization(self) -> None:
        lith = parse_csv_lithology(_csv(_GEOLOGY_LOG))
        assert [d["code"] for d in lith.skipped_details] == ["missing_required"]
        mineral = parse_csv_mineralization(_csv(_GEOLOGY_LOG), companion=True)
        assert any(r["from_depth"] == 15.0 for r in mineral.records)

    def test_a_companion_never_double_reports_a_bad_row(self) -> None:
        text = (
            "HoleID,From,To,Lith,Alteration\n"
            "H1,9,5,GRN,Chlorite\n"           # inverted: the lithology parser reports it
        )
        alt = parse_csv_alteration(_csv(text), companion=True)
        assert alt.records == [] and alt.skipped_details == [] and alt.warnings == []

    def test_a_companion_of_a_file_with_no_such_columns_is_silent(self) -> None:
        text = "HoleID,From,To,Lithology\nH1,0,5,GRN\n"
        for parser in (parse_csv_alteration, parse_csv_mineralization):
            result = parser(_csv(text), companion=True)
            assert result.records == [] and result.skipped_details == [] and result.warnings == []

    def test_the_columns_nobody_read_are_listed(self) -> None:
        lith = parse_csv_lithology(_csv(_GEOLOGY_LOG))
        alt = parse_csv_alteration(_csv(_GEOLOGY_LOG), companion=True)
        mineral = parse_csv_mineralization(_csv(_GEOLOGY_LOG), companion=True)
        claimed = set(alt.column_map.values()) | set(mineral.column_map.values())
        # "Comments" beside "Lith_Desc" is a second description: kept in the
        # description, not dropped, so nothing here is left unread.
        unread = [c for c in lith.unmapped_columns if c not in claimed]
        assert unread == []
        assert lith.records[0]["lithology_description"] == "Grey granite [Comments: fresh]"

    def test_a_column_nobody_can_place_is_reported_as_unread(self) -> None:
        text = "HoleID,From,To,Lith,Alteration,Logged_By\nH1,0,5,GRN,Chlorite,AB\n"
        lith = parse_csv_lithology(_csv(text))
        alt = parse_csv_alteration(_csv(text), companion=True)
        unread = [c for c in lith.unmapped_columns if c not in alt.column_map.values()]
        assert unread == ["Logged_By"]


# ---------------------------------------------------------------------------
# Lithology: the columns that must reach silver.lithology_logs intact
# ---------------------------------------------------------------------------

class TestLithologyKeepsEveryColumn:
    _ALL = (
        "HoleID,From,To,Lith,Lith_Desc,Colour,Grain,Hardness,Weathering,RQD,Recovery\n"
        "H1,0,5,GRN,Grey granite,grey,Fine,Hard,Fresh,85,98\n"
    )

    def test_all_eight_attributes_are_kept(self) -> None:
        (rec,) = parse_csv_lithology(_csv(self._ALL)).records
        assert (
            rec["lithology_code"], rec["lithology_description"], rec["color"],
            rec["grain_size"], rec["hardness"], rec["weathering"],
            rec["rqd"], rec["recovery"],
        ) == ("GRN", "Grey granite", "grey", "Fine", "Hard", "Fresh", 85.0, 98.0)

    @pytest.mark.parametrize("header,field", [
        ("Rock_Colour", "color"), ("REC%", "recovery"), ("TCR", "recovery"),
        ("Core_Rec", "recovery"), ("RQD_%", "rqd"), ("Weath", "weathering"),
        ("Geol_Desc", "lithology_description"), ("Comment", "lithology_description"),
        ("Rock", "lithology_code"),
    ])
    def test_vendor_spellings(self, header, field) -> None:
        base = {
            "color": "Colour", "recovery": "Recovery", "rqd": "RQD",
            "weathering": "Weathering", "lithology_description": "Description",
            "lithology_code": "Lith",
        }
        cols = {"lithology_code": "Lith"}
        cols[field] = header
        values = {
            "lithology_code": "GRN", "color": "grey", "recovery": "90", "rqd": "80",
            "weathering": "Fresh", "lithology_description": "d",
        }
        assert base  # documents the canonical spelling each alias stands in for
        head = "HoleID,From,To," + ",".join(cols.values())
        row = "H1,0,5," + ",".join(values[f] for f in cols)
        (rec,) = parse_csv_lithology(_csv(head + "\n" + row + "\n")).records
        assert rec[field] == (float(values[field]) if field in ("recovery", "rqd") else values[field])

    def test_a_percent_sign_is_a_unit_not_a_defect(self) -> None:
        """'85%' used to be read as no value at all, silently."""
        result = parse_csv_lithology(_csv(
            "HoleID,From,To,Lith,RQD,Recovery\nH1,0,5,GRN,85%,98 %\n"
        ))
        assert (result.records[0]["rqd"], result.records[0]["recovery"]) == (85.0, 98.0)
        assert "optional_values_blanked" not in _codes(result)

    def test_a_non_numeric_rqd_keeps_the_row_the_text_and_says_so(self) -> None:
        result = parse_csv_lithology(_csv(
            "HoleID,From,To,Lith,RQD\nH1,0,5,GRN,poor\n"
        ))
        (rec,) = result.records
        assert rec["rqd"] is None
        assert rec["lithology_description"] == "[rqd: poor]"
        assert _warning(result, "optional_values_blanked")["fields"]["rqd (not a number)"]["count"] == 1

    def test_an_out_of_range_rqd_keeps_the_interval(self) -> None:
        """RQD 140 used to reject the whole interval - and its lithology with it."""
        result = parse_csv_lithology(_csv(
            "HoleID,From,To,Lith,Recovery\nH1,0,5,GRN,140\nH1,5,9,GRN,90\n"
        ))
        assert result.valid_rows == 2 and result.skipped_rows == 0
        assert result.records[0]["recovery"] is None
        assert "[recovery: 140]" in result.records[0]["lithology_description"]
        assert "recovery (outside 0-100)" in _warning(result, "optional_values_blanked")["fields"]

    def test_a_code_wider_than_its_column_keeps_the_full_text(self) -> None:
        """silver.lithology_logs.lithology_code is varchar(20): a longer value
        failed the whole batch insert."""
        long_code = "Manitou Falls Formation, upper member"
        result = parse_csv_lithology(_csv(
            f'HoleID,From,To,Lith\nH1,0,5,"{long_code}"\n'
        ))
        (rec,) = result.records
        assert len(rec["lithology_code"]) <= 20
        assert long_code in rec["lithology_description"]
        assert "lithology_values_too_long" in _codes(result)

    def test_a_colour_wider_than_its_column_is_blanked_and_kept(self) -> None:
        long_colour = "dark greenish grey to black with pinkish feldspar phenocrysts"
        result = parse_csv_lithology(_csv(
            f"HoleID,From,To,Lith,Colour\nH1,0,5,GRN,{long_colour}\n"
        ))
        (rec,) = result.records
        assert rec["color"] is None
        assert long_colour in rec["lithology_description"]
        assert "color" in _warning(result, "lithology_values_too_long")["fields"]

    def test_a_short_colour_is_untouched(self) -> None:
        (rec,) = parse_csv_lithology(_csv(
            "HoleID,From,To,Lith,Colour\nH1,0,5,GRN,Dark greenish grey\n"
        )).records
        assert rec["color"] == "Dark greenish grey"

    @pytest.mark.parametrize("cell,expected", [
        ("Fine grained", "Fine"), ("medium-grained", "Medium"),
        ("Very coarse grained", "Very Coarse"), ("COARSE GRAIN", "Coarse"),
    ])
    def test_grained_is_the_same_word(self, cell, expected) -> None:
        (rec,) = parse_csv_lithology(_csv(
            f"HoleID,From,To,Lith,Grain\nH1,0,5,GRN,{cell}\n"
        )).records
        assert rec["grain_size"] == expected

    @pytest.mark.parametrize("cell,expected", [
        ("Slightly weathered", "Slight"), ("Highly weathered", "High"),
        ("moderately", "Moderate"), ("Completely weathered", "Complete"),
    ])
    def test_weathering_adverbs_are_the_same_word(self, cell, expected) -> None:
        (rec,) = parse_csv_lithology(_csv(
            f"HoleID,From,To,Lith,Weathering\nH1,0,5,GRN,{cell}\n"
        )).records
        assert rec["weathering"] == expected

    def test_an_unlisted_word_is_still_blanked_not_mapped(self) -> None:
        (rec,) = parse_csv_lithology(_csv(
            "HoleID,From,To,Lith,Grain,Weathering\nH1,0,5,GRN,porphyritic,extremely weathered\n"
        )).records
        assert rec["grain_size"] is None and rec["weathering"] is None
        assert "porphyritic" in rec["lithology_description"]
        assert "extremely weathered" in rec["lithology_description"]


# ---------------------------------------------------------------------------
# Workbook sheets: the same parsers, reached through parse_xlsx_sheet
# ---------------------------------------------------------------------------

class TestWorkbookSheet:
    """``fastexcel`` (polars' spreadsheet engine) is not installed everywhere,
    so the sheet read is replaced by a DataFrame; what is under test is the
    dispatch and the companion flag, which are the new part."""

    def _sheet(self, monkeypatch, tmp_path):
        import polars as pl

        from georag_geoparsers import xlsx_parser

        frame = pl.read_csv(io.StringIO(_GEOLOGY_LOG), infer_schema=False)
        monkeypatch.setattr(xlsx_parser.pl, "read_excel", lambda *a, **k: frame)
        path = tmp_path / "geology.xlsx"
        path.write_bytes(b"not read: read_excel is replaced")
        return xlsx_parser, str(path)

    def test_a_companion_sheet_feeds_alteration_and_mineralization(
        self, monkeypatch, tmp_path,
    ) -> None:
        xlsx_parser, path = self._sheet(monkeypatch, tmp_path)
        alt = xlsx_parser.parse_xlsx_sheet(
            path, "Geology", "alteration", companion=True,
        )
        mineral = xlsx_parser.parse_xlsx_sheet(
            path, "Geology", "mineralization", companion=True,
        )
        assert [r["alteration_type"] for r in alt.records] == ["Chlorite", "Sericite"]
        assert (5.0, "Pyrite") in [(r["from_depth"], r["mineral"]) for r in mineral.records]

    def test_the_family_warnings_are_forwarded_from_a_workbook(
        self, monkeypatch, tmp_path,
    ) -> None:
        xlsx_parser, path = self._sheet(monkeypatch, tmp_path)
        mineral = xlsx_parser.parse_xlsx_sheet(
            path, "Geology", "mineralization", companion=True,
        )
        assert "mineralization_abundance_not_numeric" in {w["code"] for w in mineral.warnings}
