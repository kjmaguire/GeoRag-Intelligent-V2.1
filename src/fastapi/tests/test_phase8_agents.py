"""§8 Target Recommendation agent batch (Phase H4 graduations)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.agents.phase8.backtesting import backtesting
from app.agents.phase8.candidate_generation import candidate_generation
from app.agents.phase8.constraint import constraint
from app.agents.phase8.deposit_model import deposit_model
from app.agents.phase8.evidence_layer import evidence_layer
from app.agents.phase8.field_outcome import field_outcome
from app.agents.phase8.geologist_signoff import geologist_signoff
from app.agents.phase8.recommendation_explainer import recommendation_explainer
from app.agents.phase8.scenario_planning import scenario_planning
from app.agents.phase8.target_scoring import target_scoring
from app.agents.phase8.uncertainty import uncertainty


def _inner(agent):
    return getattr(agent, "__wrapped__", agent)


def _run(agent, **kwargs):
    return asyncio.run(_inner(agent)(ctx=None, **kwargs))


# ──────────────────────── deposit_model ─────────────────────────


def test_deposit_model_uranium_returns_athabasca() -> None:
    result = _run(deposit_model, workspace_id="ws", commodity_primary="uranium")
    assert result["selected_slug"] == "athabasca_uranium"
    assert "proximity_to_unconformity" in result["factor_weights"]
    assert "McArthur River" in result["analogues"]


def test_deposit_model_unknown_commodity_falls_back() -> None:
    result = _run(deposit_model, workspace_id="ws", commodity_primary="vanadium")
    assert result["selected_slug"] == "generic_baseline"


def test_deposit_model_override_slug() -> None:
    result = _run(
        deposit_model, workspace_id="ws", commodity_primary="gold",
        target_model_slug="some_custom_slug",
    )
    assert result["selected_slug"] == "some_custom_slug"


# ──────────────────────── evidence_layer ────────────────────────


def test_evidence_layer_emits_per_factor_layers() -> None:
    result = _run(
        evidence_layer, workspace_id="ws", project_id="p",
        target_model_id="athabasca_uranium",
        aoi_geom_wkt="POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
    )
    assert len(result["layers"]) > 0
    for layer in result["layers"]:
        assert layer["factor_name"]
        assert layer["source_kinds"]


# ──────────────────────── candidate_generation ──────────────────


def test_candidate_generation_emits_zones() -> None:
    layers = {"layers": [
        {"factor_name": "proximity_to_unconformity"},
        {"factor_name": "structural_intersect_density"},
    ]}
    result = _run(
        candidate_generation, workspace_id="ws", project_id="p",
        run_id="r", evidence_layers=layers,
    )
    assert len(result["candidate_zones"]) >= 3
    for z in result["candidate_zones"]:
        assert "POLYGON" in z["geom_wkt"]


# ──────────────────────── target_scoring ────────────────────────


def test_target_scoring_weighted_baseline() -> None:
    result = _run(
        target_scoring, workspace_id="ws", run_id="r",
        target_model_version_id="v1", zone_ids=["z1", "z2"],
        scoring_kind="weighted",
    )
    assert result["scoring_kind"] == "weighted"
    assert len(result["scores"]) == 2
    assert result["notice"] is None


def test_target_scoring_xgboost_falls_back_with_notice() -> None:
    result = _run(
        target_scoring, workspace_id="ws", run_id="r",
        target_model_version_id="v1", zone_ids=["z1"],
        scoring_kind="xgboost",
    )
    assert result["scoring_kind"] == "weighted"
    assert "xgboost" in result["notice"]


# ──────────────────────── uncertainty ───────────────────────────


def test_uncertainty_heuristic_baseline() -> None:
    result = _run(
        uncertainty, workspace_id="ws", score_ids=["s1", "s2"],
        method="heuristic",
    )
    assert len(result["uncertainties"]) == 2
    assert result["notice"] is None


def test_uncertainty_bayesian_falls_back() -> None:
    result = _run(
        uncertainty, workspace_id="ws", score_ids=["s1"],
        method="bayesian",
    )
    assert result["method"] == "heuristic"
    assert "bayesian" in result["notice"]


# ──────────────────────── constraint ────────────────────────────


def test_constraint_retains_all_zones_in_stub_mode() -> None:
    result = _run(
        constraint, workspace_id="ws", project_id="p",
        zone_ids=["z1", "z2", "z3"],
    )
    assert len(result["retained_zone_ids"]) == 3
    assert len(result["excluded_zone_ids"]) == 0
    # At least one enabled rule
    enabled = [r for r in result["rules_applied"] if r["enabled"]]
    assert len(enabled) >= 1


# ──────────────────────── recommendation_explainer ──────────────


def test_recommendation_explainer_renders_markdown_with_factors() -> None:
    factors = [
        {"factor_name": "proximity_to_unconformity", "contribution": 0.4,
         "evidence_chunk_ids": ["e1", "e2"]},
        {"factor_name": "structural_intersect_density", "contribution": 0.1,
         "evidence_chunk_ids": ["e3"]},
    ]
    result = _run(
        recommendation_explainer, workspace_id="ws", zone_id="z1",
        score_id="s1", rank=1, factor_breakdown=factors,
    )
    assert "Rank #1" in result["rationale_markdown"]
    assert "proximity_to_unconformity" in result["rationale_markdown"]
    assert result["top_factor"] == "proximity_to_unconformity"


def test_recommendation_explainer_no_factors_short_circuits() -> None:
    result = _run(
        recommendation_explainer, workspace_id="ws", zone_id="z",
        score_id="s", rank=1, factor_breakdown=[],
    )
    assert result["top_factor"] is None


# ──────────────────────── geologist_signoff ─────────────────────


def test_geologist_signoff_signed_off_requires_credential_verified() -> None:
    with pytest.raises(ValueError):
        _run(
            geologist_signoff, workspace_id="ws", target_id="t1",
            qp_user_id=1, qp_credential_id="cred",
            decision="signed_off", rationale="ok",
            qp_signature_method="wet_signature",
            credential_verified=False,
        )


def test_geologist_signoff_signed_off_with_verification_succeeds() -> None:
    result = _run(
        geologist_signoff, workspace_id="ws", target_id="t1",
        qp_user_id=1, qp_credential_id="cred",
        decision="signed_off", rationale="ok",
        qp_signature_method="wet_signature",
        credential_verified=True,
    )
    assert result["decision"] == "signed_off"
    assert result["credential_verified_at"] is not None
    assert result["target_recommendations_hash"]


def test_geologist_signoff_rejected_doesnt_require_verification() -> None:
    result = _run(
        geologist_signoff, workspace_id="ws", target_id="t1",
        qp_user_id=1, qp_credential_id="cred",
        decision="rejected", rationale="bad target",
        qp_signature_method="manual",
    )
    assert result["decision"] == "rejected"


# ──────────────────────── field_outcome ─────────────────────────


def test_field_outcome_emits_envelope() -> None:
    result = _run(
        field_outcome, workspace_id="ws", recommendation_id="r1",
        drillhole_collar_id="c1", hit_or_miss="hit",
        outcome_payload={"grade": 0.5},
    )
    assert result["hit_or_miss"] == "hit"
    assert result["recorded_at"]
    assert result["audit_action_type"] == "target.outcome.hit"


# ──────────────────────── backtesting ───────────────────────────


def test_backtesting_computes_hit_rate() -> None:
    outcomes = [
        {"target_id": "t1", "hit_or_miss": "hit"},
        {"target_id": "t2", "hit_or_miss": "miss"},
        {"target_id": "t3", "hit_or_miss": "hit"},
        {"target_id": "t4", "hit_or_miss": "hit"},
    ]
    result = _run(
        backtesting, workspace_id="ws",
        target_model_version_id="v1",
        window_start=datetime(2026, 1, 1, tzinfo=UTC),
        window_end=datetime(2026, 5, 1, tzinfo=UTC),
        outcomes=outcomes,
    )
    metrics = result["metrics_payload"]
    assert metrics["total_outcomes"] == 4
    assert metrics["hits"] == 3
    assert metrics["hit_rate"] == 0.75


def test_backtesting_precision_at_k() -> None:
    outcomes = [
        {"target_id": "t1", "hit_or_miss": "hit"},
        {"target_id": "t2", "hit_or_miss": "miss"},
    ]
    ranked = [
        {"target_id": "t1", "rank": 1},
        {"target_id": "t2", "rank": 2},
    ]
    result = _run(
        backtesting, workspace_id="ws",
        target_model_version_id="v1",
        window_start=datetime(2026, 1, 1, tzinfo=UTC),
        window_end=datetime(2026, 5, 1, tzinfo=UTC),
        outcomes=outcomes, ranked_recommendations=ranked,
    )
    metrics = result["metrics_payload"]
    assert metrics["precision_at_5"] == 0.5  # 1 hit in top-2 (k=5 ≥ list len)


# ──────────────────────── scenario_planning ─────────────────────


def test_scenario_planning_diffs_baseline_vs_scenario() -> None:
    baseline = [
        {"zone_id": "z1", "rank": 1, "aggregate_score": 0.8},
        {"zone_id": "z2", "rank": 2, "aggregate_score": 0.5},
    ]
    scenario = [
        {"zone_id": "z1", "rank": 2, "aggregate_score": 0.6},
        {"zone_id": "z2", "rank": 1, "aggregate_score": 0.7},
        {"zone_id": "z3", "rank": 3, "aggregate_score": 0.4},
    ]
    result = _run(
        scenario_planning, workspace_id="ws", project_id="p",
        baseline_run_id="r1", scenario_payload={"add_outcome": "hit"},
        baseline_ranked=baseline, scenario_ranked=scenario,
    )
    assert len(result["rank_deltas"]) == 2
    assert "z3" in result["gained_zone_ids"]
    assert result["lost_zone_ids"] == []
