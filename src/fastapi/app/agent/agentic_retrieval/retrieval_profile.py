"""Per-intent retrieval profiles — Phase 2 / Step 2.3.

Each of the six intents maps to a :class:`RetrievalProfile` that controls
*how* the execute node calls the existing tool layer:

  - ``primary_tools`` — which tools to invoke unconditionally
  - ``secondary_tools`` — extra tools to invoke when the intent demands
    broader coverage (e.g. hypothesis-generation's adversarial pass)
  - ``bm25_weight`` — fraction of the hybrid retrieval mix that should go
    to sparse / keyword search. 0.0 = dense-only, 1.0 = sparse-only
  - ``conflict_detection_enabled`` — when true, the assemble step inspects
    the tool_results for conflicting numeric values on the same entity
    and populates ``GeoRAGResponse.conflicting_evidence`` (Phase 1.3
    confidence demotion already keys off this)
  - ``adversarial_pass_enabled`` — when true, the execute node fires a
    second retrieval pass with a "find disconfirming evidence" prompt
    framing. Cheap approximation; uses the same corpus, not a separate
    index
  - ``surface_qa_qc_fields`` — anomaly subgraph hint. The execute node
    biases toward `query_assay_data` and tries to surface QA/QC fields
    (blank/CRM/duplicate). Degrades gracefully on pre-Phase-4 schemas
  - ``require_regulatory_constraints`` — decision_support hint, set when
    the classifier flagged regulatory_touch. Affects prompt selection so
    the LLM is required to emit ≥1 NI 43-101 / CIM / CRIRSCO implication
    in ``GeoAnswer.decision_support.regulatory_constraints``
  - ``answer_emphasis`` — which OIUR sections the prompt should bias
    toward (e.g. ``observations_table`` for anomaly detection)
  - ``max_chunks`` — soft cap on the retrieved-chunk count passed to the
    LLM context

These profiles are **declarative**. The execute node interprets them
into actual tool invocations; the profile itself contains no I/O.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.agent.agentic_retrieval.intent_classifier import Intent

AnswerEmphasis = Literal[
    "exact_citation",
    "synthesis_with_conflicts",
    "competing_hypotheses",
    "anomaly_table",
    "uncertainty_drivers",
    "ranked_options",
    # ADR-0007 PR-1 — structured-aggregation answer shapes that pair with
    # the new chat-card payloads (technique_timeline + coverage_table).
    "breakdown_table",
    "coverage_table",
]


class RetrievalProfile(BaseModel):
    """Declarative retrieval recipe for one intent."""

    intent: Intent
    primary_tools: list[str] = Field(
        ...,
        min_length=1,
        description="Tools the execute node MUST invoke for this intent.",
    )
    secondary_tools: list[str] = Field(
        default_factory=list,
        description="Tools invoked only when secondary signals warrant it.",
    )
    # ── Wired into the execute/assemble path ──────────────────────────────
    adversarial_pass_enabled: bool = False  # nodes.execute_node:565
    surface_qa_qc_fields: bool = False       # nodes.assemble_node:903
    answer_emphasis: AnswerEmphasis = "synthesis_with_conflicts"  # :883

    # ── NOT YET WIRED (audit 2026-06-28) ──────────────────────────────────
    # These fields are declared + set per-intent but the execute path does not
    # consume them yet. They are kept (not deleted) because applying each one
    # changes retrieval breadth / ranking / output and therefore needs a
    # golden-eval pass before flipping — blind-wiring would shift answer quality
    # untested (same gating as the Qwen3 query-prefix item). Documented here so
    # the profile does not misrepresent itself as tuning the pipeline.
    bm25_weight: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Intended sparse-vs-dense mix for document search. NOT YET WIRED — "
            "search_documents currently reads a global sparse_boost setting, "
            "not this per-intent value. Wiring is eval-gated."
        ),
    )
    conflict_detection_enabled: bool = Field(
        default=False,
        description=(
            "Intended to make assemble inspect chunks for contradictions. NOT "
            "YET WIRED — currently only logged in route_node, not acted on."
        ),
    )
    require_regulatory_constraints: bool = Field(
        default=False,
        description=(
            "Intended to force Layer-6 regulatory constraint checks for "
            "decision_support. NOT YET WIRED — currently only logged."
        ),
    )
    max_chunks: int = Field(
        default=12, ge=1, le=50,
        description=(
            "Intended soft cap on chunks passed to the assembler. NOT YET "
            "WIRED — the secondary-tool coverage heuristic uses a hardcoded "
            "threshold, not this value. Wiring is eval-gated."
        ),
    )


# ---------------------------------------------------------------------------
# Per-intent profiles — sourced verbatim from the plan's Step 2.3 table
# (lines 219-251 of georag-geologist-question-plan.md).
# ---------------------------------------------------------------------------


_PROFILES: dict[Intent, RetrievalProfile] = {
    "factual_lookup": RetrievalProfile(
        intent="factual_lookup",
        # Standards corpus prioritised; spatial / assay tools rarely useful.
        primary_tools=["search_documents"],
        secondary_tools=[],
        # BM25-weighted because standards documents (NI 43-101, CRIRSCO,
        # ICS) carry precise clause language the sparse encoder matches well.
        bm25_weight=0.75,
        answer_emphasis="exact_citation",
        max_chunks=6,
    ),
    "synthesis": RetrievalProfile(
        intent="synthesis",
        # Broad multi-source — every retrieval store contributes.
        primary_tools=[
            "search_documents",
            "query_spatial_collars",
            "query_downhole_logs",
            "query_assay_data",
            "traverse_knowledge_graph",
        ],
        secondary_tools=["query_project_overview"],
        bm25_weight=0.5,
        conflict_detection_enabled=True,
        answer_emphasis="synthesis_with_conflicts",
        max_chunks=16,
    ),
    "hypothesis_generation": RetrievalProfile(
        intent="hypothesis_generation",
        # First pass: supporting evidence. Adversarial pass runs against
        # the same corpus with a disconfirming-evidence prompt framing
        # (see execute_node.run_adversarial_pass).
        primary_tools=[
            "search_documents",
            "traverse_knowledge_graph",
            "query_assay_data",
        ],
        secondary_tools=["query_spatial_collars"],
        bm25_weight=0.4,
        adversarial_pass_enabled=True,
        answer_emphasis="competing_hypotheses",
        max_chunks=16,
    ),
    "anomaly_detection": RetrievalProfile(
        intent="anomaly_detection",
        # Assay schema + QA/QC fields targeted. Falls back gracefully when
        # Phase 4 QA/QC fields are not yet present.
        primary_tools=["query_assay_data", "query_downhole_logs"],
        secondary_tools=["search_documents"],
        bm25_weight=0.3,
        surface_qa_qc_fields=True,
        answer_emphasis="anomaly_table",
        max_chunks=20,
    ),
    "uncertainty_quantification": RetrievalProfile(
        intent="uncertainty_quantification",
        # Retrieve conflicting chunks deliberately + the supporting chunks.
        primary_tools=[
            "search_documents",
            "query_assay_data",
            "query_spatial_collars",
        ],
        secondary_tools=["query_downhole_logs"],
        bm25_weight=0.5,
        conflict_detection_enabled=True,
        answer_emphasis="uncertainty_drivers",
        max_chunks=14,
    ),
    "decision_support": RetrievalProfile(
        intent="decision_support",
        # Evidence for ALL candidate options before ranking — same broad
        # retrieval as synthesis plus the graph for analogues.
        primary_tools=[
            "search_documents",
            "query_spatial_collars",
            "query_assay_data",
            "traverse_knowledge_graph",
        ],
        secondary_tools=["query_downhole_logs", "query_project_overview"],
        bm25_weight=0.5,
        # require_regulatory_constraints is set dynamically from the
        # classifier's regulatory_touch flag (see profile_for_intent).
        answer_emphasis="ranked_options",
        max_chunks=18,
    ),
    # ADR-0007 PR-1 — structured-aggregation profiles. SQL aggregate is the
    # primary tool; search_documents is secondary so the LLM can pull
    # narrative context (campaign descriptions, contractor mentions) when
    # the structured rows alone don't carry enough text for a fluent
    # answer body.
    "project_summary": RetrievalProfile(
        intent="project_summary",
        primary_tools=["query_project_summary"],
        secondary_tools=["search_documents", "query_project_overview"],
        bm25_weight=0.5,
        answer_emphasis="breakdown_table",
        max_chunks=8,
    ),
    "coverage_gap": RetrievalProfile(
        intent="coverage_gap",
        primary_tools=["query_coverage_gap"],
        secondary_tools=["search_documents", "query_project_overview"],
        bm25_weight=0.5,
        answer_emphasis="coverage_table",
        max_chunks=8,
    ),
}


def profile_for_intent(
    intent: Intent,
    *,
    regulatory_touch: bool = False,
) -> RetrievalProfile:
    """Return the retrieval profile for *intent*.

    Returns a copy with ``require_regulatory_constraints`` flipped to True
    on decision-support queries that touch resource classification,
    drilling, sampling, or QA/QC (the plan's NI 43-101 implication gate).
    """
    base = _PROFILES[intent]
    if intent == "decision_support" and regulatory_touch:
        return base.model_copy(update={"require_regulatory_constraints": True})
    return base


__all__ = [
    "AnswerEmphasis",
    "RetrievalProfile",
    "profile_for_intent",
]
