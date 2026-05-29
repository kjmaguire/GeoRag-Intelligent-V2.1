"""Unit tests for the §6a CRS / georef-quality rule family.

`evaluate_collar_crs_row` is pure-function — one collar dict in,
list[DataQualityFlag] out — so unit-testable without spinning up
Dagster + Postgres.

The Dagster asset wrapper (silver_crs_dq) is exercised in dev via
materialization on the live DB; a full integration test would need a
fixture DB which is out of scope this session.
"""
from __future__ import annotations


def _import_evaluator():
    """Import the evaluator at first-call time so pytest collection
    doesn't fail when the dagster package isn't on sys.path (e.g. an
    accidental run from the FastAPI container where it isn't mounted).
    """
    from georag_dagster.assets.silver_crs_dq import (
        CRS_CONFIDENCE_FLOOR,
        RULE_VERSION,
        SPATIAL_UNCERTAINTY_CEILING_M,
        evaluate_collar_crs_row,
    )
    return (
        evaluate_collar_crs_row,
        RULE_VERSION,
        CRS_CONFIDENCE_FLOOR,
        SPATIAL_UNCERTAINTY_CEILING_M,
    )


def _row(**overrides) -> dict:
    """Minimal-valid collar row — detected CRS, high confidence, geom
    populated, no excess uncertainty → 0 flags."""
    defaults = dict(
        collar_id="c-1",
        workspace_id="ws-1",
        project_id="proj-1",
        hole_id="ECK-22-001",
        easting=500_000.0,
        northing=5_000_000.0,
        has_geom=True,
        georef_method="detected",
        crs_confidence=0.95,
        spatial_uncertainty_m=5.0,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Happy path — fully-populated collar produces no flags
# ---------------------------------------------------------------------------


def test_well_georeferenced_collar_emits_no_flags():
    evaluate, _, _, _ = _import_evaluator()
    assert evaluate(_row()) == []


def test_null_provenance_columns_emit_no_flags():
    """A collar with NULL provenance (legacy ingest before §6a) should
    not be flagged — we can't penalise rows where the data simply
    wasn't captured yet."""
    evaluate, _, _, _ = _import_evaluator()
    flags = evaluate(_row(
        georef_method=None,
        crs_confidence=None,
        spatial_uncertainty_m=None,
    ))
    assert flags == []


# ---------------------------------------------------------------------------
# Rule 1 — crs_assumed
# ---------------------------------------------------------------------------


def test_assumed_method_emits_warning():
    evaluate, _, _, _ = _import_evaluator()
    flags = evaluate(_row(georef_method="assumed", crs_confidence=0.7))
    assumed = [f for f in flags if f.flag_type == "collar.crs_assumed"]
    assert len(assumed) == 1
    assert assumed[0].severity == "WARNING"


def test_detected_method_does_not_emit_assumed_flag():
    evaluate, _, _, _ = _import_evaluator()
    flags = evaluate(_row(georef_method="detected"))
    assert all(f.flag_type != "collar.crs_assumed" for f in flags)


# ---------------------------------------------------------------------------
# Rule 2 — crs_low_confidence
# ---------------------------------------------------------------------------


def test_confidence_below_floor_emits_warning():
    evaluate, _, floor, _ = _import_evaluator()
    flags = evaluate(_row(crs_confidence=floor - 0.01))
    low = [f for f in flags if f.flag_type == "collar.crs_low_confidence"]
    assert len(low) == 1
    assert low[0].severity == "WARNING"
    assert low[0].threshold_payload["observed"] == floor - 0.01


def test_confidence_exactly_at_floor_does_not_fire():
    """0.5 is the threshold — strict `< 0.5` means equality passes."""
    evaluate, _, floor, _ = _import_evaluator()
    flags = evaluate(_row(crs_confidence=floor))
    assert all(f.flag_type != "collar.crs_low_confidence" for f in flags)


def test_high_confidence_does_not_fire_low_rule():
    evaluate, _, _, _ = _import_evaluator()
    flags = evaluate(_row(crs_confidence=0.9))
    assert all(f.flag_type != "collar.crs_low_confidence" for f in flags)


def test_null_confidence_does_not_fire_low_rule():
    evaluate, _, _, _ = _import_evaluator()
    flags = evaluate(_row(crs_confidence=None))
    assert all(f.flag_type != "collar.crs_low_confidence" for f in flags)


# ---------------------------------------------------------------------------
# Rule 3 — geom_missing_with_coords
# ---------------------------------------------------------------------------


def test_null_geom_with_coords_emits_error():
    evaluate, _, _, _ = _import_evaluator()
    flags = evaluate(_row(has_geom=False))
    missing = [f for f in flags if f.flag_type == "collar.geom_missing_with_coords"]
    assert len(missing) == 1
    assert missing[0].severity == "ERROR"
    assert missing[0].threshold_payload["easting"] == 500_000.0
    assert missing[0].threshold_payload["northing"] == 5_000_000.0


def test_null_geom_without_coords_does_not_fire():
    """If easting/northing are also NULL the collar simply has no
    location at all — that's a different bug and shouldn't fire
    this rule, which is specifically about FAILED conversion."""
    evaluate, _, _, _ = _import_evaluator()
    flags = evaluate(_row(has_geom=False, easting=None, northing=None))
    assert all(
        f.flag_type != "collar.geom_missing_with_coords" for f in flags
    )


def test_present_geom_does_not_fire_missing_rule():
    evaluate, _, _, _ = _import_evaluator()
    flags = evaluate(_row(has_geom=True))
    assert all(
        f.flag_type != "collar.geom_missing_with_coords" for f in flags
    )


# ---------------------------------------------------------------------------
# Rule 4 — spatial_uncertainty_excessive
# ---------------------------------------------------------------------------


def test_uncertainty_above_ceiling_emits_warning():
    evaluate, _, _, ceiling = _import_evaluator()
    flags = evaluate(_row(spatial_uncertainty_m=ceiling + 1))
    unc = [f for f in flags if f.flag_type == "collar.spatial_uncertainty_excessive"]
    assert len(unc) == 1
    assert unc[0].severity == "WARNING"
    assert unc[0].threshold_payload["observed_m"] == ceiling + 1


def test_uncertainty_exactly_at_ceiling_does_not_fire():
    evaluate, _, _, ceiling = _import_evaluator()
    flags = evaluate(_row(spatial_uncertainty_m=ceiling))
    assert all(
        f.flag_type != "collar.spatial_uncertainty_excessive" for f in flags
    )


def test_low_uncertainty_does_not_fire():
    evaluate, _, _, _ = _import_evaluator()
    flags = evaluate(_row(spatial_uncertainty_m=5.0))
    assert all(
        f.flag_type != "collar.spatial_uncertainty_excessive" for f in flags
    )


def test_null_uncertainty_does_not_fire():
    evaluate, _, _, _ = _import_evaluator()
    flags = evaluate(_row(spatial_uncertainty_m=None))
    assert all(
        f.flag_type != "collar.spatial_uncertainty_excessive" for f in flags
    )


# ---------------------------------------------------------------------------
# Multi-rule rows + tenancy + idempotency
# ---------------------------------------------------------------------------


def test_completely_broken_row_emits_all_four_flags():
    evaluate, _, _, _ = _import_evaluator()
    flags = evaluate(_row(
        georef_method="assumed",
        crs_confidence=0.2,
        has_geom=False,
        spatial_uncertainty_m=500.0,
    ))
    types = {f.flag_type for f in flags}
    assert {
        "collar.crs_assumed",
        "collar.crs_low_confidence",
        "collar.geom_missing_with_coords",
        "collar.spatial_uncertainty_excessive",
    } == types


def test_assumed_with_high_confidence_only_fires_assumed():
    """The two CRS-provenance rules are independent — a collar can
    have 'assumed' method but still high confidence (the pipeline was
    sure about its assumption). Don't double-flag in that case."""
    evaluate, _, _, _ = _import_evaluator()
    flags = evaluate(_row(georef_method="assumed", crs_confidence=0.95))
    types = {f.flag_type for f in flags}
    assert types == {"collar.crs_assumed"}


def test_flags_carry_workspace_and_project_from_row():
    evaluate, _, _, _ = _import_evaluator()
    flags = evaluate(_row(
        collar_id="c-99",
        workspace_id="ws-XYZ",
        project_id="proj-XYZ",
        georef_method="assumed",
    ))
    assert all(f.workspace_id == "ws-XYZ" for f in flags)
    assert all(f.project_id == "proj-XYZ" for f in flags)
    assert all(f.record_id == "c-99" for f in flags)
    assert all(f.record_type == "collar" for f in flags)


def test_flags_carry_rule_version():
    evaluate, rule_v, _, _ = _import_evaluator()
    flags = evaluate(_row(georef_method="assumed"))
    assert all(f.rule_version == rule_v for f in flags)


def test_idempotency_key_is_stable_across_reruns():
    """Two identical runs must produce flags with the same idempotency
    key tuple so the upsert is a no-op on re-runs."""
    evaluate, _, _, _ = _import_evaluator()
    row = _row(georef_method="assumed", crs_confidence=0.3)
    run_1 = evaluate(row)
    run_2 = evaluate(row)

    def _key(f):
        return (f.workspace_id, f.record_type, f.record_id,
                f.flag_type, f.rule_version)

    keys_1 = sorted(_key(f) for f in run_1)
    keys_2 = sorted(_key(f) for f in run_2)
    assert keys_1 == keys_2


def test_hole_id_falls_back_to_collar_prefix_when_missing():
    evaluate, _, _, _ = _import_evaluator()
    flags = evaluate(_row(
        collar_id="abcd1234-deadbeef",
        hole_id=None,
        georef_method="assumed",
    ))
    assert "abcd1234" in flags[0].description
