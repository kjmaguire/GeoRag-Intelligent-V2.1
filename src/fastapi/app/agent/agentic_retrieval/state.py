"""State shape for the agentic-retrieval LangGraph — Phase 2 / Step 2.3.

Single shared :class:`AgenticRetrievalState` flows through the graph;
each node returns a partial-update dict that LangGraph merges into the
state. We use a Pydantic model (matching the report-builder graph's
style) so the state stays inspectable and serialisable.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent.agentic_retrieval.context_envelope import ContextEnvelope
from app.agent.agentic_retrieval.intent_classifier import Intent, IntentResult
from app.agent.agentic_retrieval.preprocessor import RetrievalFilters
from app.agent.agentic_retrieval.retrieval_profile import RetrievalProfile
from app.agent.evidence import EvidencePacket
from app.models.rag import GeoRAGResponse


class AgenticRetrievalState(BaseModel):
    """LangGraph state container for one agentic-retrieval run."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ── Inputs (set by the entry function) ──────────────────────────────
    query: str = Field(..., min_length=1, description="Raw user query text.")
    deps: Any = Field(
        ...,
        description=(
            "Caller's AgentDeps bundle (pg_pool, neo4j_driver, redis_client, "
            "anthropic_client, openai_http_client, project_id, …). Typed as "
            "Any to avoid a circular import on the orchestrator module."
        ),
    )
    context_envelope: ContextEnvelope | None = Field(
        default=None,
        description=(
            "Phase 2 / Step 2.4 — the 12 context fields a query may carry. "
            "None means 'fully unspecified' (legacy callers); the envelope-"
            "router treats this as all-fields-unspecified."
        ),
    )

    # ── Classify node output ─────────────────────────────────────────────
    intent: Intent | None = None
    intent_result: IntentResult | None = None

    # ── Envelope router output (Step 2.4) ────────────────────────────────
    effective_intent: Intent | None = None
    envelope_override_reason: str | None = None
    envelope_notes: list[str] = Field(default_factory=list)

    # ── Route node output ────────────────────────────────────────────────
    retrieval_profile: RetrievalProfile | None = None
    retrieval_filters: RetrievalFilters | None = None

    # ── Execute node output ──────────────────────────────────────────────
    tool_results: list[tuple[str, Any]] = Field(default_factory=list)

    # Plan §3a/§3b — typed EvidencePacket built by execute_node from
    # tool_results via app.agent.evidence_converter.build_evidence_packet.
    # Best-effort: a converter failure leaves this None and the legacy
    # tool_results path keeps working. Downstream consumers (response
    # assembler, validators, trace_writer, the future MapLibre trigger
    # in Chat.tsx) read from this when present.
    evidence_packet: EvidencePacket | None = None

    # ── Assemble node output ─────────────────────────────────────────────
    response: GeoRAGResponse | None = None

    # ── Validate node output ─────────────────────────────────────────────
    validation_warnings: list[str] = Field(default_factory=list)

    # ── Demote node output ───────────────────────────────────────────────
    demotion_reasons: list[str] = Field(default_factory=list)

    # ── Latency tracking — set by run_agentic_retrieval at entry ─────────
    # Used by persist_node to compute silver.answer_runs.latency_ms. None
    # in tests that drive nodes directly; the persist write falls back to
    # NULL latency in that case.
    run_start_monotonic: float | None = None

    # ── Plan §0b — system prompt token estimate ──────────────────────────
    # Populated by assemble_node after _select_system_prompt builds the
    # full prompt block (chars/4 estimate). Read by persist_node into
    # silver.query_traces.system_prompt_tokens. None when the system
    # prompt path was skipped (test paths driving nodes directly).
    system_prompt_tokens_estimate: int | None = None

    # ── Plan §4b/§4c — repair loop state extensions ─────────────────────
    # See docs/architecture/repair_loop_spec.md §3 for the contract.
    # Stage 1 (shadow mode): repair_shadow_node populates these from
    # plan_repair() output without re-issuing the graph. Stage 4 (full
    # mode): a future repair_loop_node reads + mutates them on each
    # iteration.
    repair_attempts: list[Any] = Field(
        default_factory=list,
        description=(
            "RepairAttempt records — one per repair iteration. Empty until "
            "the repair loop fires. Used by detect_death_loop on each pass."
        ),
    )
    repair_strategy_history: list[str] = Field(
        default_factory=list,
        description=(
            "RepairStrategy.value strings, in execution order. Persisted to "
            "silver.query_traces.repair_strategies_used. In shadow mode this "
            "captures what the loop WOULD have attempted; in full mode it "
            "captures what it DID attempt."
        ),
    )
    repair_terminal_reason: str | None = Field(
        default=None,
        description=(
            "Short human-readable reason the repair loop ended terminally. "
            "None when the loop didn't fire or completed normally."
        ),
    )
    repair_codes_observed: list[str] = Field(
        default_factory=list,
        description=(
            "GuardErrorCode.value strings classified during the shadow pass. "
            "Distinct from silver.query_traces.guard_failure_codes (which is "
            "written by persist_node) so the trace can see EXACTLY what the "
            "repair planner saw, even if persist's classify_guards call has "
            "drifted (different inputs)."
        ),
    )

    # ── Plan §3e — multi-turn resolution state ──────────────────────────
    # See docs/architecture/multi_turn_resolution_spec.md.
    # Caller threads conversation history through `run_agentic_retrieval(...,
    # history=...)`. The resolve_node uses it to rewrite state.query
    # IN PLACE so all downstream nodes see the expanded form. The
    # original is preserved on `query_original` for trace logging.
    history: list[Any] = Field(
        default_factory=list,
        description=(
            "Optional conversation history — list[ConversationTurn]. "
            "Empty when the caller didn't supply it (legacy bridge / "
            "single-turn queries). Stored as Any to keep the import "
            "off the state module."
        ),
    )
    query_original: str | None = Field(
        default=None,
        description=(
            "The query text BEFORE multi-turn resolution rewrote it. "
            "None when resolve_node didn't fire or made no changes."
        ),
    )
    resolution_trace: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Per-substitution audit trail from resolve_multi_turn. Each "
            "entry: {kind, original_phrase, resolved_to, source_turn_index, "
            "confidence}. Empty when resolve_node didn't fire or made no "
            "changes."
        ),
    )
    resolution_confidence: float | None = Field(
        default=None,
        description=(
            "Overall confidence from resolve_multi_turn (0.0-1.0). "
            "Lower when references couldn't be resolved. Propagates "
            "into the answer's confidence as a demotion signal."
        ),
    )

    # Plan §3 — context-prep audit payload from assemble_node's
    # prepare_evidence_for_intent call. Stamped onto state when
    # CONTEXT_PREP_ENABLED is True; persist_node writes it to the
    # silver.query_traces.context_prep_audit JSONB column. None
    # otherwise.
    context_prep_audit_payload: dict[str, Any] | None = Field(
        default=None,
        description=(
            "PreparedContext audit fields stamped by assemble_node "
            "(intent, quota_used, reached_budget, dropped_evidence_ids, "
            "budget_reason, kind_distribution_before, "
            "kind_distribution_after). None when context-prep was off."
        ),
    )
