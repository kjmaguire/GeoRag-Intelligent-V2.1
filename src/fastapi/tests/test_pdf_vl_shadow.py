"""ADR-0015 Phase 3 — Qwen3-VL shadow-eval gate tests.

Pure synchronous logic — no DB, no model, no serving. Verifies the three
gate metrics (schema-valid rate, figure→caption link rate vs V2 baseline,
per-page latency p95) and the promote/block decision.
"""
from __future__ import annotations

import pytest

from app.services.eval.pdf_vl_shadow import (
    LINK_RATE_REGRESSION_TOLERANCE_PP,
    MIN_OBSERVATIONS_FOR_GATE,
    SCHEMA_VALID_RATE_MIN,
    VlShadowObservation,
    _percentile,
    assess_vl_shadow,
    grounded_claim_count,
)


def _obs(
    i: int = 0,
    *,
    v2_valid: bool = True,
    v3_valid: bool = True,
    v2_grounded: int = 2,
    v3_grounded: int = 2,
    page_count: int = 2,
    v2_ms: float | None = 200.0,
    v3_ms: float | None = 400.0,
) -> VlShadowObservation:
    return VlShadowObservation(
        pdf_id=f"pdf-{i}",
        section_ref_hash=f"hash-{i}",
        page_count=page_count,
        v2_schema_valid=v2_valid,
        v3_schema_valid=v3_valid,
        v2_grounded_claims=v2_grounded if v2_valid else 0,
        v3_grounded_claims=v3_grounded if v3_valid else 0,
        v2_latency_ms=v2_ms,
        v3_latency_ms=v3_ms,
    )


def _n_good(n: int) -> list[VlShadowObservation]:
    return [_obs(i) for i in range(n)]


# ---------------------------------------------------------------------------
# grounded_claim_count
# ---------------------------------------------------------------------------

class _SummaryLike:
    def __init__(self, claims: list) -> None:
        self.claims = claims


def test_grounded_claim_count_none() -> None:
    assert grounded_claim_count(None) == 0


def test_grounded_claim_count_object() -> None:
    assert grounded_claim_count(_SummaryLike([{"a": 1}, {"b": 2}])) == 2
    assert grounded_claim_count(_SummaryLike([])) == 0


def test_grounded_claim_count_dict() -> None:
    assert grounded_claim_count({"claims": [1, 2, 3]}) == 3
    assert grounded_claim_count({"claims": []}) == 0
    assert grounded_claim_count({}) == 0


# ---------------------------------------------------------------------------
# VlShadowObservation.from_summaries
# ---------------------------------------------------------------------------

def test_from_summaries_marks_none_as_invalid() -> None:
    obs = VlShadowObservation.from_summaries(
        pdf_id="p", section_ref_hash="h", page_count=3,
        v2_summary={"claims": [1, 2]},
        v3_summary=None,                 # V3 failed schema validation
        v2_latency_ms=120.0, v3_latency_ms=None,
    )
    assert obs.v2_schema_valid is True
    assert obs.v3_schema_valid is False
    assert obs.v2_grounded_claims == 2
    assert obs.v3_grounded_claims == 0
    assert obs.v2_has_grounded_output is True
    assert obs.v3_has_grounded_output is False


def test_has_grounded_output_requires_both_valid_and_claims() -> None:
    # schema-valid but zero claims → not a grounded output.
    obs = _obs(v3_valid=True, v3_grounded=0)
    assert obs.v3_schema_valid is True
    assert obs.v3_has_grounded_output is False


# ---------------------------------------------------------------------------
# assess_vl_shadow — gate decisions
# ---------------------------------------------------------------------------

def test_gate_allows_when_all_metrics_pass() -> None:
    result = assess_vl_shadow(_n_good(MIN_OBSERVATIONS_FOR_GATE))
    assert result.allow is True
    assert result.blocking_reasons == []
    assert result.n == MIN_OBSERVATIONS_FOR_GATE
    assert result.v3_schema_valid_rate == 1.0
    assert result.v3_link_rate == 1.0
    assert result.link_rate_delta_pp == 0.0
    # per-page p95 = 400ms / 2 pages.
    assert result.v3_latency_p95_per_page_ms == 200.0


def test_gate_blocks_on_low_schema_valid_rate() -> None:
    obs = _n_good(MIN_OBSERVATIONS_FOR_GATE)
    # Make 2/20 V3 outputs schema-invalid → 0.90 < 0.95.
    obs[0] = _obs(0, v3_valid=False)
    obs[1] = _obs(1, v3_valid=False)
    result = assess_vl_shadow(obs)
    assert result.allow is False
    assert "schema_valid_rate_below_min" in result.blocking_reasons
    assert result.v3_schema_valid_rate == pytest.approx(0.90)


def test_gate_blocks_on_figure_link_regression_without_schema_block() -> None:
    obs = _n_good(MIN_OBSERVATIONS_FOR_GATE)
    # 5/20 V3 outputs are schema-valid but ungrounded (zero claims):
    # schema-valid rate stays 1.0, but link rate drops 1.0 → 0.75.
    for k in range(5):
        obs[k] = _obs(k, v3_valid=True, v3_grounded=0)
    result = assess_vl_shadow(obs)
    assert result.allow is False
    assert "figure_link_rate_regression" in result.blocking_reasons
    assert "schema_valid_rate_below_min" not in result.blocking_reasons
    assert result.v3_schema_valid_rate == 1.0
    assert result.v3_link_rate == pytest.approx(0.75)
    assert result.link_rate_delta_pp == pytest.approx(-25.0)


def test_small_link_regression_within_tolerance_passes() -> None:
    # One ungrounded V3 section out of a large sample → ~ -N pp; size the
    # sample so the drop sits inside LINK_RATE_REGRESSION_TOLERANCE_PP.
    count = 100
    obs = _n_good(count)
    obs[0] = _obs(0, v3_valid=True, v3_grounded=0)  # -1.0 pp, within 2.0 pp
    result = assess_vl_shadow(obs)
    assert abs(result.link_rate_delta_pp) <= LINK_RATE_REGRESSION_TOLERANCE_PP
    assert result.allow is True


def test_gate_blocks_on_insufficient_observations() -> None:
    result = assess_vl_shadow(_n_good(MIN_OBSERVATIONS_FOR_GATE - 1))
    assert result.allow is False
    assert result.blocking_reasons == ["insufficient_observations"]


def test_gate_blocks_on_empty() -> None:
    result = assess_vl_shadow([])
    assert result.allow is False
    assert result.blocking_reasons == ["insufficient_observations"]
    assert result.v3_latency_p95_per_page_ms is None


def test_latency_p95_divides_by_page_count_and_skips_missing() -> None:
    obs = [
        _obs(0, page_count=4, v3_ms=800.0),   # 200/page
        _obs(1, page_count=2, v3_ms=400.0),   # 200/page
        _obs(2, page_count=1, v3_ms=None),    # skipped (no timing)
    ]
    result = assess_vl_shadow(obs)
    # Only two timed observations, both 200/page.
    assert result.v3_latency_p95_per_page_ms == 200.0


def test_to_dict_surfaces_metrics_and_thresholds() -> None:
    d = assess_vl_shadow(_n_good(MIN_OBSERVATIONS_FOR_GATE)).to_dict()
    assert d["allow"] is True
    assert d["thresholds"]["schema_valid_rate_min"] == SCHEMA_VALID_RATE_MIN
    assert d["v3_schema_valid_rate"] == 1.0
    assert d["blocking_reasons"] == []
    assert "v3_latency_p95_per_page_ms" in d


# ---------------------------------------------------------------------------
# _percentile
# ---------------------------------------------------------------------------

def test_percentile_basics() -> None:
    assert _percentile([], 0.95) is None
    assert _percentile([42.0], 0.95) == 42.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 0.0) == 1.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 1.0) == 4.0
    # Nearest-rank interpolation: p50 of 1..4 = midpoint between 2 and 3.
    assert _percentile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)
