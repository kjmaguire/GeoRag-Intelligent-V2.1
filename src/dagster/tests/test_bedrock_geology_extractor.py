"""Unit tests for _extract_bedrock_geology and SPEC_BEDROCK_GEOLOGY.

Covers:
- Happy-path: all source fields present → all canonical columns populated
- unit_name derivation: NAME non-blank → uses NAME
- unit_name derivation: NAME blank → formation + ' / ' + member
- unit_name derivation: NAME blank + FORMATION blank + MEMBER blank → unit_code
- unit_name derivation: NAME blank + FORMATION present + MEMBER blank → formation only
- All optional fields blank (just ROCK_CODE) → all nullable columns are None
- scale is always '250K' regardless of input
- GROUP_ source field → canonical group_name
- DOMAIN source field → canonical structural_domain
- Whitespace-only field values normalised to None
- Missing ROCK_CODE → extractor returns None
- SPEC_BEDROCK_GEOLOGY registration: target_table, geometry_type, canonical_columns length
- _stage_column_type: correct VARCHAR/TEXT types for each bedrock column

Run with:  pytest tests/test_bedrock_geology_extractor.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from georag_dagster.assets.silver_public_geoscience import (
    SPEC_BEDROCK_GEOLOGY,
    CrosswalkSet,
    _extract_bedrock_geology,
    _stage_column_type,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def empty_crosswalks() -> CrosswalkSet:
    return CrosswalkSet(commodity={}, status={})


def _make_ctx(source_crs: int = 3154) -> dict:
    """Build a minimal ctx dict with a Reprojector mock that passes geometry through."""
    reprojector = MagicMock()
    # Return stable WKT stubs so we can assert the record is non-None
    reprojector.transform.return_value = (
        "MULTIPOLYGON(((0 0,1 0,1 1,0 1,0 0)))",  # src_wkt
        "MULTIPOLYGON(((0 0,1 0,1 1,0 1,0 0)))",  # tgt_wkt (mocked)
    )
    return {
        "reprojector": reprojector,
        "source_crs": source_crs,
        "jurisdiction_code": "CA-SK",
    }


_FULL_PROPS = {
    "OBJECTID":   1,
    "ROCK_CODE":  "MCob",
    "NAME":       "Otherside Formation",
    "EON":        "Proterozoic",
    "ERA":        "Meso to Paleoproterozoic",
    "PERIOD":     "Statherian to Calymmian",
    "GROUP_":     "McFarlane",
    "FORMATION":  "Otherside",
    "MEMBER":     "Birkbeck",
    "DOMAIN":     "Athabasca Basin",
    "LITHOLOGY":  "Quartz arenite",
}

_GEOM = {"type": "MultiPolygon", "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]]}

SOURCE_ID = "CA-SK-GEOLOGY-BEDROCK-250K"


# ---------------------------------------------------------------------------
# Happy-path: all fields present
# ---------------------------------------------------------------------------

class TestExtractBedrockGeologyHappyPath:
    def test_returns_canonical_record_not_none(self, empty_crosswalks):
        record = _extract_bedrock_geology(
            SOURCE_ID, dict(_FULL_PROPS), _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record is not None

    def test_unit_code_populated(self, empty_crosswalks):
        record = _extract_bedrock_geology(
            SOURCE_ID, dict(_FULL_PROPS), _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["unit_code"] == "MCob"

    def test_unit_name_uses_name_when_non_blank(self, empty_crosswalks):
        record = _extract_bedrock_geology(
            SOURCE_ID, dict(_FULL_PROPS), _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["unit_name"] == "Otherside Formation"

    def test_eon_populated(self, empty_crosswalks):
        record = _extract_bedrock_geology(
            SOURCE_ID, dict(_FULL_PROPS), _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["eon"] == "Proterozoic"

    def test_era_populated(self, empty_crosswalks):
        record = _extract_bedrock_geology(
            SOURCE_ID, dict(_FULL_PROPS), _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["era"] == "Meso to Paleoproterozoic"

    def test_period_populated(self, empty_crosswalks):
        record = _extract_bedrock_geology(
            SOURCE_ID, dict(_FULL_PROPS), _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["period"] == "Statherian to Calymmian"

    def test_group_underscore_maps_to_group_name(self, empty_crosswalks):
        """GROUP_ source field (trailing underscore) → canonical group_name."""
        record = _extract_bedrock_geology(
            SOURCE_ID, dict(_FULL_PROPS), _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["group_name"] == "McFarlane"

    def test_formation_populated(self, empty_crosswalks):
        record = _extract_bedrock_geology(
            SOURCE_ID, dict(_FULL_PROPS), _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["formation"] == "Otherside"

    def test_member_populated(self, empty_crosswalks):
        record = _extract_bedrock_geology(
            SOURCE_ID, dict(_FULL_PROPS), _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["member"] == "Birkbeck"

    def test_domain_maps_to_structural_domain(self, empty_crosswalks):
        """DOMAIN source field → canonical structural_domain."""
        record = _extract_bedrock_geology(
            SOURCE_ID, dict(_FULL_PROPS), _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["structural_domain"] == "Athabasca Basin"

    def test_lithology_populated(self, empty_crosswalks):
        record = _extract_bedrock_geology(
            SOURCE_ID, dict(_FULL_PROPS), _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["lithology"] == "Quartz arenite"

    def test_scale_is_250k_constant(self, empty_crosswalks):
        """scale is always '250K' — it is a constant, not read from source."""
        record = _extract_bedrock_geology(
            SOURCE_ID, dict(_FULL_PROPS), _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["scale"] == "250K"

    def test_source_id_on_record(self, empty_crosswalks):
        record = _extract_bedrock_geology(
            SOURCE_ID, dict(_FULL_PROPS), _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.source_id == SOURCE_ID

    def test_jurisdiction_code_on_record(self, empty_crosswalks):
        record = _extract_bedrock_geology(
            SOURCE_ID, dict(_FULL_PROPS), _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.jurisdiction_code == "CA-SK"


# ---------------------------------------------------------------------------
# unit_name fallback chain
# ---------------------------------------------------------------------------

class TestUnitNameFallback:
    def test_name_blank_whitespace_falls_to_formation_slash_member(self, empty_crosswalks):
        """NAME is whitespace only → unit_name = formation + ' / ' + member."""
        props = dict(_FULL_PROPS)
        props["NAME"] = "   "
        record = _extract_bedrock_geology(
            SOURCE_ID, props, _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["unit_name"] == "Otherside / Birkbeck"

    def test_name_blank_no_member_falls_to_formation_only(self, empty_crosswalks):
        """NAME blank + MEMBER absent → unit_name = formation (no slash)."""
        props = dict(_FULL_PROPS)
        props["NAME"] = ""
        props["MEMBER"] = ""
        record = _extract_bedrock_geology(
            SOURCE_ID, props, _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["unit_name"] == "Otherside"

    def test_name_blank_formation_blank_member_blank_falls_to_unit_code(self, empty_crosswalks):
        """NAME blank + FORMATION blank + MEMBER blank → unit_name = unit_code."""
        props = dict(_FULL_PROPS)
        props["NAME"] = ""
        props["FORMATION"] = ""
        props["MEMBER"] = ""
        record = _extract_bedrock_geology(
            SOURCE_ID, props, _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["unit_name"] == "MCob"

    def test_name_absent_falls_to_formation_slash_member(self, empty_crosswalks):
        """NAME key missing entirely → same fallback as blank NAME."""
        props = {k: v for k, v in _FULL_PROPS.items() if k != "NAME"}
        record = _extract_bedrock_geology(
            SOURCE_ID, props, _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["unit_name"] == "Otherside / Birkbeck"


# ---------------------------------------------------------------------------
# All optional fields blank
# ---------------------------------------------------------------------------

class TestAllOptionalFieldsBlank:
    def test_nullable_columns_all_none_when_only_rock_code_present(self, empty_crosswalks):
        """Only OBJECTID + ROCK_CODE → all nullable canonical columns are None."""
        props = {"OBJECTID": 42, "ROCK_CODE": "X"}
        record = _extract_bedrock_geology(
            SOURCE_ID, props, _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record is not None
        attrs = record.canonical_attrs
        assert attrs["eon"] is None
        assert attrs["era"] is None
        assert attrs["period"] is None
        assert attrs["group_name"] is None
        assert attrs["formation"] is None
        assert attrs["member"] is None
        assert attrs["structural_domain"] is None
        assert attrs["lithology"] is None

    def test_scale_still_250k_with_minimal_props(self, empty_crosswalks):
        props = {"OBJECTID": 42, "ROCK_CODE": "X"}
        record = _extract_bedrock_geology(
            SOURCE_ID, props, _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["scale"] == "250K"


# ---------------------------------------------------------------------------
# scale constant
# ---------------------------------------------------------------------------

class TestScaleConstant:
    def test_scale_is_250k_ignores_any_source_field(self, empty_crosswalks):
        """Even if a hypothetical SCALE source field existed it must be ignored."""
        props = dict(_FULL_PROPS)
        props["SCALE"] = "1M"  # hypothetical — not in the real source
        record = _extract_bedrock_geology(
            SOURCE_ID, props, _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs["scale"] == "250K"


# ---------------------------------------------------------------------------
# Whitespace normalisation
# ---------------------------------------------------------------------------

class TestWhitespaceNormalisation:
    @pytest.mark.parametrize("field_name,canon_key", [
        ("EON",       "eon"),
        ("ERA",       "era"),
        ("PERIOD",    "period"),
        ("GROUP_",    "group_name"),
        ("FORMATION", "formation"),
        ("MEMBER",    "member"),
        ("DOMAIN",    "structural_domain"),
        ("LITHOLOGY", "lithology"),
    ])
    def test_whitespace_only_source_value_normalised_to_none(
        self, field_name, canon_key, empty_crosswalks
    ):
        props = dict(_FULL_PROPS)
        props[field_name] = "   "
        record = _extract_bedrock_geology(
            SOURCE_ID, props, _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record.canonical_attrs[canon_key] is None, (
            f"{field_name} whitespace-only should normalise to None in {canon_key}"
        )


# ---------------------------------------------------------------------------
# Missing ROCK_CODE → None (NOT NULL guard)
# ---------------------------------------------------------------------------

class TestMissingRockCode:
    def test_missing_rock_code_returns_none(self, empty_crosswalks):
        props = {"OBJECTID": 1, "EON": "Proterozoic"}  # no ROCK_CODE
        record = _extract_bedrock_geology(
            SOURCE_ID, props, _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record is None

    def test_whitespace_only_rock_code_returns_none(self, empty_crosswalks):
        props = {"OBJECTID": 1, "ROCK_CODE": "   "}
        record = _extract_bedrock_geology(
            SOURCE_ID, props, _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record is None

    def test_missing_objectid_returns_none(self, empty_crosswalks):
        """No OBJECTID / OBJECTID_1 / FID → _derive_source_feature_id returns None → None."""
        props = {"ROCK_CODE": "MCob", "EON": "Proterozoic"}
        record = _extract_bedrock_geology(
            SOURCE_ID, props, _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record is None


# ---------------------------------------------------------------------------
# SPEC_BEDROCK_GEOLOGY registration
# ---------------------------------------------------------------------------

class TestSpecBedrockGeology:
    def test_target_table(self):
        assert SPEC_BEDROCK_GEOLOGY.target_table == "public_geoscience.pg_bedrock_geology"

    def test_history_table(self):
        assert SPEC_BEDROCK_GEOLOGY.history_table == "public_geoscience.pg_bedrock_geology_history"

    def test_geometry_type_is_multipolygon(self):
        assert SPEC_BEDROCK_GEOLOGY.geometry_type == "MULTIPOLYGON"

    def test_canonical_columns_length_is_11(self):
        assert len(SPEC_BEDROCK_GEOLOGY.canonical_columns) == 11

    def test_canonical_columns_contains_all_expected(self):
        expected = {
            "unit_code", "unit_name", "eon", "era", "period",
            "group_name", "formation", "member",
            "structural_domain", "lithology", "scale",
        }
        assert set(SPEC_BEDROCK_GEOLOGY.canonical_columns) == expected

    def test_extractor_is_callable(self):
        assert callable(SPEC_BEDROCK_GEOLOGY.extractor)

    def test_extractor_returns_canonical_record_for_valid_input(self, empty_crosswalks):
        """SPEC extractor lambda wires correctly to _extract_bedrock_geology."""
        record = SPEC_BEDROCK_GEOLOGY.extractor(
            SOURCE_ID, dict(_FULL_PROPS), _GEOM, _make_ctx(), empty_crosswalks
        )
        assert record is not None
        assert record.canonical_attrs["unit_code"] == "MCob"


# ---------------------------------------------------------------------------
# _stage_column_type for bedrock columns
# ---------------------------------------------------------------------------

class TestStageColumnTypeBedrockGeology:
    @pytest.mark.parametrize("col,expected_type", [
        ("unit_code",         "VARCHAR(16)"),
        ("unit_name",         "VARCHAR(128)"),
        ("eon",               "VARCHAR(32)"),
        ("era",               "VARCHAR(64)"),
        ("period",            "VARCHAR(64)"),
        ("group_name",        "VARCHAR(64)"),
        ("formation",         "VARCHAR(64)"),
        ("member",            "VARCHAR(64)"),
        ("structural_domain", "VARCHAR(64)"),
        ("lithology",         "VARCHAR(256)"),
        ("scale",             "VARCHAR(8)"),
    ])
    def test_stage_column_type(self, col, expected_type):
        result = _stage_column_type(col, SPEC_BEDROCK_GEOLOGY)
        assert result == expected_type, (
            f"_stage_column_type('{col}', SPEC_BEDROCK_GEOLOGY) "
            f"returned '{result}', expected '{expected_type}'"
        )
