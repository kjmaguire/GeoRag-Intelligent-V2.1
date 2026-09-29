"""Radiometric-age table parsing (ING-19, 2026-09-29).

Pins the behaviours that are wrong quietly: headers matched through the
shared normaliser (the old exact match rejected ``Sample ID``), X/Y read as
easting/northing rather than longitude/latitude (which threw out every row
of a UTM-located table), the isotopic system read from the method only when
exactly one is named, 1σ/2σ never guessed, and a bad latitude costing the
row its location but never its age.
"""

from __future__ import annotations

from pathlib import Path

from georag_geoparsers.csv_geochronology import (
    CODE_AGE_IMPLAUSIBLE,
    CODE_AGE_UNIT_ASSUMED,
    CODE_INVALID_ISOTOPIC,
    CODE_SYSTEM_FROM_METHOD,
    CODE_UNCERTAINTY_KIND_UNSTATED,
    canonical_isotopic_system,
    geochronology_signal,
    parse_csv_geochronology,
    parse_geochronology_rows,
    uncertainty_kind_from_header,
)

LAB_TABLE = """\
Sample ID,Rock Type,System,Mineral,Age (Ma),±2σ (Ma),Method,Lab,Reference,Easting,Northing
SK-01,granodiorite,U-Pb,zircon,1845.2,3.1,LA-ICP-MS,PCIGR,doi:10.1/x,495000,6220000
SK-02,tonalite,,titanite,1810.0,5.0,SHRIMP U-Pb,ANU,,495100,6220100
SK-03,schist,40Ar/39Ar,muscovite,1750.5,4.2,step heating,UBC,,,
SK-04,basalt,Carbon-14,whole rock,0.01,0.001,AMS,Lab,,495200,6220200
SK-05,diorite,U-Pb,zircon,-5,1,,,,,
SK-06,granite,U–Pb,monazite,99999,1,,,,,
"""


def _csv(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "ages.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_lab_table_maps_and_keeps_the_valid_rows(tmp_path: Path) -> None:
    result = parse_csv_geochronology(_csv(tmp_path, LAB_TABLE), source_label="ages.csv")
    assert result.column_map["easting"] == "Easting"
    assert "latitude" not in result.column_map
    samples = [(r["sample_id"], r["isotopic_system"], r["age_ma"]) for r in result.records]
    assert samples == [
        ("SK-01", "U-Pb", 1845.2),
        ("SK-02", "U-Pb", 1810.0),      # system read from "SHRIMP U-Pb"
        ("SK-03", "Ar-Ar", 1750.5),     # "40Ar/39Ar"
    ]
    codes = {d["code"] for d in result.skipped_details}
    assert CODE_INVALID_ISOTOPIC in codes      # Carbon-14 is not in the enum
    assert CODE_AGE_IMPLAUSIBLE in codes       # 99,999 Ma is a unit error
    assert result.skipped_rows == 3
    assert {w["code"] for w in result.warnings} >= {CODE_SYSTEM_FROM_METHOD}


def test_uncertainty_kind_comes_from_the_header_when_there_is_no_column(tmp_path: Path) -> None:
    result = parse_csv_geochronology(_csv(tmp_path, LAB_TABLE))
    assert {r["uncertainty_kind"] for r in result.records} == {"2sigma"}


def test_unstated_sigma_is_stored_null_and_warned(tmp_path: Path) -> None:
    text = "Sample,System,Age_Ma,Error\nA1,U-Pb,100.0,1.2\n"
    result = parse_csv_geochronology(_csv(tmp_path, text))
    assert result.records[0]["uncertainty_kind"] is None
    assert CODE_UNCERTAINTY_KIND_UNSTATED in {w["code"] for w in result.warnings}


def test_a_bare_age_column_is_read_as_ma_and_says_so(tmp_path: Path) -> None:
    text = "Sample,System,Age\nA1,Re-Os,2700\n"
    result = parse_csv_geochronology(_csv(tmp_path, text))
    assert result.records[0]["age_ma"] == 2700.0
    assert CODE_AGE_UNIT_ASSUMED in {w["code"] for w in result.warnings}


def test_ga_is_converted_to_ma() -> None:
    result = parse_geochronology_rows(
        [{"Sample": "A1", "System": "Pb-Pb", "Age (Ga)": "2.7"}],
    )
    assert result.records[0]["age_ma"] == 2700.0


def test_an_out_of_range_latitude_drops_the_location_not_the_age() -> None:
    result = parse_geochronology_rows([
        {"Sample": "A1", "System": "U-Pb", "Age_Ma": "100", "Lat": "956.1", "Long": "-129.1"},
    ])
    assert result.valid_rows == 1
    assert result.records[0]["geom_wkt"] is None
    assert result.location_issues and result.location_issues[0]["row"] == 2


def test_lat_long_rows_get_a_wgs84_point() -> None:
    result = parse_geochronology_rows([
        {"Sample": "A1", "System": "U-Pb", "Age_Ma": "100", "Lat": "56.1", "Long": "-129.1"},
    ])
    assert result.records[0]["geom_wkt"] == "POINT(-129.1 56.1)"


def test_a_method_naming_two_systems_is_refused() -> None:
    result = parse_geochronology_rows([
        {"Sample": "A1", "Method": "U-Pb and Ar-Ar", "Age_Ma": "100"},
    ])
    assert result.valid_rows == 0
    assert result.skipped_details[0]["code"] == CODE_INVALID_ISOTOPIC


def test_isotopic_system_spellings() -> None:
    assert canonical_isotopic_system("U–Pb") == "U-Pb"      # en dash
    assert canonical_isotopic_system("206Pb/238U") == "U-Pb"
    assert canonical_isotopic_system("207Pb/206Pb") == "Pb-Pb"
    assert canonical_isotopic_system("Re-Os molybdenite") == "Re-Os"
    assert canonical_isotopic_system("other") == "other"
    assert canonical_isotopic_system("Carbon-14") is None


def test_uncertainty_kind_from_header() -> None:
    assert uncertainty_kind_from_header("±2σ (Ma)") == "2sigma"
    assert uncertainty_kind_from_header("Err_2s") == "2sigma"
    assert uncertainty_kind_from_header("2s_Ma") == "2sigma"
    assert uncertainty_kind_from_header("1sd") == "1sigma"
    assert uncertainty_kind_from_header("Error") is None


def test_detection_signal() -> None:
    assert geochronology_signal(["Sample", "System", "Age (Ma)"]) == "strong"
    # A drill-core dating table is still a dating table.
    assert geochronology_signal(["HoleID", "From", "To", "SampleID", "System", "Age_Ma"]) == "strong"
    assert geochronology_signal(["Sample", "Method", "Age"]) == "weak"
    # An assay table has a sample and a method but no age.
    assert geochronology_signal(["HoleID", "From", "To", "Sample", "Method", "Au_ppm"]) is None
    assert geochronology_signal(["Age", "System"]) is None
