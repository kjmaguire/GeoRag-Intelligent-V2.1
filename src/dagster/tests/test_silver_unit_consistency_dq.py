"""Unit tests for the §6a unit consistency rule family.

`evaluate_unit_buckets` is pure-function — list[bucket-dict] in,
list[DataQualityFlag] out — so unit-testable without spinning up
Dagster + Postgres.

The Dagster asset wrapper (silver_unit_consistency_dq) is exercised
in dev via materialization on the live DB + a poisoned-row probe;
a full integration test would need a fixture DB which is out of
scope this session.
"""
from __future__ import annotations


def _import_evaluator():
    """Lazy import so pytest collection doesn't fail when the dagster
    package isn't on sys.path."""
    from georag_dagster.assets.silver_unit_consistency_dq import (
        RULE_VERSION,
        _normalize_unit,
        evaluate_unit_buckets,
    )
    return evaluate_unit_buckets, _normalize_unit, RULE_VERSION


def _bucket(**overrides) -> dict:
    """Build one (collar, element, unit) bucket row."""
    defaults = dict(
        collar_id="c-1",
        workspace_id="ws-1",
        project_id="proj-1",
        hole_id="ECK-22-001",
        element="Au",
        unit="ppm",
        sample_count=30,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Unit normalizer
# ---------------------------------------------------------------------------


def test_normalize_unit_handles_case_and_whitespace():
    _, normalize, _ = _import_evaluator()
    assert normalize("PPM") == "ppm"
    assert normalize("  ppm  ") == "ppm"
    assert normalize("Ppm") == "ppm"


def test_normalize_unit_folds_percent_synonyms():
    _, normalize, _ = _import_evaluator()
    assert normalize("%") == "pct"
    assert normalize("percent") == "pct"
    assert normalize("PCT") == "pct"


def test_normalize_unit_folds_g_per_tonne_synonyms():
    _, normalize, _ = _import_evaluator()
    assert normalize("g/t") == "g/t"
    assert normalize("g/tonne") == "g/t"
    assert normalize("gpt") == "g/t"


def test_normalize_unit_folds_oz_per_tonne_synonyms():
    _, normalize, _ = _import_evaluator()
    assert normalize("oz/t") == "oz/t"
    assert normalize("ozpt") == "oz/t"
    assert normalize("oz/ton") == "oz/t"


def test_normalize_unit_null_and_empty_return_none():
    _, normalize, _ = _import_evaluator()
    assert normalize(None) is None
    assert normalize("") is None
    assert normalize("   ") is None


def test_normalize_unit_unknown_passes_through_lowercased():
    """An exotic unit not in the synonym map shouldn't error — just
    return its lowercased form so equality checks still work."""
    _, normalize, _ = _import_evaluator()
    assert normalize("WeIrDoUnIt") == "weirdounit"


# ---------------------------------------------------------------------------
# Happy path — single unit per (collar, element) → no flags
# ---------------------------------------------------------------------------


def test_empty_input_emits_no_flags():
    evaluate, _, _ = _import_evaluator()
    assert evaluate([]) == []


def test_consistent_units_emit_no_flags():
    """One unit per (collar, element) across all samples → clean."""
    evaluate, _, _ = _import_evaluator()
    rows = [
        _bucket(element="Au", unit="g/t", sample_count=30),
        _bucket(element="Cu", unit="ppm", sample_count=30),
        _bucket(element="SiO2", unit="pct", sample_count=30),
    ]
    assert evaluate(rows) == []


def test_synonyms_do_not_trigger_rule():
    """'ppm' and 'PPM' and ' ppm ' on the same (collar, element) all
    normalize to 'ppm' → not a mixed-units violation."""
    evaluate, _, _ = _import_evaluator()
    rows = [
        _bucket(element="Au", unit="ppm", sample_count=10),
        _bucket(element="Au", unit="PPM", sample_count=10),
        _bucket(element="Au", unit="  ppm  ", sample_count=10),
    ]
    assert evaluate(rows) == []


def test_percent_synonyms_do_not_trigger_rule():
    evaluate, _, _ = _import_evaluator()
    rows = [
        _bucket(element="Cu", unit="%", sample_count=10),
        _bucket(element="Cu", unit="pct", sample_count=10),
        _bucket(element="Cu", unit="percent", sample_count=10),
    ]
    assert evaluate(rows) == []


# ---------------------------------------------------------------------------
# Rule fires
# ---------------------------------------------------------------------------


def test_mixed_units_on_one_element_emits_one_error_flag():
    evaluate, _, _ = _import_evaluator()
    rows = [
        _bucket(element="Au", unit="ppm", sample_count=15),
        _bucket(element="Au", unit="pct", sample_count=3),
    ]
    flags = evaluate(rows)

    assert len(flags) == 1
    f = flags[0]
    assert f.flag_type == "assay.mixed_units"
    assert f.severity == "ERROR"
    assert f.record_type == "collar"
    assert f.record_id == "c-1"
    # Description names the element + counts.
    assert "Au" in f.description
    assert "ppm" in f.description
    assert "pct" in f.description
    # Payload exposes the per-element rollup.
    assert f.threshold_payload["affected_element_count"] == 1
    assert f.threshold_payload["elements"] == ["Au"]
    per_el = f.threshold_payload["per_element"]
    assert per_el[0]["element"] == "Au"
    assert sorted(per_el[0]["normalized_units"]) == ["pct", "ppm"]
    assert per_el[0]["unit_counts"] == {"ppm": 15, "pct": 3}
    assert per_el[0]["total_samples"] == 18


def test_g_per_tonne_vs_ppm_does_trigger_rule():
    """g/t and ppm are conceptually equivalent (1 g/t = 1 ppm) but
    mixing them within one collar+element is a reporting heterogeneity
    we DO want to surface — the spec deliberately doesn't fold them."""
    evaluate, _, _ = _import_evaluator()
    rows = [
        _bucket(element="Au", unit="ppm", sample_count=5),
        _bucket(element="Au", unit="g/t", sample_count=5),
    ]
    flags = evaluate(rows)
    assert len(flags) == 1
    assert flags[0].flag_type == "assay.mixed_units"


def test_multiple_elements_mixed_on_one_collar_fan_in_to_one_flag():
    """Fan-IN — collar with Au mixed + Cu mixed → ONE summary flag,
    not two. The badge UI shouldn't render two 'mixed_units' rows
    on one collar; the per-element breakdown lives in the payload."""
    evaluate, _, _ = _import_evaluator()
    rows = [
        _bucket(element="Au", unit="ppm", sample_count=10),
        _bucket(element="Au", unit="g/t", sample_count=2),
        _bucket(element="Cu", unit="ppm", sample_count=10),
        _bucket(element="Cu", unit="pct", sample_count=2),
    ]
    flags = evaluate(rows)
    assert len(flags) == 1
    f = flags[0]
    assert f.threshold_payload["affected_element_count"] == 2
    assert f.threshold_payload["elements"] == ["Au", "Cu"]
    # Per-element entries cover both.
    per_el_names = [e["element"] for e in f.threshold_payload["per_element"]]
    assert sorted(per_el_names) == ["Au", "Cu"]


def test_multiple_collars_each_get_their_own_flag():
    evaluate, _, _ = _import_evaluator()
    rows = [
        _bucket(collar_id="c-1", element="Au", unit="ppm", sample_count=5),
        _bucket(collar_id="c-1", element="Au", unit="g/t", sample_count=5),
        _bucket(collar_id="c-2", element="Cu", unit="ppm", sample_count=5),
        _bucket(collar_id="c-2", element="Cu", unit="pct", sample_count=5),
    ]
    flags = evaluate(rows)
    assert len(flags) == 2
    ids = sorted(f.record_id for f in flags)
    assert ids == ["c-1", "c-2"]


def test_only_one_element_mixed_other_elements_clean():
    """Au has mixed units, Cu is single-unit → flag fires for the
    collar but the threshold_payload should only mention Au."""
    evaluate, _, _ = _import_evaluator()
    rows = [
        _bucket(element="Au", unit="ppm", sample_count=5),
        _bucket(element="Au", unit="g/t", sample_count=5),
        _bucket(element="Cu", unit="ppm", sample_count=10),
        _bucket(element="SiO2", unit="pct", sample_count=10),
    ]
    flags = evaluate(rows)
    assert len(flags) == 1
    assert flags[0].threshold_payload["elements"] == ["Au"]


def test_one_collar_one_element_one_unit_does_not_fire():
    """Sanity: a single bucket with one unit and one element → no flag."""
    evaluate, _, _ = _import_evaluator()
    assert evaluate([_bucket(element="Au", unit="ppm", sample_count=30)]) == []


def test_null_unit_in_one_bucket_does_not_inflate_distinct_count():
    """A NULL unit bucket normalizes to None which is excluded from
    the distinct-unit set, so it shouldn't push a single-unit collar
    over the threshold."""
    evaluate, _, _ = _import_evaluator()
    rows = [
        _bucket(element="Au", unit="ppm", sample_count=10),
        _bucket(element="Au", unit=None, sample_count=2),
    ]
    assert evaluate(rows) == []


# ---------------------------------------------------------------------------
# Tenancy + idempotency
# ---------------------------------------------------------------------------


def test_flag_carries_workspace_and_project_from_buckets():
    evaluate, _, rule_v = _import_evaluator()
    rows = [
        _bucket(workspace_id="ws-XYZ", project_id="proj-XYZ",
                element="Au", unit="ppm", sample_count=5),
        _bucket(workspace_id="ws-XYZ", project_id="proj-XYZ",
                element="Au", unit="g/t", sample_count=5),
    ]
    flags = evaluate(rows)
    assert flags[0].workspace_id == "ws-XYZ"
    assert flags[0].project_id == "proj-XYZ"
    assert flags[0].rule_version == rule_v


def test_idempotency_key_is_stable_across_reruns():
    evaluate, _, _ = _import_evaluator()
    rows = [
        _bucket(element="Au", unit="ppm", sample_count=5),
        _bucket(element="Au", unit="g/t", sample_count=5),
    ]
    run_1 = evaluate(rows)
    run_2 = evaluate(rows)

    def _key(f):
        return (f.workspace_id, f.record_type, f.record_id,
                f.flag_type, f.rule_version)

    assert _key(run_1[0]) == _key(run_2[0])


def test_hole_id_falls_back_to_collar_prefix_when_missing():
    evaluate, _, _ = _import_evaluator()
    rows = [
        _bucket(collar_id="abcd1234-deadbeef", hole_id=None,
                element="Au", unit="ppm", sample_count=5),
        _bucket(collar_id="abcd1234-deadbeef", hole_id=None,
                element="Au", unit="g/t", sample_count=5),
    ]
    flags = evaluate(rows)
    assert "abcd1234" in flags[0].description
