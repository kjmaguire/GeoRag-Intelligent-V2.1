"""Per-intent retrieval profiles — Phase 2 / Step 2.3.

Each of the six intents maps to a :class:`RetrievalProfile` that controls
*how* the execute node calls the existing tool layer:

  - ``primary_tools`` — which tools to invoke unconditionally
  - ``secondary_tools`` — extra tools to invoke when the intent demands
    broader coverage (e.g. hypothesis-generation's adversarial pass)
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

    # ── Deleted 2026-10-04 (audit item 23): ``bm25_weight`` and ``max_chunks``
    # were declared and set for every intent but NEVER read -- retrieval fuses
    # with a bare Fusion.RRF and keeps RERANKER_TOP_K chunks for every intent --
    # so profiles read as if they tuned retrieval and did not. Wiring either one
    # (per-branch RRF weights, a per-intent top-k) changes which chunks reach the
    # answer and needs a golden-eval pass first; if that is ever done, add the
    # field back WITH its reader in search_documents / qdrant_service.hybrid_query
    # in the same change. (The field-mode ``max_chunks`` on RetrievalFilters in
    # preprocessor.py is a separate, equally unread, value.)
    #
    # ── Declared, set per intent, and only LOGGED (re-audited 2026-10-04) ──
    # The two fields below -- ``conflict_detection_enabled`` and
    # ``require_regulatory_constraints`` -- are read by exactly one thing: the
    # route node's log line (nodes.route_node). Neither changes tool dispatch,
    # prompts or validation. They are kept (not deleted) because wiring either
    # changes what the answer says and needs a golden-eval pass first; deleting
    # them is equally legitimate. Documented here so the profile does not
    # misrepresent itself as tuning the pipeline. (``bm25_weight`` and
    # ``max_chunks``, which this header used to cover as well, are gone --
    # see the "Deleted" note above.)
    conflict_detection_enabled: bool = Field(
        default=False,
        description=(
            "Declares that this intent expects conflicting evidence to be "
            "surfaced. Still not a switch: what actually drives conflict "
            "surfacing is answer_emphasis='synthesis_with_conflicts', whose "
            "prompt fragment orders the model to emit a '### Conflicting "
            "evidence' sub-section, plus the parse in validate_node that reads "
            "that section back into GeoRAGResponse.conflicting_evidence "
            "(app/agent/conflict_extraction.py, wired 2026-08-21). Both "
            "profiles that set this flag also set that emphasis, so the two "
            "have never disagreed — but they could, and this one is the "
            "duplicate. Either delete it or make it the single gate; do not "
            "leave a second declaration of the same intent."
        ),
    )
    require_regulatory_constraints: bool = Field(
        default=False,
        description=(
            "Intended to force Layer-6 regulatory constraint checks for "
            "decision_support. NOT YET WIRED — currently only logged."
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
        # Public geoscience is a SECONDARY, not primary, tool: it only fires
        # when the internal corpus came back under _SECONDARY_COVERAGE_
        # THRESHOLD, which is exactly the "we have little in-house on this,
        # check the government record" case. Keeping it off the primary list
        # means a well-covered factual lookup never pays its latency.
        secondary_tools=["search_public_geoscience"],
        answer_emphasis="exact_citation",
    ),
    # NOTE (audit 2026-08-14, finding 7): "traverse_knowledge_graph" was
    # removed from every profile below. Neo4j was removed from the stack
    # (B1, 2026-07-28) — the tool early-returns an empty
    # GraphTraversalResult unconditionally, so listing it as a primary tool
    # only burned an execute-node dispatch and injected a guaranteed-empty
    # "Neo4j knowledge graph (unavailable)" block into telemetry. Re-add it
    # here if a graph store ever returns.
    "synthesis": RetrievalProfile(
        intent="synthesis",
        # Broad multi-source — every live retrieval store contributes.
        primary_tools=[
            "search_documents",
            "query_spatial_collars",
            "query_downhole_logs",
            "query_assay_data",
        ],
        secondary_tools=["query_project_overview", "search_public_geoscience"],
        conflict_detection_enabled=True,
        answer_emphasis="synthesis_with_conflicts",
    ),
    "hypothesis_generation": RetrievalProfile(
        intent="hypothesis_generation",
        # First pass: supporting evidence. Adversarial pass runs against
        # the same corpus with a disconfirming-evidence prompt framing
        # (see execute_node.run_adversarial_pass).
        primary_tools=[
            "search_documents",
            "query_assay_data",
        ],
        secondary_tools=["query_spatial_collars"],
        adversarial_pass_enabled=True,
        answer_emphasis="competing_hypotheses",
    ),
    "anomaly_detection": RetrievalProfile(
        intent="anomaly_detection",
        # Assay schema + QA/QC fields targeted. Falls back gracefully when
        # Phase 4 QA/QC fields are not yet present.
        primary_tools=["query_assay_data", "query_downhole_logs"],
        secondary_tools=["search_documents"],
        surface_qa_qc_fields=True,
        answer_emphasis="anomaly_table",
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
        conflict_detection_enabled=True,
        answer_emphasis="uncertainty_drivers",
    ),
    "decision_support": RetrievalProfile(
        intent="decision_support",
        # Evidence for ALL candidate options before ranking — same broad
        # retrieval as synthesis.
        primary_tools=[
            "search_documents",
            "query_spatial_collars",
            "query_assay_data",
        ],
        secondary_tools=["query_downhole_logs", "query_project_overview"],
        # require_regulatory_constraints is set dynamically from the
        # classifier's regulatory_touch flag (see profile_for_intent).
        answer_emphasis="ranked_options",
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
        answer_emphasis="breakdown_table",
    ),
    "coverage_gap": RetrievalProfile(
        intent="coverage_gap",
        primary_tools=["query_coverage_gap"],
        secondary_tools=["search_documents", "query_project_overview"],
        answer_emphasis="coverage_table",
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
