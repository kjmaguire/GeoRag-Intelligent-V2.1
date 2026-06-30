"""Tests for Sprint 5 Phase 1: vendor_profile_id metadata extraction and sensor plumbing.

Covers:
  - _extract_vendor_profile_id unit tests (10 cases)
  - _build_sensor_run_config unit tests (4 cases, fully pure function)

Target: 717 existing + 14 new tests green, 0 xfails.
"""

import logging


from georag_dagster.definitions import (
    _build_sensor_run_config,
    _extract_vendor_profile_id,
)


# ---------------------------------------------------------------------------
# Unit tests for _extract_vendor_profile_id
# ---------------------------------------------------------------------------


class TestExtractVendorProfileId:

    def test_minio_style_key_valid_int(self):
        metadata = {"x-amz-meta-x-georag-vendor-profile-id": "42"}
        assert _extract_vendor_profile_id(metadata) == 42

    def test_boto3_style_key_valid_int(self):
        metadata = {"x-georag-vendor-profile-id": "7"}
        assert _extract_vendor_profile_id(metadata) == 7

    def test_value_stored_as_list_returns_int(self):
        metadata = {"x-amz-meta-x-georag-vendor-profile-id": ["99"]}
        assert _extract_vendor_profile_id(metadata) == 99

    def test_non_int_string_returns_none_and_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = _extract_vendor_profile_id({"x-georag-vendor-profile-id": "not-an-int"})
        assert result is None
        assert any(
            "is not a valid integer" in r.message and "not-an-int" in r.message
            for r in caplog.records
        )

    def test_key_absent_returns_none(self):
        metadata = {"content-type": "text/csv", "etag": "abc123"}
        assert _extract_vendor_profile_id(metadata) is None

    def test_none_metadata_returns_none(self):
        assert _extract_vendor_profile_id(None) is None

    def test_empty_dict_returns_none(self):
        assert _extract_vendor_profile_id({}) is None

    def test_case_insensitive_key_lookup(self):
        metadata = {"X-Amz-Meta-X-GeoRAG-Vendor-Profile-Id": "5"}
        assert _extract_vendor_profile_id(metadata) == 5

    def test_whitespace_stripped_before_int_parse(self):
        metadata = {"x-georag-vendor-profile-id": "  123  "}
        assert _extract_vendor_profile_id(metadata) == 123

    def test_empty_list_value_returns_none(self):
        metadata = {"x-amz-meta-x-georag-vendor-profile-id": []}
        assert _extract_vendor_profile_id(metadata) is None


# ---------------------------------------------------------------------------
# Unit tests for _build_sensor_run_config
#
# This is a pure function that builds the run_config dict the sensor passes
# to RunRequest.  Testing it directly avoids Dagster context/type-check
# overhead while fully exercising the Sprint 5 Phase 1 logic.
# ---------------------------------------------------------------------------


class TestBuildSensorRunConfig:

    def test_vendor_profile_id_included_for_triggered_asset(self):
        """Triggered bronze_collars + vendor_profile_id=7 appears in silver_collars config."""
        rc = _build_sensor_run_config(
            triggered_assets={"bronze_collars"},
            asset_vendor_profile={"bronze_collars": 7},
        )
        assert rc == {"ops": {"silver_collars": {"config": {"vendor_profile_id": 7}}}}

    def test_none_vendor_profile_id_passes_through(self):
        """Absent vendor_profile_id yields None in the config (backward compat)."""
        rc = _build_sensor_run_config(
            triggered_assets={"bronze_samples"},
            asset_vendor_profile={"bronze_samples": None},
        )
        assert rc == {"ops": {"silver_samples": {"config": {"vendor_profile_id": None}}}}

    def test_multiple_assets_all_included(self):
        """Multiple triggered assets each get their own silver ops entry."""
        rc = _build_sensor_run_config(
            triggered_assets={"bronze_collars", "bronze_samples"},
            asset_vendor_profile={"bronze_collars": 3, "bronze_samples": 3},
        )
        ops = rc.get("ops", {})
        assert "silver_collars" in ops
        assert "silver_samples" in ops
        assert ops["silver_collars"]["config"]["vendor_profile_id"] == 3
        assert ops["silver_samples"]["config"]["vendor_profile_id"] == 3

    def test_no_silver_mapping_returns_empty_dict(self):
        """An asset key with no silver counterpart yields an empty run_config dict."""
        rc = _build_sensor_run_config(
            triggered_assets={"bronze_unknown_type"},
            asset_vendor_profile={"bronze_unknown_type": 5},
        )
        assert rc == {}
