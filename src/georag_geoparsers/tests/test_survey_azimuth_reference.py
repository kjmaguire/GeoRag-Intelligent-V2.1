"""Per-file azimuth reference in survey tables (Kyle, 2026-09-29).

A survey export that says which north its azimuths use — a column like
``Azimuth_Ref`` / ``Az_Reference`` / ``North_Ref`` — is read into
``azimuth_reference`` as 'true' | 'magnetic' | 'grid' (the silver.surveys
CHECK vocabulary). Desurvey prefers it over the project's default.

An unreadable value is BLANKED and reported, never read as grid: grid is the
no-correction default, so guessing it would make a declaration the file did
make indistinguishable from one it did not.
"""
from __future__ import annotations

from io import StringIO

import pytest

from georag_geoparsers._azimuth_reference import (
    AZIMUTH_REFERENCES,
    canonical_azimuth_reference,
)
from georag_geoparsers._drill_schema import SURVEY_ALIASES
from georag_geoparsers._header_match import build_column_map
from georag_geoparsers.csv_survey import parse_csv_surveys


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("True", "true"), ("TRUE NORTH", "true"), ("true-north", "true"), ("TN", "true"),
        ("T", "true"), ("Geographic", "true"),
        ("Magnetic", "magnetic"), ("MAG", "magnetic"), ("Mag North", "magnetic"),
        ("MN", "magnetic"), ("m", "magnetic"),
        ("Grid", "grid"), ("GRID NORTH", "grid"), ("GN", "grid"), ("g", "grid"),
    ],
)
def test_spellings_canonicalise(raw: str, expected: str) -> None:
    assert canonical_azimuth_reference(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "BOH", "TOH", "UTM", "local", "12.5", "north"])
def test_non_declarations_are_none(raw: object) -> None:
    # BOH/TOH are core-orientation marks; UTM/local/north say nothing about
    # WHICH north. None of them may be read as a reference.
    assert canonical_azimuth_reference(raw) is None


def test_canonical_values_are_the_check_vocabulary() -> None:
    assert AZIMUTH_REFERENCES == ("true", "magnetic", "grid")
    for value in AZIMUTH_REFERENCES:
        assert len(value) <= 10, "silver.surveys.azimuth_reference is varchar(10)"
        assert canonical_azimuth_reference(value) == value


@pytest.mark.parametrize(
    "header",
    ["Azimuth_Ref", "Az_Reference", "North_Ref", "AZ REF", "azimuthReference", "North Reference"],
)
def test_reference_headers_map(header: str) -> None:
    column_map, _ = build_column_map(
        ["HoleID", "Depth", "Azimuth", "Dip", header], SURVEY_ALIASES,
    )
    assert column_map.get("azimuth_reference") == header
    # ...without stealing the azimuth column itself.
    assert column_map["azimuth"] == "Azimuth"


def _csv(rows: list[str], header: str = "HoleID,Depth,Azimuth,Dip,Azimuth_Ref") -> StringIO:
    return StringIO("\n".join([header, *rows]) + "\n")


def test_parser_reads_and_canonicalises_the_column() -> None:
    result = parse_csv_surveys(_csv([
        "DDH-1,0,45,-60,True North",
        "DDH-1,50,46,-59,MAG",
        "DDH-1,100,47,-58,grid",
        "DDH-1,150,48,-57,",
    ]))
    assert result.valid_rows == 4
    assert [r.get("azimuth_reference") for r in result.records] == ["true", "magnetic", "grid", None]


def test_unrecognised_reference_is_blanked_and_reported_not_rejected() -> None:
    result = parse_csv_surveys(_csv([
        "DDH-2,0,45,-60,UTM",
        "DDH-2,50,46,-59,true",
    ]))
    # The station survives: the reference is optional.
    assert result.valid_rows == 2
    assert [r.get("azimuth_reference") for r in result.records] == [None, "true"]
    blanked = [w for w in result.warnings if w.get("code") == "optional_values_blanked"]
    assert blanked, "an unreadable declaration must be reported"
    assert "azimuth_reference" in blanked[0]["fields"]


def test_file_without_the_column_carries_no_reference() -> None:
    result = parse_csv_surveys(_csv(["DDH-3,0,45,-60"], header="HoleID,Depth,Azimuth,Dip"))
    assert result.valid_rows == 1
    assert result.records[0].get("azimuth_reference") is None
