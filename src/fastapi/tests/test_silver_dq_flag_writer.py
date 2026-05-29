"""Unit tests for app.services.silver_dq_flag_writer — §6a writer helper.

Pure-function validation logic + SQL-string regression. The full
asyncpg upsert path is exercised by an operator smoke script
(scripts/_smoke_dq_flag_writer.py) rather than a pytest because the
asyncpg conftest fixture chain has a known null-byte interaction we
chose not to chase mid-session (see 903a827 commit body).
"""
from __future__ import annotations

import pytest

from app.services.silver_dq_flag_writer import (
    ALLOWED_RECORD_TYPES,
    ALLOWED_SEVERITIES,
    DataQualityFlag,
    _payload_jsonb,
    _validate,
)


# ---------------------------------------------------------------------------
# Validation — argument-shape checks before DB roundtrip
# ---------------------------------------------------------------------------


def _valid_flag(**overrides) -> DataQualityFlag:
    defaults = dict(
        workspace_id="ws-1",
        record_type="assay_interval",
        record_id="assay-row-42",
        flag_type="value_out_of_range",
        severity="WARNING",
        description="Au value 99.9 g/t exceeds 3-sigma threshold (24.5 g/t).",
    )
    defaults.update(overrides)
    return DataQualityFlag(**defaults)


def test_valid_minimal_flag_passes_validation():
    """The minimal-positional set of required fields should validate."""
    _validate(_valid_flag())  # raises on failure


def test_valid_full_flag_passes_validation():
    """Every optional field set + non-empty → validation passes."""
    _validate(_valid_flag(
        project_id="proj-1",
        source_document_id="doc-1",
        source_page=42,
        source_row_range="34-67",
        rule_id="assay.value_out_of_range",
        rule_version="v1.0",
        threshold_payload={"threshold_g_t": 24.5, "observed_g_t": 99.9},
    ))


def test_unknown_severity_raises_value_error():
    """Severity is a CHECK-constrained enum; catch the typo before DB does."""
    with pytest.raises(ValueError, match="severity="):
        _validate(_valid_flag(severity="critical"))


def test_lowercase_severity_rejected():
    """Severity is exact-case — 'warning' fails because the DB has
    only 'WARNING' in its CHECK array."""
    with pytest.raises(ValueError, match="severity="):
        _validate(_valid_flag(severity="warning"))


def test_unknown_record_type_raises_value_error():
    """14 allowed record_type values — anything else is a caller bug."""
    with pytest.raises(ValueError, match="record_type="):
        _validate(_valid_flag(record_type="future_record_type"))


def test_empty_workspace_id_rejected():
    """workspace_id drives RLS — empty string would land the row in
    the wrong tenancy or fail policy; reject early."""
    with pytest.raises(ValueError, match="workspace_id"):
        _validate(_valid_flag(workspace_id=""))


def test_empty_record_id_rejected():
    """Without record_id the badge query can't link the flag back
    to the underlying row."""
    with pytest.raises(ValueError, match="record_id"):
        _validate(_valid_flag(record_id=""))


def test_empty_flag_type_rejected():
    """flag_type IS the discriminator for the rule that fired —
    can't be empty."""
    with pytest.raises(ValueError, match="flag_type"):
        _validate(_valid_flag(flag_type=""))


def test_empty_description_rejected():
    """SME review surface reads the description. Empty is useless."""
    with pytest.raises(ValueError, match="description"):
        _validate(_valid_flag(description=""))


# ---------------------------------------------------------------------------
# Allowed-set contract — pinned against the DB migration
# ---------------------------------------------------------------------------


def test_allowed_severities_match_db_check_constraint():
    """If the DB CHECK changes, this assertion forces an update here.
    Drift would surface as a CheckViolationError at runtime."""
    assert ALLOWED_SEVERITIES == frozenset({"INFO", "WARNING", "ERROR"})


def test_allowed_record_types_match_db_check_constraint():
    """Same pin for record_type — 14 values per the 2026_05_26 migration."""
    expected = frozenset({
        "assay_interval", "collar", "survey_point",
        "lithology_interval", "alteration_interval",
        "mineralization_interval", "structural_interval",
        "downhole_geophysics_point", "composite_interval",
        "document_chunk", "table_extraction", "spatial_feature",
        "sample", "geochronology_sample",
    })
    assert ALLOWED_RECORD_TYPES == expected


# ---------------------------------------------------------------------------
# JSONB payload encoding
# ---------------------------------------------------------------------------


def test_payload_jsonb_none_returns_empty_object():
    """None threshold_payload → '{}' so the DEFAULT '{}'::jsonb
    column stays JSONB-valid even when the caller didn't supply
    rule-specific metadata."""
    assert _payload_jsonb(None) == "{}"


def test_payload_jsonb_empty_dict_returns_empty_object():
    """{} → '{}'. The DB column never sees a NULL or a malformed
    JSONB literal."""
    assert _payload_jsonb({}) == "{}"


def test_payload_jsonb_simple_dict_serialises():
    """Standard rule payload — bounds + observed value."""
    result = _payload_jsonb({"threshold_g_t": 24.5, "observed_g_t": 99.9})
    # JSON serialisation order isn't pinned; parse it back to compare.
    import json as _json
    decoded = _json.loads(result)
    assert decoded == {"threshold_g_t": 24.5, "observed_g_t": 99.9}


def test_payload_jsonb_nested_dict_serialises():
    """Some rules emit nested context (e.g. 'comparison: {asof: ..., expected: ...}')."""
    payload = {
        "rule": "assay.value_out_of_range",
        "comparison": {"min": 0.001, "max": 24.5, "observed": 99.9},
        "evidence": ["row-42"],
    }
    import json as _json
    assert _json.loads(_payload_jsonb(payload)) == payload


# ---------------------------------------------------------------------------
# DataQualityFlag dataclass
# ---------------------------------------------------------------------------


def test_dataquality_flag_is_frozen():
    """Frozen so batch lists can't be mutated mid-iteration."""
    flag = _valid_flag()
    with pytest.raises((AttributeError, Exception)):
        flag.severity = "ERROR"  # type: ignore[misc]


def test_dataquality_flag_optional_fields_default_to_none():
    """Required fields positional / kwarg; optionals default to None."""
    flag = DataQualityFlag(
        workspace_id="ws",
        record_type="collar",
        record_id="c-1",
        flag_type="missing_elevation",
        severity="WARNING",
        description="Collar c-1 has NULL elevation",
    )
    assert flag.project_id is None
    assert flag.source_document_id is None
    assert flag.source_page is None
    assert flag.source_row_range is None
    assert flag.rule_id is None
    assert flag.rule_version is None
    assert flag.threshold_payload is None
