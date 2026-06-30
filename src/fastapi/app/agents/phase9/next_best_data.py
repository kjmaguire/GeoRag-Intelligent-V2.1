"""Next-Best-Data Agent (§9.7 / §20.5).

When the geological reasoning layer flags evidence gaps, this agent
proposes specific data-acquisition actions from the §20.5 menu. Each
recommendation carries:

  - kind: controlled vocabulary slug (see ``NEXT_BEST_DATA_KINDS``)
  - scope: free-text target (the agent forwards the gap description)
  - cost_estimate_usd: SME-curated low–high range
  - time_estimate_days: SME-curated low–high range
  - expected_uncertainty_reduction: 0–1 heuristic
  - prerequisites: list of preconditions before the action is viable
  - rationale: why this is the best next data for the flagged gap

Phase H4 graduation — deterministic rule table over keyword match
against the evidence-gap text. The agent emits proposals; the §8
Target Recommendation Cockpit and the §10 Geologist Sign-Off ceremony
own the accept/reject decision.

The cost/time/uncertainty-reduction ranges come from `_NBD_CATALOG`
below and reflect Kyle's SME defaults as of 2026-05 (junior-mining
project context). Operators can override per workspace via
`silver.workspace_settings.nbd_overrides` once that table lands.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent

logger = logging.getLogger(__name__)


# The 14 §20.5 recommendation kinds as a controlled vocabulary.
NEXT_BEST_DATA_KINDS: tuple[str, ...] = (
    "em_survey",
    "alteration_traverse",
    "assay_resample",
    "core_relog",
    "gravity_survey",
    "outcrop_validation",
    "structure_reinterpret",
    "ocr_improvement",
    "hyperspectral_survey",
    "biogeochem_samples",
    "satellite_imagery",
    "geophysics_ground_truth",
    "age_dating_samples",
    "adjacent_claim_filings",
)


# SME catalogue keyed by kind. Tuple shape:
#   (cost_low, cost_high, days_low, days_high, uncertainty_reduction,
#    prerequisites, keyword_triggers)
_NBD_CATALOG: dict[str, tuple[float, float, int, int, float, tuple[str, ...], tuple[str, ...]]] = {
    "em_survey":              ( 25_000, 120_000,  10,  45, 0.55, ("AOI defined", "permits"), ("conductivity", "em ", "conductive body", "sulphide")),
    "alteration_traverse":    (  3_000,  15_000,   3,  10, 0.30, ("field crew",), ("alteration", "hydrothermal", "sericite", "chlorite")),
    "assay_resample":         (  5_000,  25_000,   5,  20, 0.40, ("core retained",), ("assay", "low confidence", "outlier", "qaqc")),
    "core_relog":             (  4_000,  18_000,   4,  14, 0.50, ("core retained", "qualified logger"), ("relog", "lithology mismatch", "inconsistent log")),
    "gravity_survey":         ( 35_000, 160_000,  14,  60, 0.50, ("AOI", "access roads"), ("gravity", "density contrast", "intrusive")),
    "outcrop_validation":     (  2_500,  12_000,   2,   7, 0.25, ("road access",), ("occurrence", "showing", "outcrop", "unverified")),
    "structure_reinterpret":  (  6_000,  20_000,   5,  15, 0.35, ("structural data",), ("structure", "fault", "vein orientation", "fold")),
    "ocr_improvement":        (    500,   3_000,   1,   5, 0.20, ("source PDFs",), ("ocr", "scan quality", "illegible", "garbled")),
    "hyperspectral_survey":   ( 45_000, 180_000,  21,  75, 0.45, ("AOI", "aircraft"), ("hyperspectral", "alteration mapping", "swir")),
    "biogeochem_samples":     (  4_000,  18_000,   7,  21, 0.30, ("permits", "field crew"), ("covered terrain", "overburden", "biogeochem", "till")),
    "satellite_imagery":      (  1_500,  12_000,   2,  10, 0.20, (), ("satellite", "imagery", "spectral", "remote sensing")),
    "geophysics_ground_truth":(  3_000,  15_000,   3,  10, 0.40, ("geophys anomaly",), ("anomaly", "ground truth", "follow-up")),
    "age_dating_samples":     (  8_000,  30_000,  30, 120, 0.35, ("rock samples", "lab queue"), ("age", "u-pb", "geochronology", "dating")),
    "adjacent_claim_filings": (  1_000,   6_000,   2,   8, 0.25, ("crown record portal",), ("adjacent", "assessment", "filings", "neighbour")),
}


def _classify_gap(gap_text: str) -> list[str]:
    """Return the NBD kinds whose keyword triggers fire on `gap_text`."""
    lowered = gap_text.lower()
    matches: list[str] = []
    for kind, (_cl, _ch, _dl, _dh, _u, _p, triggers) in _NBD_CATALOG.items():
        for kw in triggers:
            if re.search(r"\b" + re.escape(kw) + r"\b", lowered):
                matches.append(kind)
                break
    return matches


def _make_recommendation(kind: str, scope: str) -> dict[str, Any]:
    cl, ch, dl, dh, u, prereq, _kw = _NBD_CATALOG[kind]
    return {
        "kind":                            kind,
        "scope":                           scope,
        "cost_estimate_usd":               [cl, ch],
        "time_estimate_days":              [dl, dh],
        "expected_uncertainty_reduction":  u,
        "prerequisites":                   list(prereq),
        "rationale": (
            f"Gap '{scope[:120]}' triggers §20.5 action '{kind}'. "
            f"Estimated cost USD {cl:,.0f}–{ch:,.0f}, "
            f"time {dl}–{dh} days, "
            f"expected uncertainty reduction {u:.0%}."
        ),
    }


def _filter_budget(
    recs: list[dict[str, Any]],
    budget_ceiling_usd: float | None,
) -> list[dict[str, Any]]:
    """Drop recommendations whose low-cost estimate exceeds the
    operator's budget ceiling. None = no filter."""
    if budget_ceiling_usd is None:
        return recs
    return [r for r in recs if r["cost_estimate_usd"][0] <= budget_ceiling_usd]


def _rank_by_uncertainty_reduction(
    recs: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Sort recommendations by expected uncertainty reduction DESC."""
    return sorted(
        recs,
        key=lambda r: r["expected_uncertainty_reduction"],
        reverse=True,
    )


def _dedupe_keep_first(recs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedup on (kind, scope) keeping the first occurrence."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for r in recs:
        key = (r["kind"], r["scope"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


@georag_agent(
    name="Next-Best-Data Agent",
    risk_tier="R1",
    version="1.0.0",  # graduated Phase H4
)
async def next_best_data(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    project_id: UUID | str,
    evidence_gaps: list[str],
    budget_ceiling_usd: float | None = None,
) -> dict[str, Any]:
    """Recommend next-best-data actions for evidence gaps.

    Args:
        workspace_id / project_id: RLS scope (informational; agent is
            pure-function).
        evidence_gaps: free-text descriptions of the gaps. Multiple
            recommendations may fire per gap if the keywords overlap.
        budget_ceiling_usd: optional upper bound. Recommendations whose
            low-cost estimate exceeds this are filtered out.

    Returns:
        {
            "recommendations": [...],  # ranked by uncertainty reduction
            "summary":         str,
            "gaps_processed":  int,
            "kinds_proposed":  list[str],
        }
    """
    if not evidence_gaps:
        return {
            "recommendations": [],
            "summary": "no evidence gaps provided",
            "gaps_processed": 0,
            "kinds_proposed": [],
        }

    raw: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for gap in evidence_gaps:
        kinds = _classify_gap(gap)
        if not kinds:
            unmatched.append(gap)
            # Fallback: offer outcrop_validation as the lowest-cost
            # generic next step. The Recommendation Cockpit operator
            # decides if it's useful.
            raw.append(_make_recommendation("outcrop_validation", gap))
            continue
        for kind in kinds:
            raw.append(_make_recommendation(kind, gap))

    recs = _dedupe_keep_first(raw)
    recs = _filter_budget(recs, budget_ceiling_usd)
    recs = _rank_by_uncertainty_reduction(recs)

    kinds_proposed = sorted({r["kind"] for r in recs})
    summary = (
        f"gaps={len(evidence_gaps)} unmatched={len(unmatched)} "
        f"recommendations={len(recs)} kinds={','.join(kinds_proposed)}"
        + (f" budget_ceiling=${budget_ceiling_usd:,.0f}" if budget_ceiling_usd else "")
    )
    logger.info("next_best_data: %s", summary)

    return {
        "recommendations": recs,
        "summary":         summary,
        "gaps_processed":  len(evidence_gaps),
        "kinds_proposed":  kinds_proposed,
    }


__all__ = ["next_best_data", "NEXT_BEST_DATA_KINDS"]
