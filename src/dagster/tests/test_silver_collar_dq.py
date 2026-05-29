"""Unit tests for the §6a collar validation rule family.

`evaluate_collar_row` is pure-function — one collar dict in,
list[DataQualityFlag] out — so unit-testable without spinning up
Dagster + Postgres.

The Dagster asset wrapper (silver_collar_dq) is exercised in dev
via materialization on the live DB; a full integration test
would need a fixture DB which is out of scope this session.
"""
from __future__ import annotations


def _import_evaluator():
    """Import the evaluator at first-call time so pytest collection
    doesn't fail when the dagster package isn't on sys.path (e.g. an
    accidental run from the FastAPI container where it isn't mounted)."""
    from georag_dagster.assets.silver_collar_dq import (
        evaluate_collar_row, RULE_VERSION,
    )
    return evaluate_collar_row, RULE_VERSION


def _row(**overrides) -> dict:
    """Build a minimal-valid collar row dict — all fields populated
    with sensible defaults so each test only has to override the
    field under test."""
    defaults = dict(
        collar_id="c-1",
        workspace_id="ws-1",
        project_id="proj-1",
        hole_id="ECK-22-001",
        elevation=300.0,
        azimuth=180.0,
        dip=-60.0,
        total_depth=350.0,
        bronze_source_id=None,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Happy path — fully-populated collar produces no flags
# ---------------------------------------------------------------------------


def test_well_populated_collar_emits_no_flags():
    """The geologist's dream: every field present + in range → no flags."""
    evaluate, _ = _import_evaluator()
    assert evaluate(_row()) == []


# ---------------------------------------------------------------------------
# Rule 1 — dip range
# ---------------------------------------------------------------------------


def test_positive_dip_emits_invalid_dip_range_error():
    """Drilled holes go DOWN — a positive dip is physically impossible."""
    evaluate, _ = _import_evaluator()
    flags = evaluate(_row(dip=15.0))
    assert len(flags) == 1
    assert flags[0].flag_type == "collar.invalid_dip_range"
    assert flags[0].severity == "ERROR"
    assert flags[0].threshold_payload == {"min": -90, "max": 0, "observed": 15.0}


def test_dip_below_minus_90_emits_invalid_dip_range_error():
    """Below -90° is over-vertical, also invalid."""
    evaluate, _ = _import_evaluator()
    flags = evaluate(_row(dip=-95.0))
    assert any(f.flag_type == "collar.invalid_dip_range" for f in flags)


def test_dip_at_boundary_emits_no_dip_range_flag():
    """0° and -90° are valid boundaries (vertical hole + horizontal hole)."""
    evaluate, _ = _import_evaluator()
    for dip in (0.0, -90.0):
        flags = evaluate(_row(dip=dip))
        assert not any(f.flag_type == "collar.invalid_dip_range" for f in flags)


# ---------------------------------------------------------------------------
# Rule 2 — azimuth range
# ---------------------------------------------------------------------------


def test_negative_azimuth_emits_invalid_azimuth_range_error():
    evaluate, _ = _import_evaluator()
    flags = evaluate(_row(azimuth=-1.0))
    assert any(f.flag_type == "collar.invalid_azimuth_range" for f in flags)


def test_azimuth_360_emits_invalid_azimuth_range_error():
    """Convention is [0, 360) — 360 is OUT (== 0)."""
    evaluate, _ = _import_evaluator()
    flags = evaluate(_row(azimuth=360.0))
    assert any(f.flag_type == "collar.invalid_azimuth_range" for f in flags)


def test_azimuth_359_99_emits_no_range_flag():
    evaluate, _ = _import_evaluator()
    flags = evaluate(_row(azimuth=359.99))
    assert not any(f.flag_type == "collar.invalid_azimuth_range" for f in flags)


# ---------------------------------------------------------------------------
# Rule 3 — total_depth
# ---------------------------------------------------------------------------


def test_null_total_depth_emits_invalid_total_depth_error():
    evaluate, _ = _import_evaluator()
    flags = evaluate(_row(total_depth=None))
    assert any(f.flag_type == "collar.invalid_total_depth" for f in flags)


def test_zero_total_depth_emits_invalid_total_depth_error():
    """A 0-depth hole means we didn't drill — almost certainly a
    data-entry error."""
    evaluate, _ = _import_evaluator()
    flags = evaluate(_row(total_depth=0))
    assert any(f.flag_type == "collar.invalid_total_depth" for f in flags)


def test_negative_total_depth_emits_invalid_total_depth_error():
    evaluate, _ = _import_evaluator()
    flags = evaluate(_row(total_depth=-50.0))
    assert any(f.flag_type == "collar.invalid_total_depth" for f in flags)


# ---------------------------------------------------------------------------
# Rules 4-6 — missing-data INFOs + WARNING
# ---------------------------------------------------------------------------


def test_null_elevation_emits_warning():
    """WARNING (not ERROR) — legacy ingests can have NULL elevation;
    SMEs back-fill from the source PDF."""
    evaluate, _ = _import_evaluator()
    flags = evaluate(_row(elevation=None))
    missing = [f for f in flags if f.flag_type == "collar.missing_elevation"]
    assert len(missing) == 1
    assert missing[0].severity == "WARNING"


def test_null_azimuth_emits_info():
    """INFO — vertical holes don't need azimuth, so this is observation
    not error."""
    evaluate, _ = _import_evaluator()
    flags = evaluate(_row(azimuth=None))
    missing = [f for f in flags if f.flag_type == "collar.missing_azimuth"]
    assert len(missing) == 1
    assert missing[0].severity == "INFO"


def test_null_dip_emits_info():
    evaluate, _ = _import_evaluator()
    flags = evaluate(_row(dip=None))
    missing = [f for f in flags if f.flag_type == "collar.missing_dip"]
    assert len(missing) == 1
    assert missing[0].severity == "INFO"


# ---------------------------------------------------------------------------
# Composite — many issues on one row
# ---------------------------------------------------------------------------


def test_completely_broken_row_emits_all_critical_flags():
    """The 'worst-case collar': bad dip + bad azimuth + bad total_depth
    + missing elevation. Should emit ALL 4 flags."""
    evaluate, _ = _import_evaluator()
    flags = evaluate(_row(
        dip=45,                 # invalid range
        azimuth=-10,            # invalid range
        total_depth=-1,         # invalid range
        elevation=None,         # missing (WARNING)
    ))
    flag_types = {f.flag_type for f in flags}
    assert "collar.invalid_dip_range" in flag_types
    assert "collar.invalid_azimuth_range" in flag_types
    assert "collar.invalid_total_depth" in flag_types
    assert "collar.missing_elevation" in flag_types


def test_legacy_collar_with_only_total_depth_emits_4_flags():
    """Realistic 2022-era ingest scenario: only total_depth populated."""
    evaluate, _ = _import_evaluator()
    flags = evaluate(_row(
        elevation=None, azimuth=None, dip=None, total_depth=120.0,
    ))
    flag_types = {f.flag_type for f in flags}
    # Should have: missing_elevation (WARN) + missing_azimuth (INFO)
    # + missing_dip (INFO). Total depth is fine.
    assert "collar.missing_elevation" in flag_types
    assert "collar.missing_azimuth" in flag_types
    assert "collar.missing_dip" in flag_types
    assert "collar.invalid_total_depth" not in flag_types


# ---------------------------------------------------------------------------
# Workspace + project tenancy
# ---------------------------------------------------------------------------


def test_flags_carry_workspace_and_project_from_row():
    """Every flag must inherit the collar row's tenancy so RLS picks
    it up at SME-review time."""
    evaluate, _ = _import_evaluator()
    flags = evaluate(_row(
        workspace_id="ws-abc", project_id="proj-xyz", elevation=None,
    ))
    assert all(f.workspace_id == "ws-abc" for f in flags)
    assert all(f.project_id == "proj-xyz" for f in flags)


def test_flags_carry_rule_version():
    """rule_version is part of the idempotency key — pin it explicitly
    so a bump retires old flags rather than collides with them."""
    evaluate, version = _import_evaluator()
    flags = evaluate(_row(elevation=None))
    assert flags[0].rule_version == version
    assert version == "v1.0"
