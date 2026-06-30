"""Live tests for the §8.7 weighted-scoring formula + the §18.2
target_recommendation planning/scoring/ranking nodes (doc-phase 138).

Covers 6 of 12 §18.2 nodes:
  1. select_commodity_deposit_model
  2. load_workspace_playbook
  6. score_candidate_zones (REAL §8.7 math)
  7. calculate_uncertainty
  8. apply_constraints
  9. rank_targets (REAL sort)

Pure unit tests against the Pydantic state model — no DB required.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.target_recommendation.nodes import (
    _synthetic_factors_for_zone,
    apply_constraints,
    calculate_uncertainty,
    collect_private_evidence,
    collect_public_geoscience,
    create_map_layers,
    explain_score_factors,
    generate_candidate_zones,
    load_workspace_playbook,
    rank_targets,
    route_to_review_cockpit,
    score_candidate_zones,
    select_commodity_deposit_model,
    weighted_aggregate,
)
from app.services.target_recommendation.state import (
    CandidateZone,
    ScoreFactor,
    TargetRecommendationState,
    ZoneScore,
)


def _make_state(
    candidate_zones: list[CandidateZone] | None = None,
    workspace_playbook: dict | None = None,
) -> TargetRecommendationState:
    return TargetRecommendationState(
        run_id=uuid4(),
        workspace_id=uuid4(),
        project_id=uuid4(),
        requested_by_user_id=1,
        aoi_geom_wkt="POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        candidate_zones=candidate_zones or [],
        workspace_playbook=workspace_playbook or {},
    )


def _make_zone() -> CandidateZone:
    return CandidateZone(
        zone_id=uuid4(),
        geom_wkt="POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
    )


# ----------------------------------------------------------------------
# §8.7 weighted_aggregate formula unit tests
# ----------------------------------------------------------------------
def test_weighted_aggregate_empty_returns_zero():
    assert weighted_aggregate([]) == 0.0


def test_weighted_aggregate_zero_weights_returns_zero():
    factors = [
        ScoreFactor(factor_name="x", factor_value=0.5, factor_weight=0.0, contribution=0.0)
    ]
    assert weighted_aggregate(factors) == 0.0


def test_weighted_aggregate_single_factor():
    factors = [
        ScoreFactor(factor_name="x", factor_value=0.8, factor_weight=1.0, contribution=0.8)
    ]
    assert abs(weighted_aggregate(factors) - 0.8) < 1e-9


def test_weighted_aggregate_two_factors():
    factors = [
        ScoreFactor(factor_name="a", factor_value=0.6, factor_weight=0.5, contribution=0.30),
        ScoreFactor(factor_name="b", factor_value=0.8, factor_weight=0.5, contribution=0.40),
    ]
    # (0.6*0.5 + 0.8*0.5) / 1.0 = 0.7
    assert abs(weighted_aggregate(factors) - 0.7) < 1e-9


def test_weighted_aggregate_clamps_to_range():
    """If factor_values land outside [0,1] for any reason, result
    still clamps to [0,1]."""
    factors = [
        ScoreFactor(factor_name="x", factor_value=2.0, factor_weight=1.0, contribution=2.0)
    ]
    assert weighted_aggregate(factors) == 1.0
    factors2 = [
        ScoreFactor(factor_name="x", factor_value=-1.0, factor_weight=1.0, contribution=-1.0)
    ]
    assert weighted_aggregate(factors2) == 0.0


# ----------------------------------------------------------------------
# Synthetic factor generator
# ----------------------------------------------------------------------
def test_synthetic_factors_produce_three_factors():
    z = _make_zone()
    factors = _synthetic_factors_for_zone(z, "athabasca_uranium")
    assert len(factors) == 3
    names = [f.factor_name for f in factors]
    assert "proximity_to_known_occurrence" in names
    assert "alteration_signature_match" in names
    assert "structural_intersect_density" in names
    # Weights sum to 1.0.
    total_weight = sum(f.factor_weight for f in factors)
    assert abs(total_weight - 1.0) < 1e-9


def test_synthetic_factors_deterministic_for_same_zone_id():
    z = _make_zone()
    a = _synthetic_factors_for_zone(z, "athabasca_uranium")
    b = _synthetic_factors_for_zone(z, "athabasca_uranium")
    assert [f.factor_value for f in a] == [f.factor_value for f in b]


# ----------------------------------------------------------------------
# select_commodity_deposit_model
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_select_commodity_deposit_model_defaults_to_athabasca():
    state = _make_state()
    out = await select_commodity_deposit_model(state)
    assert out.target_model_id is not None
    assert out.target_model_version_id is not None
    assert out.workspace_playbook["selected_deposit_model_slug"] == "athabasca_uranium"


@pytest.mark.asyncio
async def test_select_commodity_deposit_model_matches_commodity_hint():
    state = _make_state(workspace_playbook={"target_commodity": "Au"})
    out = await select_commodity_deposit_model(state)
    slug = out.workspace_playbook["selected_deposit_model_slug"]
    assert "gold" in slug


# ----------------------------------------------------------------------
# load_workspace_playbook
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_load_workspace_playbook_idempotent_passthrough():
    state = _make_state(workspace_playbook={"target_commodity": "U"})
    out = await load_workspace_playbook(state)
    assert out.workspace_playbook == state.workspace_playbook


# ----------------------------------------------------------------------
# score_candidate_zones (REAL §8.7 math)
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_score_candidate_zones_assigns_score_per_zone():
    zones = [_make_zone() for _ in range(3)]
    state = _make_state(candidate_zones=zones)
    state = await select_commodity_deposit_model(state)
    out = await score_candidate_zones(state)

    assert len(out.scores) == 3
    for s in out.scores:
        assert 0.0 <= s.aggregate_score <= 1.0
        assert len(s.factors) == 3
        # Contributions populated.
        for f in s.factors:
            assert abs(f.contribution - (f.factor_value * f.factor_weight)) < 1e-9


@pytest.mark.asyncio
async def test_score_candidate_zones_is_idempotent():
    zones = [_make_zone() for _ in range(2)]
    state = _make_state(candidate_zones=zones)
    state = await select_commodity_deposit_model(state)
    once = await score_candidate_zones(state)
    twice = await score_candidate_zones(once)
    assert len(twice.scores) == len(once.scores) == 2
    assert {s.zone_id for s in once.scores} == {s.zone_id for s in twice.scores}


@pytest.mark.asyncio
async def test_score_candidate_zones_no_zones_no_scores():
    state = _make_state()
    out = await score_candidate_zones(state)
    assert out.scores == []


# ----------------------------------------------------------------------
# calculate_uncertainty
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_calculate_uncertainty_populates_per_zone():
    zones = [_make_zone() for _ in range(3)]
    state = _make_state(candidate_zones=zones)
    state = await select_commodity_deposit_model(state)
    state = await score_candidate_zones(state)
    out = await calculate_uncertainty(state)

    assert len(out.uncertainties) == 3
    for u in out.uncertainties:
        assert u.method == "heuristic"
        assert 0.0 <= u.uncertainty_value <= 1.0
    # Aggregate_uncertainty back-filled on each ZoneScore.
    for s in out.scores:
        assert s.aggregate_uncertainty is not None


# ----------------------------------------------------------------------
# apply_constraints + rank_targets
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rank_targets_orders_by_aggregate_score_desc():
    """Manually seed scores in mixed order; rank_targets should
    produce a ranking in descending aggregate_score."""
    state = _make_state()
    # Manually seed scores (skip the synthetic generator).
    state = state.model_copy(update={"scores": [
        ZoneScore(zone_id=uuid4(), aggregate_score=0.3),
        ZoneScore(zone_id=uuid4(), aggregate_score=0.9),
        ZoneScore(zone_id=uuid4(), aggregate_score=0.6),
    ]})
    state = await apply_constraints(state)
    out = await rank_targets(state)

    assert len(out.ranked_targets) == 3
    assert out.ranked_targets[0].rank == 1
    assert out.ranked_targets[0].aggregate_score == 0.9
    assert out.ranked_targets[1].aggregate_score == 0.6
    assert out.ranked_targets[2].aggregate_score == 0.3


@pytest.mark.asyncio
async def test_rank_targets_filters_excluded_zones():
    state = _make_state()
    excluded_id = uuid4()
    included_id = uuid4()
    state = state.model_copy(update={
        "scores": [
            ZoneScore(zone_id=excluded_id, aggregate_score=0.9),
            ZoneScore(zone_id=included_id, aggregate_score=0.5),
        ],
        "excluded_zone_ids": [excluded_id],
    })
    out = await rank_targets(state)
    assert len(out.ranked_targets) == 1
    assert out.ranked_targets[0].zone_id == included_id
    assert out.ranked_targets[0].rank == 1


# ----------------------------------------------------------------------
# Full pipeline integration
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_full_scoring_pipeline_chain():
    """Chain select → load → score → uncertainty → apply_constraints → rank
    against 5 candidate zones. Verify final ranked_targets are sorted
    and explanation_markdown carries the doc-phase 138 tag."""
    zones = [_make_zone() for _ in range(5)]
    state = _make_state(candidate_zones=zones)
    state = await select_commodity_deposit_model(state)
    state = await load_workspace_playbook(state)
    state = await score_candidate_zones(state)
    state = await calculate_uncertainty(state)
    state = await apply_constraints(state)
    state = await rank_targets(state)

    assert len(state.ranked_targets) == 5
    # Sorted DESC.
    scores = [t.aggregate_score for t in state.ranked_targets]
    assert scores == sorted(scores, reverse=True)
    # Doc-phase tag in explanation.
    for t in state.ranked_targets:
        assert "doc-phase 138" in t.explanation_markdown


@pytest.mark.asyncio
async def test_explain_score_factors_renders_markdown_table() -> None:
    """Phase H4 graduation — deterministic template emits per-target
    rationale with factor table + top/bottom contributors."""
    zones = [_make_zone() for _ in range(3)]
    state = _make_state(candidate_zones=zones)
    state = await select_commodity_deposit_model(state)
    state = await load_workspace_playbook(state)
    state = await score_candidate_zones(state)
    state = await calculate_uncertainty(state)
    state = await apply_constraints(state)
    state = await rank_targets(state)
    state = await explain_score_factors(state)

    assert len(state.ranked_targets) == 3
    for t in state.ranked_targets:
        md = t.explanation_markdown
        assert f"Rank #{t.rank}" in md
        # Markdown table header
        assert "| Factor | Value | Weight | Contribution |" in md
        assert "Top contributing factor" in md


@pytest.mark.asyncio
async def test_explain_score_factors_no_targets_short_circuit() -> None:
    state = _make_state()
    out = await explain_score_factors(state)
    assert out.ranked_targets == []


# ──────────────────── Phase H4: 5 newly-graduated nodes ───────────────


@pytest.mark.asyncio
async def test_collect_private_evidence_emits_kinds() -> None:
    state = _make_state()
    out = await collect_private_evidence(state)
    assert out.private_evidence["status"] == "deterministic_stub"
    assert "collars" in out.private_evidence["kinds_planned"]
    assert out.private_evidence["workspace_id"] == str(state.workspace_id)


@pytest.mark.asyncio
async def test_collect_public_geoscience_emits_jurisdictions() -> None:
    state = _make_state()
    out = await collect_public_geoscience(state)
    assert out.public_evidence["status"] == "deterministic_stub"
    assert "CA-SK" in out.public_evidence["jurisdiction_hints"]


@pytest.mark.asyncio
async def test_generate_candidate_zones_synthesises_when_empty() -> None:
    state = _make_state()
    out = await generate_candidate_zones(state)
    assert len(out.candidate_zones) == 5
    for z in out.candidate_zones:
        assert "POLYGON" in z.geom_wkt


@pytest.mark.asyncio
async def test_generate_candidate_zones_passthrough_when_prepopulated() -> None:
    existing = [_make_zone() for _ in range(3)]
    state = _make_state(candidate_zones=existing)
    out = await generate_candidate_zones(state)
    assert len(out.candidate_zones) == 3  # unchanged


@pytest.mark.asyncio
async def test_create_map_layers_records_manifest() -> None:
    zones = [_make_zone() for _ in range(2)]
    state = _make_state(candidate_zones=zones)
    state = await select_commodity_deposit_model(state)
    state = await load_workspace_playbook(state)
    state = await score_candidate_zones(state)
    state = await calculate_uncertainty(state)
    state = await apply_constraints(state)
    state = await rank_targets(state)
    state = await create_map_layers(state)
    # target_heatmap + ranked_target_zones + per-zone overlays
    assert "target_heatmap" in state.map_layer_uris
    assert "ranked_target_zones" in state.map_layer_uris
    # 2 zone-specific overlays
    zone_keys = [k for k in state.map_layer_uris if k.startswith("zone/")]
    assert len(zone_keys) == 2


@pytest.mark.asyncio
async def test_route_to_review_cockpit_sets_url_when_targets_ranked() -> None:
    zones = [_make_zone() for _ in range(2)]
    state = _make_state(candidate_zones=zones)
    state = await select_commodity_deposit_model(state)
    state = await load_workspace_playbook(state)
    state = await score_candidate_zones(state)
    state = await calculate_uncertainty(state)
    state = await apply_constraints(state)
    state = await rank_targets(state)
    state = await route_to_review_cockpit(state)
    assert state.sent_to_review_cockpit is True
    assert state.review_cockpit_url is not None
    assert str(state.run_id) in state.review_cockpit_url


@pytest.mark.asyncio
async def test_route_to_review_cockpit_no_targets_marks_unrouted() -> None:
    state = _make_state()
    out = await route_to_review_cockpit(state)
    assert out.sent_to_review_cockpit is False
