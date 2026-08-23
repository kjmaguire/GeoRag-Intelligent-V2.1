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
from collections.abc import Iterable

from app.agent.hallucination.orchestrator_validators import (
    LAYER3_WARNING_PREFIXES,
)
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
    have different display ids. Citations whose upstream tool returned no
    rows are excluded via the predicate SHARED with the assembler's IND-6
    guard (``response_assembler.is_empty_source_id``) so the two filters
    cannot drift (audit 2026-08-14, finding 3).

    That was a frozenset of three literal strings until 2026-08-22, while
    ``_extract_source_id`` minted eleven distinct zero-row shapes — so a
    missed hole lookup (``silver.collars:miss``) or an empty assay query
    (``silver.samples:element=U3O8:count=0``) counted as an independent
    source here and as real evidence there.
    """
    # Local import — response_assembler imports this module lazily inside
    # a function, so a top-of-function import here keeps the pair cycle-free
    # regardless of module load order.
    from app.agent.response_assembler import is_empty_source_id  # noqa: PLC0415

    ids: set[str] = set()
    for c in citations:
        if not c.source_chunk_id:
            continue
        if is_empty_source_id(c.source_chunk_id):
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
    """Layer 3 covers TWO guards, and they are not prefixed alike.

    The numeric guard emits "Layer 3: Ungrounded number ..." and the
    unit-pair guard emits "Layer 3 tuple: value 5.2 reported as 'ppm' ..."
    -- a space where the other has a colon. Matching "Layer 3:" excluded
    every unit-pair warning, so an answer whose only guard hit was a unit
    mismatch kept whatever confidence it started with.

    That is the wrong one to miss. A g/t-vs-ppm-vs-% confusion is a
    thousandfold error in a grade, presented at High confidence with the
    warning that identifies it sitting unread beside the answer.

    The prefixes come from the module that emits them so this and the
    severity classifier in orchestrator_validators cannot drift apart
    again — they already had, in exactly this way.
    """
    return warning.startswith(LAYER3_WARNING_PREFIXES)


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


#: Ceiling applied to the numeric confidence when sources conflict.
#:
#: Mirrors the Level rule (conflicts force "Low"), expressed on the float
#: every consumer renders. A cap rather than a multiplier because
#: conflicting evidence is a statement about the corpus, not a discount on
#: how well-retrieved the answer was.
CONFLICT_CONFIDENCE_CAP = 0.30

#: Multiplier applied when the Layer 3 numeric/unit guards flagged a claim.
#:
#: Multiplicative rather than a floor: an ungrounded number in an otherwise
#: well-grounded answer should not end up at the same confidence as one in
#: a weakly-grounded answer. Scaling keeps the ordering retrieval
#: established instead of flattening every flagged answer into one bucket.
NUMERIC_FLAG_CONFIDENCE_FACTOR = 0.7


def apply_guard_demotion(
    response: GeoRAGResponse,
    validation_warnings: list[str],
) -> tuple[GeoRAGResponse, list[str]]:
    """Apply Stage-2 demotion to a validated response.

    Two demotions, on two fields, because they have different owners.

    ``GeoRAGResponse.confidence`` (float) is what every consumer renders
    today, so the guard signals act on it unconditionally.

    ``geo_answer.uncertainty.confidence.level`` is the OIUR
    ``ConfidenceLevel`` and only exists when ``GEO_ANSWER_OIUR_ENABLED`` is
    on, so that half stays conditional.

    Until 2026-08-22 the whole function returned immediately when
    ``geo_answer`` was None — which is always, since the flag ships False
    and is unset on fastapi-cc. One of the eight graph nodes therefore did
    nothing in production and ``demotion_reasons`` was ``[]`` on every row
    of silver.answer_runs. That mattered most in the case Stage 2 was built
    for: one or two ungrounded numbers is below NUMERIC_RETRY_THRESHOLD, so
    nothing retries and nothing floors, and demotion was the only remaining
    signal.

    Returns the (possibly new) response and the reasons applied — the
    caller persists these into the lineage artifact (Step 1.5).
    """
    numeric_flagged = any(_is_layer3_warning(w) for w in validation_warnings)
    conflicts_present = bool(response.conflicting_evidence)

    if not numeric_flagged and not conflicts_present:
        return response, []

    reasons: list[str] = []
    updates: dict[str, object] = {}

    # --- the float, always -------------------------------------------------
    confidence = response.confidence
    if conflicts_present and confidence > CONFLICT_CONFIDENCE_CAP:
        reasons.append(
            f"conflicting evidence detected in retrieved corpus — "
            f"confidence capped at {CONFLICT_CONFIDENCE_CAP:.2f}"
        )
        confidence = CONFLICT_CONFIDENCE_CAP
    if numeric_flagged:
        reasons.append(
            f"numeric grounding guard flagged unverified claim(s) — "
            f"confidence scaled by {NUMERIC_FLAG_CONFIDENCE_FACTOR:.1f}"
        )
        confidence *= NUMERIC_FLAG_CONFIDENCE_FACTOR
    if confidence != response.confidence:
        updates["confidence"] = round(confidence, 4)

    # --- the OIUR Level, only when OIUR produced one -----------------------
    if response.geo_answer is not None and isinstance(
        response.geo_answer.uncertainty, UncertaintyBlock
    ):
        current_level = response.geo_answer.uncertainty.confidence.level
        new_level, level_reasons = demote_for_guards(
            current_level,
            numeric_flagged=numeric_flagged,
            conflicts_present=conflicts_present,
        )
        if new_level != current_level:
            reasons.extend(level_reasons)
            updates["geo_answer"] = apply_level_to_geo_answer(
                response.geo_answer, new_level,
            )
            logger.info(
                "confidence: demoted Level %s → %s (%s)",
                current_level,
                new_level,
                "; ".join(level_reasons),
            )

    if not updates:
        return response, []

    if "confidence" in updates:
        logger.info(
            "confidence: %.3f → %.3f (%s)",
            response.confidence,
            updates["confidence"],
            "; ".join(reasons),
        )

    return response.model_copy(update=updates), reasons


__all__ = [
    "CONFLICT_CONFIDENCE_CAP",
    "NUMERIC_FLAG_CONFIDENCE_FACTOR",
    "apply_guard_demotion",
    "apply_level_to_geo_answer",
    "compute_initial_level",
    "demote_for_guards",
]
