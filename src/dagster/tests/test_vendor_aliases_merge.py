"""CC-02 Item 6 — vendor_profile column-alias merge helpers.

Pins the merge-precedence and shape-conversion contracts that
parsers/_vendor_aliases.py exposes. The csv_lithology parser is the
first consumer; the same helper will back the other csv_* parsers as
they adopt the pattern.
"""
from __future__ import annotations


from georag_dagster.parsers._vendor_aliases import (
    merge_vendor_aliases,
    vendor_aliases_from_rows,
)


class TestMergeVendorAliases:
    def test_none_vendor_returns_shallow_copy_of_base(self):
        base = {"hole_id": ["HoleID", "hole_id"]}
        merged = merge_vendor_aliases(base, None)
        assert merged == base
        # Independent dict — mutating merged should not touch base.
        merged["hole_id"].append("MutatedAlias")
        assert "MutatedAlias" not in base["hole_id"]

    def test_empty_vendor_returns_shallow_copy_of_base(self):
        base = {"hole_id": ["HoleID"]}
        merged = merge_vendor_aliases(base, {})
        assert merged == base

    def test_vendor_aliases_prepend_to_base(self):
        base = {"hole_id": ["HoleID", "hole_id"]}
        vendor = {"hole_id": ["DDH_Number"]}
        merged = merge_vendor_aliases(base, vendor)
        assert merged["hole_id"] == ["DDH_Number", "HoleID", "hole_id"]

    def test_vendor_aliases_deduplicate_against_base(self):
        # Vendor declares an alias that's already in base — should appear
        # exactly once, in vendor position (first).
        base = {"hole_id": ["HoleID", "hole_id"]}
        vendor = {"hole_id": ["HoleID"]}
        merged = merge_vendor_aliases(base, vendor)
        assert merged["hole_id"] == ["HoleID", "hole_id"]

    def test_vendor_only_canonicals_passed_through(self):
        # A canonical field that's not in base at all (e.g. a vendor adds
        # a custom column) appears in the merged dict.
        base = {"hole_id": ["HoleID"]}
        vendor = {"custom_field": ["VendorCustomColumn"]}
        merged = merge_vendor_aliases(base, vendor)
        assert merged["custom_field"] == ["VendorCustomColumn"]
        assert merged["hole_id"] == ["HoleID"]

    def test_canonicals_in_base_without_vendor_entry_unchanged(self):
        base = {"hole_id": ["HoleID"], "from_depth": ["From", "from_depth"]}
        vendor = {"hole_id": ["DDH"]}
        merged = merge_vendor_aliases(base, vendor)
        assert merged["from_depth"] == ["From", "from_depth"]


class TestVendorAliasesFromRows:
    def test_simple_two_field_profile(self):
        rows = [
            {"parser_type": "csv_lithology", "canonical_field": "hole_id",    "source_column": "HoleID"},
            {"parser_type": "csv_lithology", "canonical_field": "from_depth", "source_column": "DepthFrom"},
        ]
        out = vendor_aliases_from_rows(rows, parser_type="csv_lithology")
        assert out == {"hole_id": ["HoleID"], "from_depth": ["DepthFrom"]}

    def test_multiple_aliases_per_canonical(self):
        rows = [
            {"parser_type": "csv_lithology", "canonical_field": "hole_id", "source_column": "HoleID"},
            {"parser_type": "csv_lithology", "canonical_field": "hole_id", "source_column": "DDH"},
        ]
        out = vendor_aliases_from_rows(rows, parser_type="csv_lithology")
        assert out == {"hole_id": ["HoleID", "DDH"]}

    def test_wrong_parser_type_filtered_out(self):
        # Defensive guard: csv_collar aliases should not bleed into a
        # csv_lithology resolve call.
        rows = [
            {"parser_type": "csv_lithology", "canonical_field": "hole_id", "source_column": "HoleID"},
            {"parser_type": "csv_collar",    "canonical_field": "hole_id", "source_column": "WrongParser"},
        ]
        out = vendor_aliases_from_rows(rows, parser_type="csv_lithology")
        assert out == {"hole_id": ["HoleID"]}
        assert "WrongParser" not in out["hole_id"]

    def test_missing_fields_skipped_silently(self):
        rows = [
            {"parser_type": "csv_lithology", "canonical_field": "hole_id", "source_column": "HoleID"},
            {"parser_type": "csv_lithology", "canonical_field": None, "source_column": "x"},  # bad row
            {"parser_type": "csv_lithology", "canonical_field": "y", "source_column": None},  # bad row
            {"parser_type": "csv_lithology"},  # bad row
        ]
        out = vendor_aliases_from_rows(rows, parser_type="csv_lithology")
        assert out == {"hole_id": ["HoleID"]}


class TestMergeWithRows:
    def test_end_to_end_round_trip(self):
        """Simulate the full flow: DB rows → vendor_aliases dict → merged."""
        base = {
            "hole_id":    ["HoleID", "hole_id"],
            "from_depth": ["From", "from_depth"],
            "to_depth":   ["To", "to_depth"],
        }
        db_rows = [
            {"parser_type": "csv_lithology", "canonical_field": "hole_id",    "source_column": "DDH_Number"},
            {"parser_type": "csv_lithology", "canonical_field": "from_depth", "source_column": "DepthFrom_m"},
            {"parser_type": "csv_collar",    "canonical_field": "hole_id",    "source_column": "ShouldNotAppear"},
        ]
        vendor = vendor_aliases_from_rows(db_rows, parser_type="csv_lithology")
        merged = merge_vendor_aliases(base, vendor)

        assert merged["hole_id"] == ["DDH_Number", "HoleID", "hole_id"]
        assert merged["from_depth"] == ["DepthFrom_m", "From", "from_depth"]
        # csv_collar entry was filtered out
        assert "ShouldNotAppear" not in merged["hole_id"]
        # untouched canonical preserved
        assert merged["to_depth"] == ["To", "to_depth"]
