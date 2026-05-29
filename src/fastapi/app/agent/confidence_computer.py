"""Rule-based confidence Level computation — Phase 1 / Step 1.3.

The plan requires *evidence-weighted* confidence — not model self-rating.
The LLM emits the prose ``reason`` and ``drivers`` for the uncertainty
block, but the Level (High / Medium / Low) is computed deterministically
from retrieval signals and guard-fire state.

Two-stage computation:

  Stage 1 — initial Level (in :func:`compute_initial_level`)
      Available at response-assembly time. Uses retrieval signals only:
      number of distinct cited sources. ≥2 sources → High; 1 → Medium;
      0 → Low (refusal-adjacent — should not occur on real OIUR answers
      because the schema rejects empty observations).

  Stage 2 — guard demotion (in :func:`demote_for_guards`)
      Runs after :func:`app.agent.hallucination.orchestrator_validators.run_post_assembly_validation`
      so the L3 numeric-grounding flag and ``conflicting_evidence`` field
      are populated. Conflicting evidence forces Low; an L3 flag forces
      High → Medium (the plan's "no High when numeric grounding would flag"
      gate). Demotion is monotonic — never raises a Level.

The pair is invoked from :mod:`app.agent.response_assembler` (stage 1) and
:mod:`app.agent.orchestrator` (stage 2). Stage 2 is a no-op when
``geo_answer`` is None (flag off or parse fell back).
"""

from __future__ import annotations

import logging
from typing import Iterable

from app.agent.schemas import ConfidenceLevel, GeoAnswer, UncertaintyBlock
from app.models.rag import Citation, GeoRAGResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage 1 — retrieval-time signals
# ---------------------------------------------------------------------------


def _count_independent_sources(citations: Iterable[Citation]) -> int:
    """Count distinct citation sources.

    Independence is measured at the ``source_chunk_id`` granularity — two
    citations pointing at the same chunk count as one source even if they
    have different display ids. Refusal placeholder citations (no real
    upstream record) are excluded.
    """
    ids: set[str] = set()
    for c in citations:
        if not c.source_chunk_id:
            continue
        if c.source_chunk_id in {"no-tool-call", "georag_reports:empty", "pg_public_geoscience:empty"}:
            continue
        ids.add(c.source_chunk_id)
    return len(ids)


def compute_initial_level(
    citations: Iterable[Citation],
) -> tuple[ConfidenceLevel, str]:
    """Stage 1 — derive an initial Level from retrieval signals only.

    Returns ``(level, computation_note)``. The note is a one-line plain-text
    explanation suitable for the lineage artifact (Step 1.5) — it does NOT
    replace the LLM-authored ``ConfidenceBlock.reason`` prose, which stays
    intact.
    """
    n_sources = _count_independent_sources(citations)
    if n_sources >= 2:
        return "High", f"computed: {n_sources} independent cited sources, no guards run yet"
    if n_sources == 1:
        return "Medium", "computed: single cited source"
    return "Low", "computed: no cited upstream sources"


# ---------------------------------------------------------------------------
# Stage 2 — guard-fire demotion
# ---------------------------------------------------------------------------


def _is_layer3_warning(warning: str) -> bool:
    return warning.startswith("Layer 3:")


def demote_for_guards(
    level: ConfidenceLevel,
    *,
    numeric_flagged: bool,
    conflicts_present: bool,
) -> tuple[ConfidenceLevel, list[str]]:
    """Stage 2 — apply guard-driven demotions.

    Returns ``(new_level, reasons_for_change)``. The list is empty when no
    demotion fired.

    Rules:
      * ``conflicts_present`` forces Low (strongest signal — the plan's
        "Low: conflicting sources" tier).
      * ``numeric_flagged`` forces High → Medium (the plan's "must not
        assign High when Numeric grounding would flag" gate).
      * Demotion is monotonic. The function never raises a Level.
    """
    reasons: list[str] = []
    new_level: ConfidenceLevel = level

    if conflicts_present and new_level != "Low":
        reasons.append(
            "conflicting evidence detected in retrieved corpus — demoted to Low"
        )
        new_level = "Low"

    if numeric_flagged and new_level == "High":
        reasons.append(
            "numeric grounding guard flagged unverified claim(s) — High demoted to Medium"
        )
        new_level = "Medium"

    return new_level, reasons


def apply_level_to_geo_answer(
    answer: GeoAnswer,
    level: ConfidenceLevel,
) -> GeoAnswer:
    """Return a new ``GeoAnswer`` with the confidence Level overridden.

    Pydantic ``model_copy`` is used so the input is not mutated. The
    LLM-authored ``reason``, ``drivers``, and ``data_to_reduce_uncertainty``
    fields are preserved — only ``level`` changes.

    When uncertainty is :class:`SectionEmpty` (a partial-evidence answer
    that has no interpretations and therefore no confidence to override),
    the input is returned unchanged.
    """
    if not isinstance(answer.uncertainty, UncertaintyBlock):
        return answer
    if answer.uncertainty.confidence.level == level:
        return answer
    new_conf = answer.uncertainty.confidence.model_copy(update={"level": level})
    new_uncert = answer.uncertainty.model_copy(update={"confidence": new_conf})
    return answer.model_copy(update={"uncertainty": new_uncert})


# ---------------------------------------------------------------------------
# Integration helper — invoked from orchestrator after post-validation
# ---------------------------------------------------------------------------


def apply_guard_demotion(
    response: GeoRAGResponse,
    validation_warnings: list[str],
) -> tuple[GeoRAGResponse, list[str]]:
    """Apply Stage-2 demotion to a validated response.

    No-op when ``response.geo_answer`` is None (OIUR flag off or parse fell
    back) or when uncertainty is :class:`SectionEmpty`. Returns the
    (possibly new) response and the list of demotion reasons applied — the
    caller may persist these into the lineage artifact (Step 1.5).
    """
    if response.geo_answer is None:
        return response, []
    if not isinstance(response.geo_answer.uncertainty, UncertaintyBlock):
        return response, []

    numeric_flagged = any(_is_layer3_warning(w) for w in validation_warnings)
    conflicts_present = bool(response.conflicting_evidence)

    current_level = response.geo_answer.uncertainty.confidence.level
    new_level, reasons = demote_for_guards(
        current_level,
        numeric_flagged=numeric_flagged,
        conflicts_present=conflicts_present,
    )

    if new_level == current_level:
        return response, []

    logger.info(
        "confidence: demoted Level %s → %s (%s)",
        current_level,
        new_level,
        "; ".join(reasons),
    )

    new_answer = apply_level_to_geo_answer(response.geo_answer, new_level)
    return response.model_copy(update={"geo_answer": new_answer}), reasons


__all__ = [
    "apply_guard_demotion",
    "apply_level_to_geo_answer",
    "compute_initial_level",
    "demote_for_guards",
]
