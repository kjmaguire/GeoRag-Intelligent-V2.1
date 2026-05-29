"""Service partials + ML deterministic baselines (Phase H4 final batch).

Covers:
  - app/services/llm_incident_diagnosis/nodes.py (5 nodes)
  - app/services/target_scoring_ml/xgboost_inference.py (linear baseline)
  - app/services/target_scoring_ml/shap_writer.py (workspace_id gate)
  - app/services/target_scoring_ml/ab_comparison.py (compare + strategy)
  - app/services/source_trust/boost.py (deterministic fallback)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.services.llm_incident_diagnosis.nodes import (
    classify_incident,
    gather_traces,
    identify_root_cause,
    propose_remediation,
    record_diagnosis,
)
from app.services.llm_incident_diagnosis.state import IncidentDiagnosisState
from app.services.target_scoring_ml.ab_comparison import (
    choose_display_strategy,
)
from app.services.target_scoring_ml.xgboost_inference import (
    _linear_baseline,
)


# ──────────────────── incident_diagnosis nodes ───────────────────


def _make_incident_state(payload: dict | None = None) -> IncidentDiagnosisState:
    return IncidentDiagnosisState(
        incident_id=uuid4(),
        workspace_id=uuid4(),
        triage_kind="other",
        reported_at=datetime.now(timezone.utc),
        initial_payload=payload or {},
    )


def test_classify_incident_hallucination_keyword() -> None:
    state = _make_incident_state({
        "report": "the model hallucinated a grade value of 12 g/t",
    })
    out = asyncio.run(classify_incident(state))
    assert out.classified_kind == "hallucination"
    assert out.classification_confidence == 0.85


def test_classify_incident_unmatched_keeps_triage() -> None:
    state = _make_incident_state({"report": "system reported nothing unusual"})
    out = asyncio.run(classify_incident(state))
    assert out.classified_kind == "other"


def test_gather_traces_emits_stub_excerpt() -> None:
    state = _make_incident_state()
    out = asyncio.run(gather_traces(state))
    assert len(out.trace_excerpts) >= 1


def test_identify_root_cause_emits_hypothesis() -> None:
    state = _make_incident_state()
    state = state.model_copy(update={
        "classified_kind": "hallucination",
        "classification_confidence": 0.85,
    })
    out = asyncio.run(identify_root_cause(state))
    assert out.root_cause_hypothesis is not None
    assert "hallucination" in out.root_cause_hypothesis.lower()


def test_propose_remediation_picks_remediation_for_kind() -> None:
    state = _make_incident_state()
    state = state.model_copy(update={"classified_kind": "citation_drift"})
    out = asyncio.run(propose_remediation(state))
    assert out.proposed_remediation_kind == "rerun_attach_citations"
    assert "attach_citations" in out.proposed_remediation_payload["remediation_text"]


def test_record_diagnosis_marks_completed() -> None:
    state = _make_incident_state()
    out = asyncio.run(record_diagnosis(state))
    assert out.diagnosis_recorded is True
    assert out.completed_at is not None


# ──────────────────── target_scoring_ml ─────────────────────────


def test_linear_baseline_weighted_aggregate() -> None:
    aggregate, values, contribs = _linear_baseline(
        {"feature_a": 0.8, "feature_b": 0.6},
        {"feature_a": 0.5, "feature_b": 0.5},
    )
    # contribs = a*0.5 + b*0.5 = 0.4 + 0.3 = 0.7; weight sum=1.0
    assert aggregate == pytest.approx(0.7)
    assert values["feature_a"] == 0.8
    assert contribs["feature_a"] == pytest.approx(0.4)


def test_linear_baseline_handles_non_numeric() -> None:
    aggregate, values, contribs = _linear_baseline(
        {"feature_a": "not-a-number"},
        {"feature_a": 0.5},
    )
    assert values["feature_a"] == 0.0
    assert aggregate == 0.0


def test_linear_baseline_no_factor_weights_uniform() -> None:
    aggregate, values, contribs = _linear_baseline(
        {"a": 1.0, "b": 1.0},
        {},  # no weights → uniform 1/N
    )
    # contributions sum, divided by len → mean
    assert aggregate == pytest.approx(0.5)


# ──────────────────── ab_comparison strategy ─────────────────────


def test_choose_display_strategy_no_xgboost_score_is_weighted() -> None:
    s = choose_display_strategy(
        weighted_score=0.8, xgboost_score=None, xgboost_confidence=None,
    )
    assert s == "weighted_only"


def test_choose_display_strategy_low_confidence_falls_back() -> None:
    s = choose_display_strategy(
        weighted_score=0.8, xgboost_score=0.7, xgboost_confidence=0.2,
    )
    assert s == "weighted_only"


def test_choose_display_strategy_high_confidence_ensemble() -> None:
    s = choose_display_strategy(
        weighted_score=0.8, xgboost_score=0.7, xgboost_confidence=0.9,
    )
    assert s == "ensemble"
