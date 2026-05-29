"""Unit tests for commodity crosswalk and grouping logic in
silver_public_geoscience.py.

Covers:
- _canonicalize_commodities with a fake CrosswalkSet (codes list + majority grouping)
- Unknown alias preserved in codes, doesn't contribute to grouping
- Empty input → ([], None)
- _group_string_to_enum for all registered enum values + unknown + case-insensitive
- CrosswalkSet.resolve_status (key match + default on miss)
- CrosswalkSet.resolve_commodity (case-folded key + CommodityRecord returned)

Run with:  pytest tests/test_commodity_grouping.py -v
"""

from __future__ import annotations

import pytest

from georag_dagster.assets.silver_public_geoscience import (
    CommodityRecord,
    CrosswalkSet,
    _canonicalize_commodities,
    _group_string_to_enum,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_crosswalk_set(entries: list[tuple[str, str, str, str]]) -> CrosswalkSet:
    """Build a CrosswalkSet from a list of (alias_lower, canonical_code, canonical_name, grouping)."""
    commodity: dict[str, CommodityRecord] = {}
    for alias_lower, code, name, grouping in entries:
        commodity[alias_lower] = CommodityRecord(
            canonical_code=code,
            canonical_name=name,
            commodity_grouping=grouping,
        )
    return CrosswalkSet(commodity=commodity, status={})


def _make_status_crosswalk(
    entries: list[tuple[str, str, str, str]],
) -> CrosswalkSet:
    """Build a CrosswalkSet with status entries.

    Each entry is (jurisdiction_code, canonical_type, source_value_lower, canonical_status).
    """
    status: dict[tuple[str, str, str], str] = {}
    for jur, ctype, raw, canonical in entries:
        status[(jur, ctype, raw)] = canonical
    return CrosswalkSet(commodity={}, status=status)


# ---------------------------------------------------------------------------
# _canonicalize_commodities
# ---------------------------------------------------------------------------

class TestCanonicalizeCommodities:
    def _fake_crosswalks(self) -> CrosswalkSet:
        return _make_crosswalk_set([
            ("au",  "AU",  "Gold",   "precious_metals"),
            ("cu",  "CU",  "Copper", "base_metals"),
            ("zn",  "ZN",  "Zinc",   "base_metals"),
            ("ag",  "AG",  "Silver", "precious_metals"),
            ("fe",  "FE",  "Iron",   "base_metals"),
        ])

    def test_all_known_aliases_resolved(self):
        crosswalks = self._fake_crosswalks()
        codes, grouping = _canonicalize_commodities(["Au", "Cu", "Zn"], crosswalks)
        assert codes == ["AU", "CU", "ZN"]

    def test_majority_grouping_is_base_metals(self):
        crosswalks = self._fake_crosswalks()
        # Cu + Zn (base_metals×2) > Au (precious_metals×1) → base_metals wins
        _, grouping = _canonicalize_commodities(["Au", "Cu", "Zn"], crosswalks)
        assert grouping == "base_metals"

    def test_tie_broken_by_first_occurrence(self):
        crosswalks = self._fake_crosswalks()
        # Au (precious_metals) first, Cu (base_metals) second → tie → precious_metals wins
        # because first_seen for precious_metals = 0, base_metals = 1
        _, grouping = _canonicalize_commodities(["Au", "Cu"], crosswalks)
        assert grouping == "precious_metals"

    def test_unknown_alias_preserved_in_codes(self):
        crosswalks = self._fake_crosswalks()
        codes, _ = _canonicalize_commodities(["Au", "Unobtanium"], crosswalks)
        assert "AU" in codes
        assert "Unobtanium" in codes

    def test_unknown_alias_does_not_contribute_to_grouping(self):
        crosswalks = self._fake_crosswalks()
        # Only "Au" is known → grouping = precious_metals (1 vote)
        codes, grouping = _canonicalize_commodities(["Au", "Unobtanium"], crosswalks)
        assert grouping == "precious_metals"

    def test_empty_input_returns_empty_codes_and_none_grouping(self):
        crosswalks = self._fake_crosswalks()
        codes, grouping = _canonicalize_commodities([], crosswalks)
        assert codes == []
        assert grouping is None

    def test_all_unknown_aliases_returns_none_grouping(self):
        crosswalks = self._fake_crosswalks()
        codes, grouping = _canonicalize_commodities(["Foo", "Bar"], crosswalks)
        assert "Foo" in codes
        assert "Bar" in codes
        assert grouping is None

    def test_single_known_commodity(self):
        crosswalks = self._fake_crosswalks()
        codes, grouping = _canonicalize_commodities(["Au"], crosswalks)
        assert codes == ["AU"]
        assert grouping == "precious_metals"

    def test_empty_strings_in_input_skipped(self):
        crosswalks = self._fake_crosswalks()
        codes, _ = _canonicalize_commodities(["Au", "", "Cu"], crosswalks)
        assert "" not in codes
        assert len(codes) == 2

    def test_majority_three_base_one_precious(self):
        crosswalks = self._fake_crosswalks()
        _, grouping = _canonicalize_commodities(["Cu", "Zn", "Fe", "Au"], crosswalks)
        assert grouping == "base_metals"


# ---------------------------------------------------------------------------
# _group_string_to_enum
# ---------------------------------------------------------------------------

class TestGroupStringToEnum:
    @pytest.mark.parametrize("raw,expected", [
        ("Base Metals",       "base_metals"),
        ("base metals",       "base_metals"),
        ("BASE METALS",       "base_metals"),
        ("Precious Metals",   "precious_metals"),
        ("Uranium",           "uranium"),
        ("Potash-Salt",       "potash_salt"),
        ("Potash",            "potash_salt"),
        ("Industrial Materials", "industrial_materials"),
        ("Industrial Minerals",  "industrial_materials"),
        ("Gemstones",         "gemstones"),
        ("Lithium",           "lithium"),
        ("REE",               "ree"),
        ("Rare Earth Elements", "ree"),
        ("Coal",              "coal"),
        ("Other",             "other"),
    ])
    def test_known_strings_map_correctly(self, raw, expected):
        assert _group_string_to_enum(raw) == expected

    def test_case_insensitive(self):
        assert _group_string_to_enum("BASE METALS") == "base_metals"
        assert _group_string_to_enum("rare earth elements") == "ree"

    def test_unknown_string_returns_none(self):
        assert _group_string_to_enum("gibberish") is None

    def test_empty_string_returns_none(self):
        assert _group_string_to_enum("") is None

    def test_whitespace_only_returns_none(self):
        assert _group_string_to_enum("   ") is None


# ---------------------------------------------------------------------------
# CrosswalkSet.resolve_status
# ---------------------------------------------------------------------------

class TestCrosswalkSetResolveStatus:
    def _make_crosswalks(self) -> CrosswalkSet:
        return _make_status_crosswalk([
            ("CA-SK", "mine",               "active",   "producing"),
            ("CA-SK", "mine",               "inactive", "past_producer"),
            ("CA-BC", "mineral_occurrence", "past producer", "past-producer"),
        ])

    def test_exact_key_returns_canonical(self):
        cw = self._make_crosswalks()
        result = cw.resolve_status(
            jurisdiction="CA-SK",
            canonical_type="mine",
            raw="active",
        )
        assert result == "producing"

    def test_case_insensitive_key_lookup(self):
        cw = self._make_crosswalks()
        result = cw.resolve_status(
            jurisdiction="CA-SK",
            canonical_type="mine",
            raw="Active",  # uppercase first letter
        )
        # raw.strip().lower() = "active" which should match
        assert result == "producing"

    def test_miss_returns_default_unknown(self):
        cw = self._make_crosswalks()
        result = cw.resolve_status(
            jurisdiction="CA-SK",
            canonical_type="mine",
            raw="totally_unknown_status",
        )
        assert result == "unknown"

    def test_miss_returns_custom_default(self):
        cw = self._make_crosswalks()
        result = cw.resolve_status(
            jurisdiction="CA-SK",
            canonical_type="mine",
            raw="totally_unknown",
            default="pending",
        )
        assert result == "pending"

    def test_none_raw_returns_default(self):
        cw = self._make_crosswalks()
        result = cw.resolve_status(
            jurisdiction="CA-SK",
            canonical_type="mine",
            raw=None,
        )
        assert result == "unknown"

    def test_different_jurisdiction_miss(self):
        cw = self._make_crosswalks()
        # "active" is only registered for CA-SK; CA-ON should miss
        result = cw.resolve_status(
            jurisdiction="CA-ON",
            canonical_type="mine",
            raw="active",
        )
        assert result == "unknown"

    def test_bc_mineral_occurrence_entry(self):
        cw = self._make_crosswalks()
        result = cw.resolve_status(
            jurisdiction="CA-BC",
            canonical_type="mineral_occurrence",
            raw="past producer",
        )
        assert result == "past-producer"


# ---------------------------------------------------------------------------
# CrosswalkSet.resolve_commodity
# ---------------------------------------------------------------------------

class TestCrosswalkSetResolveCommodity:
    def _make_crosswalks(self) -> CrosswalkSet:
        return _make_crosswalk_set([
            ("au",   "AU",  "Gold",   "precious_metals"),
            ("gold", "AU",  "Gold",   "precious_metals"),
            ("cu",   "CU",  "Copper", "base_metals"),
        ])

    def test_lowercase_alias_resolves(self):
        cw = self._make_crosswalks()
        result = cw.resolve_commodity("au")
        assert result is not None
        assert result.canonical_code == "AU"
        assert result.commodity_grouping == "precious_metals"

    def test_uppercase_input_case_folded(self):
        cw = self._make_crosswalks()
        result = cw.resolve_commodity("AU")
        assert result is not None
        assert result.canonical_code == "AU"

    def test_alias_gold_resolves_to_au_code(self):
        cw = self._make_crosswalks()
        result = cw.resolve_commodity("gold")
        assert result is not None
        assert result.canonical_code == "AU"

    def test_unknown_alias_returns_none(self):
        cw = self._make_crosswalks()
        assert cw.resolve_commodity("unobtanium") is None

    def test_none_input_returns_none(self):
        cw = self._make_crosswalks()
        assert cw.resolve_commodity(None) is None

    def test_empty_string_returns_none(self):
        cw = self._make_crosswalks()
        assert cw.resolve_commodity("") is None

    def test_returns_commodity_record_type(self):
        cw = self._make_crosswalks()
        result = cw.resolve_commodity("cu")
        assert isinstance(result, CommodityRecord)

    def test_commodity_record_fields_populated(self):
        cw = self._make_crosswalks()
        result = cw.resolve_commodity("cu")
        assert result is not None
        assert result.canonical_code == "CU"
        assert result.canonical_name == "Copper"
        assert result.commodity_grouping == "base_metals"

    def test_whitespace_in_input_stripped(self):
        cw = self._make_crosswalks()
        # resolve_commodity does raw.strip().lower() before lookup
        result = cw.resolve_commodity("  AU  ")
        assert result is not None
        assert result.canonical_code == "AU"
