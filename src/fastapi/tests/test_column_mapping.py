"""A column mapping the user confirmed, from the wire to the parser.

The gap this closes: alias matching is generous but finite, and when it
misses a REQUIRED field the whole file is refused. Until now the only
remedy the app offered was "rename the key columns and re-upload" — advice
that asks a geologist to edit their source data to suit our vocabulary, and
which they cannot follow at all for a file they received from a third party.

The mapping is applied AHEAD of the built-in spellings, never instead of
them. Naming the one column we could not find must not oblige the user to
re-state the six we did.
"""
from __future__ import annotations

import io

import pytest
from georag_geoparsers._sheet_classifier import classify_sheet_type
from georag_geoparsers.csv_collar import parse_csv_collars

from app.hatchet_workflows.ingest_tabular import _vendor_aliases_for


def _csv(text: str) -> io.StringIO:
    return io.StringIO(text)


# A real-world shape alias matching cannot resolve: the identity column is
# a site-local name, and the coordinates are labelled by grid axis only.
_UNMATCHABLE = (
    "Site Ref,Grid Ref East,Grid Ref North,Collar Height\n"
    "APO-001,471250.5,4657100.0,1830.2\n"
    "APO-002,471310.0,4657160.0,1835.0\n"
)


class TestVendorAliasesFor:
    def test_a_mapping_becomes_single_entry_alias_lists(self):
        assert _vendor_aliases_for(
            {"collar": {"hole_id": "Site Ref"}}, "collar",
        ) == {"hole_id": ["Site Ref"]}

    def test_another_sheet_types_mapping_is_not_borrowed(self):
        # Keyed by drill type so one workbook can map its collar sheet and
        # its lithology sheet differently.
        assert _vendor_aliases_for({"lithology": {"hole_id": "X"}}, "collar") is None

    @pytest.mark.parametrize("empty", [None, {}, {"collar": {}}])
    def test_no_mapping_is_no_aliases(self, empty):
        assert _vendor_aliases_for(empty, "collar") is None

    def test_blank_and_non_string_choices_are_dropped(self):
        # An untouched dropdown posts "" — that is "I did not choose", not
        # "map this field to a column with no name".
        assert _vendor_aliases_for(
            {"collar": {"hole_id": "Site Ref", "easting": "  ", "northing": None}},
            "collar",
        ) == {"hole_id": ["Site Ref"]}


class TestTheParserHonoursTheMapping:
    def test_without_a_mapping_the_file_is_refused(self):
        result = parse_csv_collars(_csv(_UNMATCHABLE))

        assert result.valid_rows == 0
        assert result.skipped_details[0]["code"] == "missing_required"

    def test_with_a_mapping_every_row_lands(self):
        result = parse_csv_collars(
            _csv(_UNMATCHABLE),
            vendor_aliases={
                "hole_id": ["Site Ref"],
                "easting": ["Grid Ref East"],
                "northing": ["Grid Ref North"],
            },
        )

        assert result.valid_rows == 2
        assert result.records[0]["hole_id"] == "APO-001"
        assert result.records[0]["easting"] == pytest.approx(471250.5)

    def test_fields_left_unmapped_still_match_by_alias(self):
        # 'Collar Height' is in the built-in elevation aliases; the user
        # named only the three the parser could not find.
        result = parse_csv_collars(
            _csv(_UNMATCHABLE),
            vendor_aliases={
                "hole_id": ["Site Ref"],
                "easting": ["Grid Ref East"],
                "northing": ["Grid Ref North"],
            },
        )

        assert result.column_map["elevation"] == "Collar Height"
        assert result.records[0]["elevation"] == pytest.approx(1830.2)

    def test_a_mapping_overrides_a_column_alias_matching_would_have_taken(self):
        # Both columns could be read as the easting. The user's choice wins
        # because it is matched first, not because anything special-cases it.
        text = (
            "HoleID,Easting,Grid Ref East,Northing\n"
            "A-1,111111.0,471250.5,4657100.0\n"
        )
        result = parse_csv_collars(
            _csv(text), vendor_aliases={"easting": ["Grid Ref East"]},
        )

        assert result.column_map["easting"] == "Grid Ref East"
        assert result.records[0]["easting"] == pytest.approx(471250.5)

    def test_a_mapping_naming_a_column_that_is_not_there_changes_nothing(self):
        # Guards a plausible UI bug — posting a stale column name after the
        # user swapped the file. It must not be read as "map to nothing".
        result = parse_csv_collars(
            _csv("HoleID,Easting,Northing\nA-1,471250.5,4657100.0\n"),
            vendor_aliases={"easting": ["A Column That Does Not Exist"]},
        )

        assert result.column_map["easting"] == "Easting"
        assert result.valid_rows == 1


class TestTheClassifierHonoursTheMapping:
    def test_an_unmappable_sheet_is_unknown_without_one(self):
        headers = ["Site Ref", "Grid Ref East", "Grid Ref North", "Collar Height"]

        assert classify_sheet_type(headers)[0] == "unknown"

    def test_the_same_sheet_classifies_once_its_columns_are_named(self):
        # Classification has to see the mapping or it could never take
        # effect: an unknown sheet goes to the text fallback and is never
        # dispatched to the parser the mapping was written for.
        headers = ["Site Ref", "Grid Ref East", "Grid Ref North", "Collar Height"]

        sheet_type, confidence = classify_sheet_type(
            headers,
            column_map={
                "collar": {
                    "hole_id": "Site Ref",
                    "easting": "Grid Ref East",
                    "northing": "Grid Ref North",
                },
            },
        )

        assert (sheet_type, confidence) == ("collar", 1.0)

    def test_a_mapping_cannot_conjure_a_hole_id_that_is_absent(self):
        # _IDENTITY_FIELD still holds: a mapping names which column holds a
        # field, it does not assert that the field exists. A geophysics
        # station list mapped as collars is still not drill data.
        sheet_type, _ = classify_sheet_type(
            ["Grid Ref East", "Grid Ref North"],
            column_map={
                "collar": {
                    "easting": "Grid Ref East",
                    "northing": "Grid Ref North",
                },
            },
        )

        assert sheet_type == "unknown"
