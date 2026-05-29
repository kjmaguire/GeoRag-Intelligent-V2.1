"""Unit tests for FieldMapping registry and field-level helpers in
silver_public_geoscience.py.

Covers:
- _collect_commodity_values (string spec + tuple spec + None spec)
- _parse_boolean_flag (truthy/falsy/free-text)
- _derive_source_feature_id (OBJECTID priority, SK upstream-bug fix, fallback)
- _split_list (delimiter variants, whitespace trimming)
- _parse_date (ISO string, ms-since-epoch int, invalid input)
- _parse_core_availability (canonical output for known encodings + unknown)
- Field mapping registry lookups (MINE, MINERAL_OCCURRENCE, DRILLHOLE)
- Unknown source_id behaviour + fallback functions

Run with:  pytest tests/test_public_geoscience_field_mappings.py -v
"""

from __future__ import annotations

from datetime import date

import pytest

from georag_dagster.assets.silver_public_geoscience import (
    DRILLHOLE_FIELD_MAPPINGS,
    MINE_FIELD_MAPPINGS,
    MINERAL_OCCURRENCE_FIELD_MAPPINGS,
    _collect_commodity_values,
    _derive_source_feature_id,
    _fallback_mineral_occurrence_mapping,
    _parse_boolean_flag,
    _parse_core_availability,
    _parse_date,
    _split_list,
)


# ---------------------------------------------------------------------------
# _collect_commodity_values
# ---------------------------------------------------------------------------

class TestCollectCommodityValues:
    def test_string_spec_delimiter_split(self):
        props = {"PRIMARYCOMMODITIES": "Au, Cu, Zn"}
        result = _collect_commodity_values(props, "PRIMARYCOMMODITIES")
        assert result == ["Au", "Cu", "Zn"]

    def test_string_spec_single_value(self):
        props = {"PRIMARYCOMMODITIES": "Au"}
        result = _collect_commodity_values(props, "PRIMARYCOMMODITIES")
        assert result == ["Au"]

    def test_string_spec_missing_key_returns_empty(self):
        props: dict = {}
        result = _collect_commodity_values(props, "PRIMARYCOMMODITIES")
        assert result == []

    def test_tuple_spec_multi_field_merge(self):
        props = {"F1": "Au", "F2": "", "F3": "Cu"}
        result = _collect_commodity_values(props, ("F1", "F2", "F3"))
        # Empty string for F2 should be skipped
        assert result == ["Au", "Cu"]

    def test_tuple_spec_none_values_skipped(self):
        props = {"F1": "Au", "F2": None, "F3": "Cu"}
        result = _collect_commodity_values(props, ("F1", "F2", "F3"))
        assert result == ["Au", "Cu"]

    def test_tuple_spec_all_empty_returns_empty(self):
        props = {"F1": "", "F2": None, "F3": "   "}
        result = _collect_commodity_values(props, ("F1", "F2", "F3"))
        assert result == []

    def test_none_spec_returns_empty(self):
        props = {"PRIMARYCOMMODITIES": "Au"}
        result = _collect_commodity_values(props, None)
        assert result == []

    def test_list_spec_treated_like_tuple(self):
        props = {"F1": "Au", "F2": "Ag"}
        result = _collect_commodity_values(props, ["F1", "F2"])
        assert result == ["Au", "Ag"]

    def test_bc_minfile_eight_fields(self):
        """BC MINFILE pattern: up to 8 COMMODITY_CODE fields."""
        props = {
            "COMMODITY_CODE1": "Au",
            "COMMODITY_CODE2": "Ag",
            "COMMODITY_CODE3": "Cu",
            "COMMODITY_CODE4": "",
            "COMMODITY_CODE5": None,
            "COMMODITY_CODE6": "",
            "COMMODITY_CODE7": "",
            "COMMODITY_CODE8": "Pb",
        }
        spec = (
            "COMMODITY_CODE1", "COMMODITY_CODE2", "COMMODITY_CODE3",
            "COMMODITY_CODE4", "COMMODITY_CODE5", "COMMODITY_CODE6",
            "COMMODITY_CODE7", "COMMODITY_CODE8",
        )
        result = _collect_commodity_values(props, spec)
        assert result == ["Au", "Ag", "Cu", "Pb"]


# ---------------------------------------------------------------------------
# _parse_boolean_flag
# ---------------------------------------------------------------------------

class TestParseBooleanFlag:
    @pytest.mark.parametrize("raw,expected", [
        ("Y", True),
        ("y", True),
        ("YES", True),
        ("yes", True),
        ("true", True),
        ("True", True),
        ("1", True),
        ("producer", True),
        ("producing", True),
    ])
    def test_truthy_values(self, raw, expected):
        assert _parse_boolean_flag(raw) is expected

    @pytest.mark.parametrize("raw,expected", [
        ("N", False),
        ("n", False),
        ("NO", False),
        ("no", False),
        ("none", False),
        ("false", False),
        ("False", False),
        ("0", False),
        ("", False),
    ])
    def test_falsy_values(self, raw, expected):
        assert _parse_boolean_flag(raw) is expected

    def test_none_returns_false(self):
        assert _parse_boolean_flag(None) is False

    def test_free_text_production_description_returns_true(self):
        # SK SMDI free-text that doesn't match either set falls through to bool(s)
        assert _parse_boolean_flag("Copper production 1962-1967") is True

    def test_free_text_long_description_returns_true(self):
        assert _parse_boolean_flag("Gold and silver mined 1895-1932") is True

    def test_whitespace_only_returns_false(self):
        # Whitespace strips to "" which is in the falsy set
        assert _parse_boolean_flag("   ") is False


# ---------------------------------------------------------------------------
# _derive_source_feature_id
# ---------------------------------------------------------------------------

class TestDeriveSourceFeatureId:
    def test_objectid_nonzero_uses_it(self):
        props = {"OBJECTID": 42, "OBJECTID_1": 99}
        assert _derive_source_feature_id(props) == "42"

    def test_objectid_zero_falls_to_objectid_1(self):
        """SK upstream bug: OBJECTID=0, real id in OBJECTID_1."""
        props = {"OBJECTID": 0, "OBJECTID_1": 38}
        assert _derive_source_feature_id(props) == "38"

    def test_objectid_zero_string_falls_to_objectid_1(self):
        props = {"OBJECTID": "0", "OBJECTID_1": "38"}
        assert _derive_source_feature_id(props) == "38"

    def test_objectid_missing_uses_objectid_1(self):
        props = {"OBJECTID_1": 55}
        assert _derive_source_feature_id(props) == "55"

    def test_both_zero_falls_to_fid(self):
        props = {"OBJECTID": 0, "OBJECTID_1": 0, "FID": 7}
        assert _derive_source_feature_id(props) == "7"

    def test_all_zero_returns_objectid_1_as_last_resort(self):
        # When everything is degenerate, we return oid1 or oid (both "0" here)
        # The implementation returns oid1 or oid — the exact value doesn't matter
        # as long as it's not None (we're preserving the row rather than dropping)
        props = {"OBJECTID": 0, "OBJECTID_1": 0}
        result = _derive_source_feature_id(props)
        # Should return "0" (the oid1 fallback) rather than None
        assert result == "0"

    def test_all_absent_returns_none(self):
        props = {"NAME": "Some Mine"}
        assert _derive_source_feature_id(props) is None

    def test_objectid_string_nonzero(self):
        props = {"OBJECTID": "123"}
        assert _derive_source_feature_id(props) == "123"

    def test_objectid_1_zero_objectid_missing_uses_fid(self):
        props = {"OBJECTID_1": "0", "FID": "12"}
        assert _derive_source_feature_id(props) == "12"


# ---------------------------------------------------------------------------
# _split_list
# ---------------------------------------------------------------------------

class TestSplitList:
    def test_comma_separated(self):
        assert _split_list("Au, Cu, Zn") == ["Au", "Cu", "Zn"]

    def test_semicolon_separated(self):
        assert _split_list("Au;Cu;Zn") == ["Au", "Cu", "Zn"]

    def test_pipe_separated(self):
        assert _split_list("Au|Cu|Zn") == ["Au", "Cu", "Zn"]

    def test_slash_separated(self):
        assert _split_list("Au/Cu/Zn") == ["Au", "Cu", "Zn"]

    def test_whitespace_trimmed(self):
        assert _split_list("  Au ,  Cu ,  Zn  ") == ["Au", "Cu", "Zn"]

    def test_empty_string_returns_empty(self):
        assert _split_list("") == []

    def test_none_returns_empty(self):
        assert _split_list(None) == []

    def test_whitespace_only_returns_empty(self):
        assert _split_list("   ") == []

    def test_single_value_no_delimiter(self):
        assert _split_list("Gold") == ["Gold"]

    def test_list_input_passthrough(self):
        assert _split_list(["Au", "Cu"]) == ["Au", "Cu"]

    def test_list_with_none_elements_stripped(self):
        assert _split_list(["Au", None, "Cu"]) == ["Au", "Cu"]


# ---------------------------------------------------------------------------
# _parse_date
# ---------------------------------------------------------------------------

class TestParseDate:
    def test_iso_date_string(self):
        assert _parse_date("2024-03-15") == date(2024, 3, 15)

    def test_iso_date_slash_separator(self):
        assert _parse_date("2024/03/15") == date(2024, 3, 15)

    def test_iso_datetime_string(self):
        assert _parse_date("2024-03-15T14:30:00") == date(2024, 3, 15)

    def test_milliseconds_since_epoch(self):
        # 1712534400000 ms = 2024-04-08 UTC
        result = _parse_date(1712534400000)
        assert isinstance(result, date)
        assert result.year == 2024

    def test_none_returns_none(self):
        assert _parse_date(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_date("") is None

    def test_invalid_string_returns_none(self):
        assert _parse_date("not-a-date") is None

    def test_float_ms_epoch(self):
        result = _parse_date(1712534400000.0)
        assert isinstance(result, date)

    def test_zero_ms_epoch(self):
        # epoch zero = 1970-01-01
        result = _parse_date(0)
        assert result == date(1970, 1, 1)


# ---------------------------------------------------------------------------
# _parse_core_availability
# ---------------------------------------------------------------------------

class TestParseCoreAvailability:
    @pytest.mark.parametrize("raw,expected", [
        ("Available", "available"),
        ("available", "available"),
        ("Yes", "available"),
        ("yes", "available"),
        ("AVAILABLE", "available"),
    ])
    def test_available_variants(self, raw, expected):
        assert _parse_core_availability(raw) == expected

    def test_partial(self):
        assert _parse_core_availability("Partial") == "partial"

    def test_partial_lowercase(self):
        assert _parse_core_availability("partial availability") == "partial"

    @pytest.mark.parametrize("raw,expected", [
        ("Unavailable", "unavailable"),
        ("No", "unavailable"),
        ("no", "unavailable"),
        ("Lost", "unavailable"),
        ("No core", "unavailable"),
    ])
    def test_unavailable_variants(self, raw, expected):
        assert _parse_core_availability(raw) == expected

    def test_unknown_value_returns_unknown(self):
        assert _parse_core_availability("maybe") == "unknown"

    def test_none_returns_unknown(self):
        assert _parse_core_availability(None) == "unknown"

    def test_empty_string_returns_unknown(self):
        assert _parse_core_availability("") == "unknown"


# ---------------------------------------------------------------------------
# Field mapping registry lookups
# ---------------------------------------------------------------------------

class TestMineFieldMappingRegistry:
    def test_ca_sk_mine_loc_name_field(self):
        mapping = MINE_FIELD_MAPPINGS["CA-SK-MINE-LOC"]
        assert mapping.name_field == "NAME"

    def test_ca_sk_mine_loc_status_field(self):
        mapping = MINE_FIELD_MAPPINGS["CA-SK-MINE-LOC"]
        assert mapping.status_field == "STATUS"

    def test_ca_sk_mine_loc_commodities_field(self):
        mapping = MINE_FIELD_MAPPINGS["CA-SK-MINE-LOC"]
        assert mapping.commodities_field == "COMMODITY"

    def test_unknown_source_returns_none(self):
        assert MINE_FIELD_MAPPINGS.get("CA-XX-UNKNOWN") is None


class TestMineralOccurrenceFieldMappingRegistry:
    def test_ca_bc_minfile_external_id_field(self):
        mapping = MINERAL_OCCURRENCE_FIELD_MAPPINGS["CA-BC-MINFILE"]
        assert mapping.external_id_field == "MINFILE_NUMBER"

    def test_ca_bc_minfile_primary_commodities_is_tuple_of_eight(self):
        mapping = MINERAL_OCCURRENCE_FIELD_MAPPINGS["CA-BC-MINFILE"]
        assert isinstance(mapping.primary_commodities_field, tuple)
        assert len(mapping.primary_commodities_field) == 8

    def test_ca_sk_smdi_external_id_field(self):
        mapping = MINERAL_OCCURRENCE_FIELD_MAPPINGS["CA-SK-SMDI"]
        assert mapping.external_id_field == "SMDI"

    def test_unknown_source_returns_none(self):
        assert MINERAL_OCCURRENCE_FIELD_MAPPINGS.get("CA-XX-UNKNOWN") is None


class TestDrillholeFieldMappingRegistry:
    def test_ca_sk_drillhole_strat_depth_fields_has_four_keys(self):
        mapping = DRILLHOLE_FIELD_MAPPINGS["CA-SK-DRILLHOLE"]
        assert len(mapping.strat_depth_fields) == 4

    def test_ca_sk_drillhole_strat_depth_keys(self):
        mapping = DRILLHOLE_FIELD_MAPPINGS["CA-SK-DRILLHOLE"]
        expected_keys = {
            "base_quaternary_m",
            "base_phanerozoic_m",
            "base_athabasca_m",
            "top_basement_m",
        }
        assert set(mapping.strat_depth_fields.keys()) == expected_keys

    def test_unknown_source_returns_none(self):
        """Drillhole mapping has NO fallback — None is the correct return."""
        assert DRILLHOLE_FIELD_MAPPINGS.get("CA-XX-UNKNOWN") is None


# ---------------------------------------------------------------------------
# Fallback functions
# ---------------------------------------------------------------------------

class TestFallbackMappings:
    def test_fallback_mineral_occurrence_returns_working_mapping(self):
        mapping = _fallback_mineral_occurrence_mapping()
        # Should have sensible defaults — name_field at minimum must be set
        assert mapping.name_field == "NAME"
        assert mapping.status_field == "STATUS"
        assert mapping.primary_commodities_field == "COMMODITIES"
        # external_id_field is None (no jurisdiction-native ID for unknown sources)
        assert mapping.external_id_field is None

    def test_drillhole_has_no_fallback(self):
        """There is no _fallback_drillhole_mapping — the module intentionally
        omits it so unknown drillhole sources fail loudly (logged once, then
        every feature returns None). Verify the registry miss returns None
        rather than raising."""
        result = DRILLHOLE_FIELD_MAPPINGS.get("CA-DOES-NOT-EXIST")
        assert result is None
