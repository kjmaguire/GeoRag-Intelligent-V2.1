"""§9.7 Next-Best-Data Agent tests (Phase H4)."""
from __future__ import annotations

import asyncio

from app.agents.phase9.next_best_data import (
    NEXT_BEST_DATA_KINDS,
    _classify_gap,
    next_best_data,
)


def _run(evidence_gaps: list[str], *, budget_ceiling_usd=None):
    inner = getattr(next_best_data, "__wrapped__", next_best_data)
    return asyncio.run(inner(
        ctx=None,
        workspace_id="ws-1",
        project_id="prj-1",
        evidence_gaps=evidence_gaps,
        budget_ceiling_usd=budget_ceiling_usd,
    ))


def test_empty_gaps_returns_empty_recs() -> None:
    result = _run([])
    assert result["recommendations"] == []
    assert result["gaps_processed"] == 0


def test_em_keyword_triggers_em_survey() -> None:
    kinds = _classify_gap("Conductive body suspected in the NE quadrant.")
    assert "em_survey" in kinds


def test_assay_keyword_triggers_resample() -> None:
    kinds = _classify_gap("Several assays flagged as low confidence; QAQC failures.")
    assert "assay_resample" in kinds


def test_recommendations_ranked_by_uncertainty_reduction_desc() -> None:
    result = _run([
        "Need EM survey over conductive body in NE.",
        "Several assays flagged low confidence.",
        "Outcrop showing not yet validated.",
    ])
    recs = result["recommendations"]
    uncertainties = [r["expected_uncertainty_reduction"] for r in recs]
    assert uncertainties == sorted(uncertainties, reverse=True)


def test_unmatched_gap_falls_back_to_outcrop_validation() -> None:
    """A gap with no keyword hit should still produce one recommendation
    (the cheap, low-risk outcrop validation fallback)."""
    result = _run(["Some vague concern about the project area."])
    assert len(result["recommendations"]) >= 1
    assert any(r["kind"] == "outcrop_validation"
               for r in result["recommendations"])


def test_budget_filter_removes_high_cost_recs() -> None:
    result = _run(
        ["Need EM survey over conductive body in NE quadrant."],
        budget_ceiling_usd=5_000,  # below em_survey low cost
    )
    assert not any(r["kind"] == "em_survey"
                   for r in result["recommendations"])


def test_dedupe_when_two_gaps_same_kind_and_scope() -> None:
    """Same gap text + same triggered kind → 1 recommendation, not 2."""
    result = _run([
        "Conductive body in NE — need EM.",
        "Conductive body in NE — need EM.",
    ])
    em_recs = [r for r in result["recommendations"]
               if r["kind"] == "em_survey"]
    assert len(em_recs) == 1


def test_recommendation_has_required_fields() -> None:
    result = _run(["Need EM survey over conductive body."])
    rec = result["recommendations"][0]
    for key in (
        "kind", "scope", "cost_estimate_usd", "time_estimate_days",
        "expected_uncertainty_reduction", "prerequisites", "rationale",
    ):
        assert key in rec
    assert rec["kind"] in NEXT_BEST_DATA_KINDS


def test_summary_includes_counts() -> None:
    result = _run(["Need EM survey.", "Assay QAQC failures."])
    assert "gaps=2" in result["summary"]
    assert "recommendations=" in result["summary"]


def test_kinds_proposed_is_sorted_and_unique() -> None:
    result = _run([
        "Need EM survey over conductive body.",
        "Multiple EM anomalies.",
        "Hyperspectral alteration mapping needed.",
    ])
    kinds = result["kinds_proposed"]
    assert kinds == sorted(set(kinds))
