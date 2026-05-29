"""Phase G.1 — unit tests for the SHAP-equivalent zone scorer.

Pure-function tests over `score_candidate_zone` plus a DB smoke that
confirms the 10 deposit-model templates are seeded and reachable via
the canonical `factor_weights` JSONB path.
"""
from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.services.targeting.score_factors import (
    ScoreFactor,
    ScoredZone,
    score_candidate_zone,
)


_MODEL_VERSION_FAKE = UUID("00000000-0000-0000-0000-000000000001")


def test_score_factor_dataclass_fields() -> None:
    """ScoreFactor records all 6 fields the persistence layer expects."""
    f = ScoreFactor(
        factor_name="graphite_conductor_present",
        factor_value=0.8,
        factor_weight=0.25,
        contribution=0.2,
        evidence_chunk_ids=["chunk-1", "chunk-2"],
        rationale="strong signal",
    )
    assert f.factor_name == "graphite_conductor_present"
    assert f.factor_value == 0.8
    assert f.factor_weight == 0.25
    assert f.contribution == 0.2
    assert f.evidence_chunk_ids == ["chunk-1", "chunk-2"]


def test_score_empty_evidence_returns_zero() -> None:
    """Zone with no observed signals → aggregate=0, all contributions=0."""
    weights = {"graphite_conductor_present": 0.25, "clay_alteration_detected": 0.18}
    scored = score_candidate_zone(
        zone_evidence={},
        factor_weights=weights,
        model_version_id=_MODEL_VERSION_FAKE,
    )
    assert scored.aggregate_score == 0.0
    assert len(scored.factors) == 2
    assert all(f.factor_value == 0.0 for f in scored.factors)
    assert all(f.contribution == 0.0 for f in scored.factors)


def test_score_all_signals_at_one_returns_normalised_one() -> None:
    """Every factor at strength=1 → aggregate normalises to +1."""
    weights = {"a": 0.5, "b": 0.3, "c": 0.2}  # positive weights, sum=1.0
    scored = score_candidate_zone(
        zone_evidence={"a": 1.0, "b": 1.0, "c": 1.0},
        factor_weights=weights,
        model_version_id=_MODEL_VERSION_FAKE,
    )
    assert scored.aggregate_score == pytest.approx(1.0)
    assert scored.factors[0].contribution == pytest.approx(0.5)


def test_score_negative_weights_act_as_penalties() -> None:
    """Negative weight × observed signal → aggregate drops below 0."""
    weights = {"shear_zone_proximity": 0.4, "no_structural_corridor": -0.6}
    scored = score_candidate_zone(
        zone_evidence={"shear_zone_proximity": 0.5, "no_structural_corridor": 1.0},
        factor_weights=weights,
        model_version_id=_MODEL_VERSION_FAKE,
    )
    # contribution_sum = 0.5×0.4 + 1.0×(−0.6) = 0.20 − 0.60 = −0.40
    # weight_abs_sum  = 0.4 + 0.6 = 1.0
    # aggregate = −0.40 / 1.0 = −0.40
    assert scored.aggregate_score == pytest.approx(-0.40)


def test_score_value_clamped_to_unit_interval() -> None:
    """Out-of-range observed strength gets clamped to [0, 1]."""
    weights = {"a": 0.5}
    scored = score_candidate_zone(
        zone_evidence={"a": 99.0},
        factor_weights=weights,
        model_version_id=_MODEL_VERSION_FAKE,
    )
    assert scored.factors[0].factor_value == 1.0


def test_score_missing_factor_treated_as_zero() -> None:
    """Factor in weights but missing from evidence → value=0, contribution=0."""
    weights = {"a": 0.3, "b": 0.3, "c": 0.4}
    scored = score_candidate_zone(
        zone_evidence={"a": 1.0},  # b, c unobserved
        factor_weights=weights,
        model_version_id=_MODEL_VERSION_FAKE,
    )
    by_name = {f.factor_name: f for f in scored.factors}
    assert by_name["b"].factor_value == 0.0
    assert by_name["c"].factor_value == 0.0


def test_score_empty_weights_returns_zero_aggregate() -> None:
    """`custom` template path — empty weights → aggregate=0, no crash."""
    scored = score_candidate_zone(
        zone_evidence={},
        factor_weights={},
        model_version_id=_MODEL_VERSION_FAKE,
    )
    assert scored.aggregate_score == 0.0
    assert scored.factors == []


def test_rationale_strings_label_signal_strength() -> None:
    """Rationale strings carry both signal strength + boost/penalty cue."""
    weights = {"a": 0.5, "b": -0.5}
    scored = score_candidate_zone(
        zone_evidence={"a": 0.9, "b": 0.05},
        factor_weights=weights,
        model_version_id=_MODEL_VERSION_FAKE,
    )
    by_name = {f.factor_name: f for f in scored.factors}
    assert "strong signal" in by_name["a"].rationale
    assert "boost" in by_name["a"].rationale
    assert "no signal" in by_name["b"].rationale
    assert "penalty" in by_name["b"].rationale


# ─────────────────────────── DB smoke test ────────────────────────────


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@pytest.mark.asyncio
async def test_10_deposit_model_templates_seeded() -> None:
    """All 10 deposit-model templates exist with an active v1 row each."""
    if not os.environ.get("POSTGRES_PASSWORD"):
        pytest.skip("POSTGRES_PASSWORD not set; skipping DB smoke")
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        n_models = await conn.fetchval(
            "SELECT count(*) FROM targeting.target_models"
        )
        n_versions = await conn.fetchval(
            "SELECT count(*) FROM targeting.target_model_versions WHERE is_active"
        )
        # The 10 from this seed; existing custom workspace variants may add more.
        assert n_models >= 10, f"expected ≥10 models, got {n_models}"
        assert n_versions >= 10, f"expected ≥10 active versions, got {n_versions}"

        # Spot-check the demo target — roll_front_uranium — matches Cameco data
        row = await conn.fetchrow(
            "SELECT display_name, commodity_primary, factor_weights "
            "FROM targeting.target_models m "
            "JOIN targeting.target_model_versions v "
            "  ON v.target_model_id = m.target_model_id AND v.is_active "
            "WHERE m.slug = 'roll_front_uranium'"
        )
        assert row is not None
        assert row["commodity_primary"] == "uranium"
        weights = row["factor_weights"]
        if isinstance(weights, str):
            import json
            weights = json.loads(weights)
        # Positive indicators present:
        assert weights["reduced_pyrite_zone_in_log"] > 0
        assert weights["redox_interface_within_section"] > 0
        # Negative indicators carry through with negative sign:
        assert weights["fully_oxidized_section"] < 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_roll_front_demo_zone_scores_in_range() -> None:
    """End-to-end: pull the roll_front_uranium model from the DB, score a
    fake Cameco-shaped zone, assert the aggregate sits in the expected
    range and the factor breakdown is consistent.
    """
    if not os.environ.get("POSTGRES_PASSWORD"):
        pytest.skip("POSTGRES_PASSWORD not set; skipping DB smoke")
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        row = await conn.fetchrow(
            "SELECT v.version_id, v.factor_weights "
            "FROM targeting.target_models m "
            "JOIN targeting.target_model_versions v "
            "  ON v.target_model_id = m.target_model_id AND v.is_active "
            "WHERE m.slug = 'roll_front_uranium'"
        )
    finally:
        await conn.close()
    assert row is not None
    weights = row["factor_weights"]
    if isinstance(weights, str):
        import json
        weights = json.loads(weights)

    # Synthetic Cameco-shaped zone: classic roll-front with strong reduction
    # signal, weak / absent oxidised section.
    evidence = {
        "reduced_pyrite_zone_in_log": 0.85,
        "redox_interface_within_section": 0.90,
        "U_gamma_log_anomaly": 0.75,
        "organic_matter_present": 0.40,
        "permeable_sandstone_host": 0.80,
        "fully_oxidized_section": 0.10,
        "impermeable_clay_dominant": 0.05,
    }
    scored = score_candidate_zone(
        zone_evidence=evidence,
        factor_weights={k: float(v) for k, v in weights.items()},
        model_version_id=UUID(str(row["version_id"])),
    )
    # A strongly-positive roll-front signal should score well above 0.4.
    assert scored.aggregate_score > 0.4, scored.aggregate_score
    # Penalty factors did get evaluated:
    by_name = {f.factor_name: f for f in scored.factors}
    assert by_name["fully_oxidized_section"].contribution < 0
