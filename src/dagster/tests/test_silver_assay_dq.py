"""Unit tests for the §6a assay validation rule family.

`evaluate_assay_rows_for_collar` is pure-function — a collar's list of
assay row dicts in, list[DataQualityFlag] out — so unit-testable
without spinning up Dagster + Postgres.

The Dagster asset wrapper (silver_assay_dq) is exercised in dev via
materialization on the live DB; a full integration test would need a
fixture DB which is out of scope this session.
"""
from __future__ import annotations


def _import_evaluator():
    """Import the evaluator at first-call time so pytest collection
    doesn't fail when the dagster package isn't on sys.path (e.g. an
    accidental run from the FastAPI container where it isn't mounted).
    """
    from georag_dagster.assets.silver_assay_dq import (
        ELEMENT_CEILING_PPM,
        RULE_VERSION,
        evaluate_assay_rows_for_collar,
    )
    return evaluate_assay_rows_for_collar, RULE_VERSION, ELEMENT_CEILING_PPM


_BASE_COLLAR = dict(
    collar_id="c-1",
    workspace_id="ws-1",
    project_id="proj-1",
    hole_id="ECK-22-001",
)


def _assay_row(**overrides) -> dict:
    """Minimal-valid assay row — all QA/QC passes, value below ceiling."""
    defaults = dict(
        assay_id="a-1",
        sample_id="S-1",
        element="Au",
        value=1.5,
        value_ppm=1.5,
        unit="ppm",
        qaqc_flag="pass",
        crm_pass=True,
        blank_pass=True,
        duplicate_pass=True,
        over_detection=False,
        under_detection=False,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Happy path — all rows clean → no flags
# ---------------------------------------------------------------------------


def test_clean_assay_batch_emits_no_flags():
    evaluate, _, _ = _import_evaluator()
    flags = evaluate(**_BASE_COLLAR, rows=[_assay_row() for _ in range(5)])
    assert flags == []


def test_empty_assay_batch_emits_no_flags():
    evaluate, _, _ = _import_evaluator()
    flags = evaluate(**_BASE_COLLAR, rows=[])
    assert flags == []


# ---------------------------------------------------------------------------
# QAQC flag rule
# ---------------------------------------------------------------------------


def test_qaqc_fail_emits_error():
    evaluate, _, _ = _import_evaluator()
    flags = evaluate(
        **_BASE_COLLAR,
        rows=[
            _assay_row(qaqc_flag="fail"),
            _assay_row(qaqc_flag="warn", sample_id="S-2", element="Cu"),
        ],
    )
    qaqc = [f for f in flags if f.flag_type == "assay.qaqc_flag_failed"]
    assert len(qaqc) == 1
    f = qaqc[0]
    assert f.severity == "ERROR"
    assert f.record_type == "collar"
    assert f.record_id == "c-1"
    assert f.threshold_payload["fail_count"] == 2
    assert set(f.threshold_payload["elements"]) == {"Au", "Cu"}


def test_qaqc_null_or_pass_does_not_fire():
    """NULL qaqc_flag is treated as legacy 'pass' — don't flood older holes."""
    evaluate, _, _ = _import_evaluator()
    flags = evaluate(
        **_BASE_COLLAR,
        rows=[
            _assay_row(qaqc_flag=None),
            _assay_row(qaqc_flag="pass"),
        ],
    )
    assert all(f.flag_type != "assay.qaqc_flag_failed" for f in flags)


def test_qaqc_vendor_synonyms_do_not_fire():
    """Vendor pipelines write 'ok' (Cameco), 'good', 'valid' — all
    semantically equivalent to 'pass'. Case-insensitive."""
    evaluate, _, _ = _import_evaluator()
    flags = evaluate(
        **_BASE_COLLAR,
        rows=[
            _assay_row(qaqc_flag="ok"),
            _assay_row(qaqc_flag="OK", sample_id="S-2"),
            _assay_row(qaqc_flag="Good", sample_id="S-3"),
            _assay_row(qaqc_flag="  valid  ", sample_id="S-4"),
        ],
    )
    assert all(f.flag_type != "assay.qaqc_flag_failed" for f in flags)


# ---------------------------------------------------------------------------
# CRM / blank / duplicate rules — same fan-in shape
# ---------------------------------------------------------------------------


def test_crm_fail_emits_error():
    evaluate, _, _ = _import_evaluator()
    flags = evaluate(
        **_BASE_COLLAR,
        rows=[_assay_row(crm_pass=False), _assay_row(crm_pass=False, sample_id="S-2")],
    )
    crm = [f for f in flags if f.flag_type == "assay.crm_failed"]
    assert len(crm) == 1
    assert crm[0].severity == "ERROR"
    assert crm[0].threshold_payload["fail_count"] == 2


def test_blank_fail_emits_error():
    evaluate, _, _ = _import_evaluator()
    flags = evaluate(
        **_BASE_COLLAR,
        rows=[_assay_row(blank_pass=False)],
    )
    blank = [f for f in flags if f.flag_type == "assay.blank_failed"]
    assert len(blank) == 1
    assert blank[0].severity == "ERROR"


def test_duplicate_fail_emits_error():
    evaluate, _, _ = _import_evaluator()
    flags = evaluate(
        **_BASE_COLLAR,
        rows=[_assay_row(duplicate_pass=False)],
    )
    dup = [f for f in flags if f.flag_type == "assay.duplicate_failed"]
    assert len(dup) == 1
    assert dup[0].severity == "ERROR"


def test_crm_null_does_not_fire():
    """NULL crm_pass means CRM wasn't run on this batch — that's not a
    failure, just absence of QA data."""
    evaluate, _, _ = _import_evaluator()
    flags = evaluate(
        **_BASE_COLLAR,
        rows=[_assay_row(crm_pass=None, blank_pass=None, duplicate_pass=None)],
    )
    assert flags == []


# ---------------------------------------------------------------------------
# Implausibly-high rule
# ---------------------------------------------------------------------------


def test_value_above_ceiling_emits_warning():
    evaluate, _, ceilings = _import_evaluator()
    # Au ceiling is 100_000 ppm — 200_000 should flag.
    flags = evaluate(
        **_BASE_COLLAR,
        rows=[_assay_row(element="Au", value_ppm=200_000)],
    )
    high = [f for f in flags if f.flag_type == "assay.value_implausibly_high"]
    assert len(high) == 1
    f = high[0]
    assert f.severity == "WARNING"
    assert f.threshold_payload["max_value_ppm"] == 200_000.0
    assert f.threshold_payload["max_element"] == "Au"


def test_value_below_ceiling_does_not_fire():
    evaluate, _, _ = _import_evaluator()
    flags = evaluate(
        **_BASE_COLLAR,
        rows=[
            _assay_row(element="Au", value_ppm=50_000),  # below 100_000
            _assay_row(element="Cu", value_ppm=400_000, sample_id="S-2"),  # below 500_000
        ],
    )
    assert all(f.flag_type != "assay.value_implausibly_high" for f in flags)


def test_unknown_element_does_not_fire_high_rule():
    """No ceiling defined for an exotic element → skip the rule rather
    than emit a noisy false-positive."""
    evaluate, _, _ = _import_evaluator()
    flags = evaluate(
        **_BASE_COLLAR,
        rows=[_assay_row(element="Xx", value_ppm=999_999_999)],
    )
    assert all(f.flag_type != "assay.value_implausibly_high" for f in flags)


def test_element_match_is_case_insensitive():
    evaluate, _, _ = _import_evaluator()
    # 'au' lowercase should still resolve to AU ceiling.
    flags = evaluate(
        **_BASE_COLLAR,
        rows=[_assay_row(element="au", value_ppm=200_000)],
    )
    assert any(f.flag_type == "assay.value_implausibly_high" for f in flags)


def test_high_rule_handles_null_value_ppm():
    evaluate, _, _ = _import_evaluator()
    flags = evaluate(
        **_BASE_COLLAR,
        rows=[_assay_row(element="Au", value_ppm=None)],
    )
    assert all(f.flag_type != "assay.value_implausibly_high" for f in flags)


# ---------------------------------------------------------------------------
# Detection paradox rule
# ---------------------------------------------------------------------------


def test_detection_paradox_emits_info():
    evaluate, _, _ = _import_evaluator()
    flags = evaluate(
        **_BASE_COLLAR,
        rows=[_assay_row(over_detection=True, under_detection=True)],
    )
    paradox = [f for f in flags if f.flag_type == "assay.detection_paradox"]
    assert len(paradox) == 1
    assert paradox[0].severity == "INFO"


def test_normal_detection_does_not_fire_paradox():
    evaluate, _, _ = _import_evaluator()
    flags = evaluate(
        **_BASE_COLLAR,
        rows=[
            _assay_row(over_detection=True, under_detection=False),
            _assay_row(over_detection=False, under_detection=True, sample_id="S-2"),
        ],
    )
    assert all(f.flag_type != "assay.detection_paradox" for f in flags)


# ---------------------------------------------------------------------------
# Fan-IN semantics + idempotency
# ---------------------------------------------------------------------------


def test_one_flag_per_rule_regardless_of_failed_row_count():
    """200 CRM-fails on one collar → still ONE crm_failed flag (fan-in)."""
    evaluate, _, _ = _import_evaluator()
    rows = [_assay_row(crm_pass=False, sample_id=f"S-{i}") for i in range(200)]
    flags = evaluate(**_BASE_COLLAR, rows=rows)
    crm = [f for f in flags if f.flag_type == "assay.crm_failed"]
    assert len(crm) == 1
    assert crm[0].threshold_payload["fail_count"] == 200
    # threshold_payload caps the samples list to 50 to keep JSONB tame.
    assert len(crm[0].threshold_payload["samples"]) == 50


def test_multi_rule_row_emits_one_flag_per_rule_type():
    """A single nightmare row that violates 4 rules → 4 distinct flags."""
    evaluate, _, _ = _import_evaluator()
    bad_row = _assay_row(
        qaqc_flag="fail",
        crm_pass=False,
        blank_pass=False,
        element="Au",
        value_ppm=999_999,  # above 100k ceiling
    )
    flags = evaluate(**_BASE_COLLAR, rows=[bad_row])
    types = {f.flag_type for f in flags}
    assert {
        "assay.qaqc_flag_failed",
        "assay.crm_failed",
        "assay.blank_failed",
        "assay.value_implausibly_high",
    } <= types


def test_flags_carry_workspace_and_project():
    evaluate, rule_v, _ = _import_evaluator()
    flags = evaluate(
        collar_id="c-99",
        workspace_id="ws-XYZ",
        project_id="proj-XYZ",
        hole_id="WX-22-001",
        rows=[_assay_row(crm_pass=False)],
    )
    assert all(f.workspace_id == "ws-XYZ" for f in flags)
    assert all(f.project_id == "proj-XYZ" for f in flags)
    assert all(f.record_id == "c-99" for f in flags)
    assert all(f.rule_version == rule_v for f in flags)


def test_idempotency_key_is_stable_across_reruns():
    """Two identical runs must produce flags with the same idempotency
    key tuple so the upsert is a no-op on re-runs."""
    evaluate, _, _ = _import_evaluator()
    rows = [_assay_row(crm_pass=False)]
    run_1 = evaluate(**_BASE_COLLAR, rows=rows)
    run_2 = evaluate(**_BASE_COLLAR, rows=rows)

    def _key(f):
        return (f.workspace_id, f.record_type, f.record_id,
                f.flag_type, f.rule_version)

    assert _key(run_1[0]) == _key(run_2[0])


def test_hole_id_falls_back_to_collar_prefix_when_missing():
    evaluate, _, _ = _import_evaluator()
    flags = evaluate(
        collar_id="abcd1234-deadbeef",
        workspace_id="ws-1",
        project_id="proj-1",
        hole_id=None,
        rows=[_assay_row(crm_pass=False)],
    )
    # Falls back to collar_id[:8] in the description.
    assert "abcd1234" in flags[0].description
