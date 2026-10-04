"""Assay columns, assay values and sample types (audit 2026-09-29).

ING-3  any element / common name, units ppm, ppb, g/t (= ppm), gpt, %, pct,
       oz/t (x34.2857 -> ppm); a bare element is assumed ppm and SAID so.
ING-4  a missing Sample_Type column keeps the rows; synonyms map onto the
       existing enum; an unknown type is blanked, not the row rejected.
ING-9  ">10" is stored as 10 with an over-detection flag, not dropped.
ING-10 a negative cell is a negated detection limit, never a negative grade;
       -9999 / -999 are "not measured".

Nothing here is uranium-specific: the fixtures are gold, base-metal and
multi-element exports of the kind any explorer receives from a lab.
"""
from __future__ import annotations

import io

import pytest

from georag_geoparsers._assay_columns import (
    OZ_PER_TON_TO_PPM,
    parse_assay_header,
    split_assay_key,
)
from georag_geoparsers.csv_sample import (
    _parse_assay_value,
    canonical_sample_type,
    parse_csv_samples,
)


class TestAssayHeaders:
    @pytest.mark.parametrize(("header", "key", "factor"), [
        ("Au (g/t)", "Au_ppm", 1.0),
        ("Au_gpt", "Au_ppm", 1.0),
        ("Ag g/t", "Ag_ppm", 1.0),
        ("Au_g_t", "Au_ppm", 1.0),
        ("Au_ppb", "Au_ppb", 1.0),
        ("Cu_%", "Cu_pct", 1.0),
        ("Cu %", "Cu_pct", 1.0),
        ("Zn_pct", "Zn_pct", 1.0),
        ("Pb ppm", "Pb_ppm", 1.0),
        ("U3O8_pct", "U3O8_pct", 1.0),
        ("u3o8ppm", "U3O8_ppm", 1.0),
        ("AU_PPM_FA", "Au_ppm", 1.0),
        ("Au-AA24 (ppm)", "Au_ppm", 1.0),
        ("Au oz/t", "Au_ppm", OZ_PER_TON_TO_PPM),
        ("Au_opt", "Au_ppm", OZ_PER_TON_TO_PPM),
        ("Mo_ppm", "Mo_ppm", 1.0),
        ("Co (ppm)", "Co_ppm", 1.0),
        ("Pt_ppb", "Pt_ppb", 1.0),
        ("Li2O_%", "Li2O_pct", 1.0),
        ("TREO_pct", "TREO_pct", 1.0),
        ("S_%", "S_pct", 1.0),
        ("Gold (g/t)", "Au_ppm", 1.0),
        ("Copper %", "Cu_pct", 1.0),
        ("Fe2O3 wt%", "Fe2O3_pct", 1.0),
        ("Ag mg/kg", "Ag_ppm", 1.0),
    ])
    def test_recognised(self, header: str, key: str, factor: float) -> None:
        spec = parse_assay_header(header)
        assert spec is not None, header
        assert spec.key == key
        assert spec.factor == pytest.approx(factor)
        assert not spec.unit_assumed

    @pytest.mark.parametrize(("header", "key"), [
        ("Mo", "Mo_ppm"), ("Cu", "Cu_ppm"), ("Au_FA", "Au_ppm"),
        ("Au-AA24", "Au_ppm"), ("As", "As_ppm"), ("Uranium", "U_ppm"),
    ])
    def test_bare_element_is_assumed_ppm(self, header: str, key: str) -> None:
        spec = parse_assay_header(header)
        assert spec is not None and spec.key == key and spec.unit_assumed

    @pytest.mark.parametrize("header", [
        "Hole_ID", "From", "To", "Sample_Type", "Recovery %", "AuEq_gpt",
        "CuEq", "eU3O8", "Au_gt", "Y", "S", "U", "As received wt", "Co_ordinate",
        "Au_ppm_ppb", "Density", "Weight kg", "Comments", "",
    ])
    def test_not_an_assay(self, header: str) -> None:
        assert parse_assay_header(header) is None

    def test_split_key_round_trips(self) -> None:
        assert split_assay_key("Au_ppm") == ("Au", "ppm")
        assert split_assay_key("AU_PPM") == ("Au", "ppm")
        assert split_assay_key("Ti") == ("Ti", None)
        assert split_assay_key("not_an_assay") is None


class TestAssayValues:
    def test_over_limit_keeps_the_limit_and_flags_it(self) -> None:
        value, flags = _parse_assay_value(">10")
        assert value == 10.0
        assert flags is not None and flags["od_flag"] and flags["od_threshold"] == 10.0

    def test_negative_is_a_negated_detection_limit(self) -> None:
        value, flags = _parse_assay_value("-0.005")
        assert value == pytest.approx(0.0025)      # same as "<0.005"
        assert flags is not None and flags["dl_flag"]
        assert flags["dl_threshold"] == pytest.approx(0.005)
        assert value >= 0

    @pytest.mark.parametrize("raw", ["-9999", "-999", "-9999.0", "-99999"])
    def test_sentinels_are_missing(self, raw: str) -> None:
        value, flags = _parse_assay_value(raw)
        assert value is None
        assert flags is not None and flags["missing_sentinel"]

    def test_below_detection_unchanged(self) -> None:
        value, flags = _parse_assay_value("<0.01")
        assert value == pytest.approx(0.005)
        assert flags is not None and flags["substitution"] == "half_dl"

    def test_nan_is_not_a_measurement(self) -> None:
        value, flags = _parse_assay_value("nan")
        assert value is None and flags is not None and flags["unparseable"]


class TestSampleTypes:
    @pytest.mark.parametrize(("raw", "expected"), [
        ("Core", "Core"), ("core", "Core"), ("CORE", "Core"), ("DD", "Core"),
        ("DDH", "Core"), ("NQ", "Core"), ("HQ", "Core"), ("PQ", "Core"),
        ("Diamond", "Core"), ("Half Core", "Core"), ("half-core", "Core"),
        ("RC", "Chip"), ("R.C.", "Chip"), ("Reverse Circulation", "Chip"),
        ("chips", "Chip"), ("Percussion", "Chip"), ("Rock Chip", "Chip"),
        ("grab", "Grab"), ("Channel", "Channel"), ("SOIL", "Soil"),
    ])
    def test_synonyms_map_onto_the_existing_enum(self, raw: str, expected: str) -> None:
        assert canonical_sample_type(raw) == expected

    @pytest.mark.parametrize(("raw", "expected"), [
        ("Trench", "Channel"), ("TRENCH", "Channel"), ("trench channel", "Channel"),
        ("Trench-Channel", "Channel"), ("Trenches", "Channel"),
        ("RAB", "Chip"), ("rab", "Chip"), ("Rotary Air Blast", "Chip"),
        ("Aircore", "Chip"), ("Air-core", "Chip"), ("AC", "Chip"), ("A.C.", "Chip"),
    ])
    def test_sme_approved_synonyms(self, raw: str, expected: str) -> None:
        """§04e, SME-approved (Kyle, 2026-09-29)."""
        assert canonical_sample_type(raw) == expected

    @pytest.mark.parametrize("raw", ["Pulp", "Reject", "Sonic", ""])
    def test_unknown_types_are_not_guessed(self, raw: str) -> None:
        assert canonical_sample_type(raw) is None


def _parse(text: str):
    return parse_csv_samples(io.StringIO(text))


class TestParseCsvSamples:
    def test_every_row_kept_whatever_the_type_spelling(self) -> None:
        """Seven rows, seven records - an unknown type blanks, never rejects."""
        result = _parse(
            "Hole_ID,Sample_ID,From,To,Sample_Type,Au_ppm\n"
            "DH-1,S1,0,1,Core,0.5\nDH-1,S2,1,2,core,0.7\nDH-1,S3,2,3,DD,0.1\n"
            "DH-1,S4,3,4,RC,0.1\nDH-1,S5,4,5,Half Core,0.1\n"
            "DH-1,S6,5,6,CORE,0.2\nDH-1,S7,6,7,Pulp,0.2\n"
        )
        assert [r["sample_type"] for r in result.records] == [
            "Core", "Core", "Core", "Chip", "Core", "Core", None,
        ]
        codes = [w["code"] for w in result.warnings]
        assert "optional_values_blanked" in codes
        assert all(r["commodity_assays"] for r in result.records)

    def test_no_sample_type_column_keeps_the_rows(self) -> None:
        result = _parse(
            "Hole,SampleID,From,To,Au_ppm,Cu_pct\n"
            "DH-1,S1,0,1,0.5,0.1\nDH-1,S2,1,2,0.7,0.2\n"
        )
        assert len(result.records) == 2
        assert all(r.get("sample_type") is None for r in result.records)
        warning = next(w for w in result.warnings if w["code"] == "sample_type_column_missing")
        assert warning["detail"]

    def test_no_type_no_assays_no_sample_number_is_refused(self) -> None:
        """A geotech table must not become silver.samples rows."""
        result = _parse("Hole,From,To,RQD\nDH-1,0,1,80\n")
        assert result.records == []
        assert result.skipped_details[0]["code"] == "missing_required"

    def test_multi_element_units_and_flags(self) -> None:
        result = _parse(
            "Hole_ID,Sample_ID,From,To,Au (g/t),Ag_gpt,Cu_%,Pb ppm,Mo,Au_opt\n"
            "DH-1,S1,0,1,>10,1.2,0.12,-9999,-1,0.1\n"
        )
        rec = result.records[0]
        assays, flags = rec["commodity_assays"], rec["commodity_assay_flags"]
        # Au (g/t) is ">10" (flagged) and Au_opt 0.1 oz/t is a clean 3.43 ppm:
        # the clean measurement wins the shared Au_ppm key.
        assert assays["Au_ppm"] == pytest.approx(0.1 * OZ_PER_TON_TO_PPM)
        assert assays["Ag_ppm"] == 1.2
        assert assays["Cu_pct"] == 0.12
        assert "Pb_ppm" not in assays and flags["Pb_ppm"]["missing_sentinel"]
        assert assays["Mo_ppm"] == 0.5 and flags["Mo_ppm"]["dl_flag"]
        assert all(v >= 0 for v in assays.values())
        codes = {w["code"] for w in result.warnings}
        assert {"assay_unit_assumed", "assay_unit_converted", "assay_columns_merged"} <= codes

    def test_over_limit_survives_when_it_is_the_only_reading(self) -> None:
        result = _parse(
            "Hole_ID,Sample_ID,From,To,Au (g/t)\nDH-1,S1,0,1,>10\n"
        )
        rec = result.records[0]
        assert rec["commodity_assays"] == {"Au_ppm": 10.0}
        assert rec["commodity_assay_flags"]["Au_ppm"]["od_flag"] is True

    def test_oz_per_ton_thresholds_are_converted(self) -> None:
        result = _parse("Hole_ID,Sample_ID,From,To,Au_opt\nDH-1,S1,0,1,<0.002\n")
        flag = result.records[0]["commodity_assay_flags"]["Au_ppm"]
        assert flag["dl_threshold"] == pytest.approx(0.002 * OZ_PER_TON_TO_PPM)

    def test_long_format_g_per_t_lands_under_the_ppm_key(self) -> None:
        result = _parse(
            "Hole_ID,Sample_ID,From,To,Element,Value,Unit\n"
            "DH-1,S1,0,1,Au,1.5,g/t\nDH-1,S1,0,1,Cu,0.2,%\n"
        )
        assert result.records[0]["commodity_assays"] == {"Au_ppm": 1.5, "Cu_pct": 0.2}


class TestLongFormatUnitAssumption:
    """A long-format file with no unit column was read as ppm in silence.

    Cu in % read as ppm is a 10,000x error; the wide format already warned
    (``assay_unit_assumed``), the long format did not.
    """

    def test_no_unit_column_warns_with_the_element_list(self) -> None:
        result = _parse(
            "Hole_ID,Sample_ID,From,To,Element,Value\n"
            "DH-1,S1,0,1,Au,1.5\nDH-1,S1,0,1,Cu,0.2\n"
        )
        # Still stored as ppm - the point is that it is said, not hidden.
        assert result.records[0]["commodity_assays"] == {"Au_ppm": 1.5, "Cu_ppm": 0.2}
        (warning,) = [w for w in result.warnings if w["code"] == "assay_unit_assumed"]
        assert warning["context"]["elements"] == ["Au", "Cu"]
        assert "'Cu'" in warning["detail"] and "ppm" in warning["detail"]
        assert warning["row"] is None

    def test_a_unit_column_that_names_every_unit_does_not_warn(self) -> None:
        result = _parse(
            "Hole_ID,Sample_ID,From,To,Element,Value,Unit\n"
            "DH-1,S1,0,1,Au,1.5,g/t\nDH-1,S1,0,1,Cu,0.2,%\n"
        )
        assert not [w for w in result.warnings if w["code"] == "assay_unit_assumed"]

    def test_a_blank_unit_cell_names_that_element_only(self) -> None:
        result = _parse(
            "Hole_ID,Sample_ID,From,To,Element,Value,Unit\n"
            "DH-1,S1,0,1,Au,1.5,g/t\nDH-1,S1,0,1,Cu,0.2,\n"
        )
        (warning,) = [w for w in result.warnings if w["code"] == "assay_unit_assumed"]
        assert warning["context"]["elements"] == ["Cu"]


class TestSummarizeUnitAmbiguity:
    """``outlier_flags["unit_ambiguity"]`` collapses to one entry per column.

    These run the REAL detectors, so a reworded detector string breaks the
    summary here instead of silently dropping out of the run's warning.
    """

    def test_wide_bare_noble_and_bare_base_metal_columns(self) -> None:
        from georag_geoparsers._unit_ambiguity import summarize_unit_ambiguity

        result = _parse(
            "Hole_ID,Sample_ID,From,To,Au,Cu\n"
            "DH-1,S1,0,1,1.5,5000\nDH-1,S2,1,2,0.2,4000\n"
        )
        by_column = {e["column"]: e for e in summarize_unit_ambiguity(result.outlier_flags)}
        assert by_column["Au"]["kind"] == "bare_noble_metal"
        assert (by_column["Au"]["inferred_unit"], by_column["Au"]["alternative_unit"]) == ("ppm", "g/t")
        assert by_column["Au"]["records"] == 2
        assert by_column["Cu"]["kind"] == "bare_base_metal"
        assert (by_column["Cu"]["inferred_unit"], by_column["Cu"]["alternative_unit"]) == ("ppm", "pct")

    def test_long_format_noble_row_with_no_unit(self) -> None:
        from georag_geoparsers._unit_ambiguity import summarize_unit_ambiguity

        result = _parse(
            "Hole_ID,Sample_ID,From,To,Element,Value,Unit\n"
            "DH-1,S1,0,1,Au,1.5,\n"
        )
        (entry,) = summarize_unit_ambiguity(result.outlier_flags)
        assert entry["column"] == "Au" and entry["kind"] == "missing_unit_noble_metal"
        assert (entry["inferred_unit"], entry["alternative_unit"]) == ("ppm", "g/t")

    def test_long_format_cross_mixing_reports_the_minority_unit(self) -> None:
        from georag_geoparsers._unit_ambiguity import summarize_unit_ambiguity

        result = _parse(
            "Hole_ID,Sample_ID,From,To,Element,Value,Unit\n"
            "DH-1,S1,0,1,Au,1.5,g/t\nDH-1,S2,1,2,Au,2.5,g/t\n"
            "DH-1,S3,2,3,Au,0.1,oz/t\n"
        )
        entries = summarize_unit_ambiguity(result.outlier_flags)
        mixed = [e for e in entries if e["kind"] == "unit_cross_mixing"]
        assert len(mixed) == 1
        assert mixed[0]["column"] == "Au"
        assert mixed[0]["inferred_unit"] == "oz/t"
        assert mixed[0]["alternative_unit"] == "g/t"

    def test_clean_file_summarizes_to_nothing(self) -> None:
        from georag_geoparsers._unit_ambiguity import summarize_unit_ambiguity

        result = _parse("Hole_ID,Sample_ID,From,To,Au_ppm,Cu_pct\nDH-1,S1,0,1,0.5,0.1\n")
        assert summarize_unit_ambiguity(result.outlier_flags) == []
        assert summarize_unit_ambiguity(None) == []

    def test_an_unrecognised_flag_string_is_kept_not_dropped(self) -> None:
        from georag_geoparsers._unit_ambiguity import summarize_unit_ambiguity

        (entry,) = summarize_unit_ambiguity(
            [{"unit_ambiguity": ["Zn: something new the detector says"]}],
        )
        assert entry["column"] == "Zn" and entry["kind"] == "other"
        assert entry["inferred_unit"] is None
