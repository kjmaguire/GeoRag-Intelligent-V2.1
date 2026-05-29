"""Context preparation pipeline — composes §3b + §3c + §3f.

The three foundation passes shipped earlier today operate on the same
:class:`EvidencePacket` shape but the composition logic is itself
non-trivial: the order matters, the per-intent quota table needs a
single owner, and the wire site (``assemble_node`` eventually) wants
a single function to call.

This module is that composition. Public API:

  ``prepare_evidence_for_intent(packet, intent, *, max_context_tokens)
    → PreparedContext``

The pipeline:

  1. **Annotate** — refresh ``DocumentEvidence.authority_rank`` from
     ``document_type`` (§3b). The packet may have been built with the
     default rank 3 by the converter — this step re-classifies.
  2. **Rank** — sort by authority + currency + confidence (§3b).
  3. **Diversify** — apply ``QUOTA_BY_INTENT[intent]`` via
     :func:`apply_source_diversity` (§3c). Each intent has a curated
     quota table — factual_lookup keeps mostly documents, anomaly_
     detection keeps mostly assays + tables, etc.
  4. **Enforce budget** — :func:`enforce_token_budget` drops
     lowest-authority members until ``remaining_budget ≥ 0``, or
     surfaces a budget-pinned reason (§3f).

The function is pure: no I/O, no DB, no LLM. Returns a
:class:`PreparedContext` with the prepared packet + audit fields
(what got dropped, whether the budget was reachable, the active quota
table used). Caller logs the audit fields to ``silver.query_traces``.

Wiring (downstream session): ``assemble_node`` calls this between
"build context_block" and "build system_prompt", swapping
``packet.evidence`` for the prepared list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping

from app.agent.authority import (
    annotate_evidence_packet_with_authority,
    rank_evidence_by_authority,
)
from app.agent.context_budget import (
    BudgetTrimResult,
    enforce_token_budget,
)
from app.agent.evidence import EvidencePacket
from app.agent.source_diversity import (
    DEFAULT_KIND_PRIORITY,
    apply_source_diversity,
    compute_kind_distribution,
)


logger = logging.getLogger(__name__)


__all__ = [
    "QUOTA_BY_INTENT",
    "PROTECTED_KINDS_BY_INTENT",
    "PreparedContext",
    "prepare_evidence_for_intent",
]


# ---------------------------------------------------------------------------
# Per-intent quota table
# ---------------------------------------------------------------------------
#
# Each intent gets a curated quota over the six evidence kinds. The
# tables are deliberately conservative (low numbers) — the assembler's
# downstream budget pass can trim further, but the diversity pass
# should aim to PRODUCE a packet that fits without aggressive trimming.
#
# Numbers come from plan §2b's retrieval-profile spec + the Phase 1.3
# answer-mode policy. Tune via benchmark; ratios matter more than
# absolute counts.


_QUOTA_FACTUAL_LOOKUP: dict[str, int] = {
    # Citation-heavy answer with one or two precise numbers. Mostly
    # document chunks; a single spatial/assay backup row for the
    # "Direct answer" pin.
    "document": 5,
    "spatial": 1,
    "assay": 1,
    "table": 1,
    "collar": 1,
    "graph": 0,
}

_QUOTA_SYNTHESIS: dict[str, int] = {
    # Cross-cutting answer that integrates documents, structured data,
    # and graph hops. Balanced quotas.
    "document": 3,
    "spatial": 2,
    "assay": 2,
    "table": 1,
    "collar": 1,
    "graph": 1,
}

_QUOTA_HYPOTHESIS_GENERATION: dict[str, int] = {
    # Hypothesis pass wants competing evidence — heavier on graph
    # (relationship paths) + assay (numerical anchors). Fewer documents
    # to leave room for the adversarial pass that comes with this intent.
    "document": 2,
    "spatial": 1,
    "assay": 3,
    "table": 1,
    "collar": 1,
    "graph": 3,
}

_QUOTA_ANOMALY_DETECTION: dict[str, int] = {
    # Numbers-first. Heavy assay + table quota for the "Anomaly table"
    # answer emphasis (plan §2b).
    "document": 1,
    "spatial": 1,
    "assay": 5,
    "table": 3,
    "collar": 1,
    "graph": 0,
}

_QUOTA_UNCERTAINTY_QUANTIFICATION: dict[str, int] = {
    # Conflict-detection enabled — we want documents (claim sources)
    # alongside the assay/table values they cite. Spatial helps when
    # the uncertainty is geometric.
    "document": 3,
    "spatial": 2,
    "assay": 3,
    "table": 2,
    "collar": 1,
    "graph": 1,
}

_QUOTA_DECISION_SUPPORT: dict[str, int] = {
    # "Ranked options" emphasis — same breadth as synthesis plus
    # explicit regulatory documents come through. Document quota
    # higher than synthesis to make room for regulatory citations.
    "document": 4,
    "spatial": 2,
    "assay": 2,
    "table": 1,
    "collar": 1,
    "graph": 1,
}

# ADR-0007 chat-card intents are SQL-aggregate-first; their structured
# tool results (ProjectSummaryResult, CoverageGapResult) feed straight
# into card payloads, not into the LLM context. The quotas are tight —
# the answer renders the card; the doc context is just for narration.
_QUOTA_PROJECT_SUMMARY: dict[str, int] = {
    "document": 2,
    "spatial": 0,
    "assay": 0,
    "table": 2,
    "collar": 0,
    "graph": 0,
}

_QUOTA_COVERAGE_GAP: dict[str, int] = {
    "document": 1,
    "spatial": 1,
    "assay": 0,
    "table": 3,
    "collar": 1,
    "graph": 0,
}


QUOTA_BY_INTENT: dict[str, dict[str, int]] = {
    "factual_lookup": _QUOTA_FACTUAL_LOOKUP,
    "synthesis": _QUOTA_SYNTHESIS,
    "hypothesis_generation": _QUOTA_HYPOTHESIS_GENERATION,
    "anomaly_detection": _QUOTA_ANOMALY_DETECTION,
    "uncertainty_quantification": _QUOTA_UNCERTAINTY_QUANTIFICATION,
    "decision_support": _QUOTA_DECISION_SUPPORT,
    "project_summary": _QUOTA_PROJECT_SUMMARY,
    "coverage_gap": _QUOTA_COVERAGE_GAP,
}


# ---------------------------------------------------------------------------
# Per-intent protected-kinds table
# ---------------------------------------------------------------------------
#
# When ``enforce_token_budget`` would otherwise drop a kind entirely to
# fit the budget, the protected set blocks that drop. We use this for
# kinds where the *intent* depends on the kind being present in the
# answer — e.g. anomaly_detection without any assay/table data is a
# refusal, not a degraded answer.


PROTECTED_KINDS_BY_INTENT: dict[str, frozenset[str]] = {
    # Factual: cite the document or refuse.
    "factual_lookup": frozenset({"document"}),
    # Synthesis: at minimum one document is the spine.
    "synthesis": frozenset({"document"}),
    # Hypothesis: hypotheses need a graph hop OR a document; protect both.
    "hypothesis_generation": frozenset({"document"}),
    # Anomaly: the answer IS the numeric table. Protect both assay AND
    # document so the answer can cite its sources.
    "anomaly_detection": frozenset({"assay", "document"}),
    # Uncertainty: documents are the claim sources; protect them.
    "uncertainty_quantification": frozenset({"document"}),
    # Decision: regulatory + project documents are the spine.
    "decision_support": frozenset({"document"}),
    # Project summary: documents drive the narration text.
    "project_summary": frozenset({"document"}),
    # Coverage gap: tables drive the coverage rows; documents for narration.
    "coverage_gap": frozenset({"table"}),
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedContext:
    """Output of :func:`prepare_evidence_for_intent`.

    Fields:
        packet: The prepared EvidencePacket — authority-ranked,
            diversity-balanced, budget-fit. This is what the assembler
            renders into ``context_block``.
        intent: Echoed back from the input — useful for trace logging
            without re-deriving from state.
        quota_used: The quota table that was applied. Echoed for trace
            logging + benchmark analysis.
        reached_budget: Whether ``enforce_token_budget`` could fit the
            packet inside the context window. False when the per-kind
            floor (or protected set) pinned enough evidence to keep
            ``remaining_budget < 0``.
        dropped_evidence_ids: IDs the budget pass dropped, in the order
            it dropped them. Empty when nothing was over budget.
        budget_reason: Short human-readable reason when
            ``reached_budget=False``. None when budget was reached.
        kind_distribution_before: Per-kind counts on the INPUT packet
            (before diversity + budget). Useful for benchmarks.
        kind_distribution_after: Per-kind counts on the OUTPUT packet.
    """

    packet: EvidencePacket
    intent: str
    quota_used: dict[str, int]
    reached_budget: bool
    dropped_evidence_ids: list[str] = field(default_factory=list)
    budget_reason: str | None = None
    kind_distribution_before: dict[str, int] = field(default_factory=dict)
    kind_distribution_after: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def prepare_evidence_for_intent(
    packet: EvidencePacket,
    intent: str | None,
    *,
    max_context_tokens: int | None = None,
    quota_override: Mapping[str, int] | None = None,
    protected_kinds_override: frozenset[str] | None = None,
    min_per_kind: int = 1,
) -> PreparedContext:
    """Chain authority + diversity + budget into one call.

    Args:
        packet: The raw EvidencePacket — typically what the converter
            produced from ``state.tool_results``.
        intent: One of the 8 agentic intents
            (see :data:`QUOTA_BY_INTENT`). ``None`` or an unknown
            intent → falls back to the ``synthesis`` quota (most
            balanced).
        max_context_tokens: Hard ceiling for the budget pass. When
            ``None``, the packet's existing ``remaining_budget`` is the
            truth.
        quota_override: When set, REPLACES the per-intent quota.
            Useful for A/B benchmarks.
        protected_kinds_override: When set, replaces the per-intent
            protected set.
        min_per_kind: Floor passed through to :func:`enforce_token_budget`.
            Default 1 keeps at least one entry per present kind.

    Returns:
        :class:`PreparedContext` — packet + audit trail.

    Notes:
        - Pure function. No I/O, no state mutation.
        - When the input packet has no evidence, returns it untouched
          with empty distributions.
    """
    before_dist = compute_kind_distribution(packet)

    if not packet.evidence:
        return PreparedContext(
            packet=packet,
            intent=intent or "(unspecified)",
            quota_used={},
            reached_budget=packet.remaining_budget >= 0,
            dropped_evidence_ids=[],
            budget_reason=None,
            kind_distribution_before={},
            kind_distribution_after={},
        )

    # 1. Annotate authority — refresh DocumentEvidence.authority_rank
    # from document_type. Idempotent: a packet already at the right
    # rank passes through unchanged.
    annotated = annotate_evidence_packet_with_authority(packet)

    # 2. Sort by authority + currency + confidence.
    ranked = rank_evidence_by_authority(annotated)

    # 3. Diversify with the per-intent quota.
    quota = _resolve_quota(intent, quota_override)
    diverse = apply_source_diversity(
        ranked,
        kind_quotas=quota,
        kind_priority=DEFAULT_KIND_PRIORITY,
    )

    # 4. Enforce budget. Per-intent protected kinds keep the answer's
    # spine intact even when the budget gets tight.
    protected = _resolve_protected(intent, protected_kinds_override)
    trim_result: BudgetTrimResult = enforce_token_budget(
        diverse,
        max_context_tokens=max_context_tokens,
        min_per_kind=min_per_kind,
        protected_kinds=protected,
    )

    after_dist = compute_kind_distribution(trim_result.packet)

    return PreparedContext(
        packet=trim_result.packet,
        intent=intent or "(unspecified)",
        quota_used=dict(quota),
        reached_budget=trim_result.reached_target,
        dropped_evidence_ids=list(trim_result.dropped_evidence_ids),
        budget_reason=trim_result.reason,
        kind_distribution_before=before_dist,
        kind_distribution_after=after_dist,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_quota(
    intent: str | None,
    override: Mapping[str, int] | None,
) -> dict[str, int]:
    """Return the active quota table.

    Override wins. Else look up by intent. Unknown intent → synthesis
    fallback. ``None`` intent → synthesis fallback.
    """
    if override is not None:
        return dict(override)
    if intent and intent in QUOTA_BY_INTENT:
        return dict(QUOTA_BY_INTENT[intent])
    logger.debug(
        "prepare_evidence_for_intent: unknown intent %r — falling back to synthesis quota",
        intent,
    )
    return dict(QUOTA_BY_INTENT["synthesis"])


def _resolve_protected(
    intent: str | None,
    override: frozenset[str] | None,
) -> frozenset[str]:
    """Return the active protected-kinds set."""
    if override is not None:
        return override
    if intent and intent in PROTECTED_KINDS_BY_INTENT:
        return PROTECTED_KINDS_BY_INTENT[intent]
    # Default: protect documents (the "spine" of most answers).
    return frozenset({"document"})
