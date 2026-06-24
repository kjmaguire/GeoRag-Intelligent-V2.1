"""RAG pipeline Pydantic models for the GeoRAG FastAPI service.

These models define the response contract for the Pydantic AI agent and the
Laravel↔FastAPI API surface (Section 07d of the architecture).

Citation is the hallucination-prevention backbone — every LLM claim requires
a Citation with a valid source_chunk_id (Layer 2: typed output validation).
GeoRAGResponse is the final assembled payload sent back to Laravel.

MapPayload and VizPayload are intentionally open-ended at this stage. Their
internal structures will be locked in during Milestone 2 when the
visualization layer is implemented. They are typed as Pydantic models (not
raw dicts) so future fields can be added without breaking validation.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.agent.schemas import GeoAnswer


class AnswerMode(str, Enum):
    """Plan §4a — three answer-shape modes the caller can request.

    ``detailed`` (default): full 8-section structured format
    (Direct answer / Key numbers / Evidence / Source citation /
    Assumptions / Confidence / Missing / Follow-up).

    ``short``: section 1 + section 4 only. ~50 tokens. Field mode
    (per `project_phase3_geologist_question_plan` Q21 decision).

    ``evidence_only``: section 3 + section 4 only. No synthesis, no
    confidence, no interpretation. For SMEs who want to interpret
    themselves.

    Resolved by the orchestrator's prompt selector + the response
    assembler. When unset on the request, defaults to ``detailed``
    on desktop surfaces and ``short`` on Field mode (Q21).
    """

    SHORT = "short"
    DETAILED = "detailed"
    EVIDENCE_ONLY = "evidence_only"


# ---------------------------------------------------------------------------
# Citation
# ---------------------------------------------------------------------------


class Citation(BaseModel):
    """A single citation linking an LLM claim to its grounding source chunk.

    citation_id is the display label shown to the user in the frontend,
    formatted as [NI43-1], [PUB-3], [DATA-7], or [PGEO-2]. It is assigned
    during response assembly, not stored in the database.

    source_chunk_id is the canonical provenance pointer — it references a
    Qdrant vector or RAGFlow chunk. Every Citation must have a non-empty
    source_chunk_id. Responses with any Citation missing this field are
    rejected by Pydantic AI's typed output validation (Layer 2).

    relevance_score is the cross-encoder reranking score (0.0–1.0). Chunks
    that fall below the retrieval quality gate threshold (Layer 1) must not
    appear as citations in the final response.

    corpus identifies the GeoRAG surface the citation originates from. This
    lets the chat UI label results by source surface when both corpora are
    queried together (plan §09b: "queries both corpora when ambiguous and
    labels results by source surface"). Legacy NI43/PUB/DATA citations have
    corpus='internal_archive'; Public Geoscience citations have
    corpus='public_geo'.
    """

    citation_id: str = Field(
        ...,
        min_length=1,
        description="Display label for the citation, e.g. [NI43-1], [PUB-3], [DATA-7], [PGEO-2]",
    )
    citation_type: Literal["NI43", "PUB", "DATA", "PGEO"] = Field(
        ...,
        description=(
            "NI43=NI 43-101 report, PUB=publication/paper, DATA=direct data query, "
            "PGEO=Public Geoscience (government-published)"
        ),
    )
    source_chunk_id: str = Field(
        ...,
        min_length=1,
        description="Qdrant/RAGFlow chunk ID. Must be non-empty — missing IDs are a validation failure.",
    )
    document_title: str = Field(..., min_length=1)
    section: str | None = Field(
        default=None,
        description="Section heading or table name within the source document",
    )
    page: int | None = Field(
        default=None,
        ge=1,
        description="Page number for PDF sources",
    )
    relevance_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Cross-encoder reranking score after retrieval",
    )

    # ── Public Geoscience extensions (plan §08) ────────────────────────
    # None on internal-archive citations; populated on PGEO citations so the
    # UI can render the jurisdiction-aware citation card without a second
    # round-trip to Laravel's resolver on hover.
    corpus: Literal["internal_archive", "public_geo"] | None = Field(
        default=None,
        description="Source surface — internal_archive (default) or public_geo",
    )
    jurisdiction_code: str | None = Field(
        default=None,
        description="ISO/jurisdiction code (e.g. 'CA-SK'). Only set on PGEO citations.",
    )
    jurisdiction_name: str | None = Field(
        default=None,
        description="Human-readable jurisdiction label (e.g. 'Saskatchewan'). Only set on PGEO citations.",
    )
    license_summary: str | None = Field(
        default=None,
        description="One-line license attribution (e.g. 'Saskatchewan Standard Unrestricted Use Data License v2.0').",
    )
    license_url: str | None = Field(
        default=None,
        description="Link to the full license terms for the citation's jurisdiction.",
    )
    source_url: str | None = Field(
        default=None,
        description="Direct deep link back to the upstream record (plan §08 'Open in GeoHub ↗').",
    )
    staleness_seconds: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Seconds since this entity was last refreshed from upstream. UI renders "
            "a 'data may be stale' hint when this exceeds the workspace threshold. "
            "Only set on PGEO citations."
        ),
    )


# ---------------------------------------------------------------------------
# MapPayload
# ---------------------------------------------------------------------------


class MapPayload(BaseModel):
    """GeoJSON payload for the MapLibre GL map layer.

    The frontend renders this directly as a GeoJSON FeatureCollection.
    Additional layer-control metadata (layer_id, style hints) travels
    alongside the GeoJSON so the React map component can register the
    layer without extra API calls.

    Internal structure will be finalized in Milestone 2. The geojson field
    accepts any valid GeoJSON dict for now; a stricter GeoJSON model will
    replace it once the visualization contract is locked.
    """

    layer_id: str = Field(..., min_length=1, description="Unique identifier for the map layer")
    layer_type: Literal["collar", "sample", "structure", "alteration", "custom"] = "custom"
    geojson: dict[str, Any] = Field(
        ...,
        description="Valid GeoJSON FeatureCollection or Feature object",
    )
    bbox: tuple[float, float, float, float] | None = Field(
        default=None,
        description="Bounding box [min_lon, min_lat, max_lon, max_lat] for auto-zoom",
    )
    label: str | None = Field(default=None, description="Human-readable label for layer toggle UI")


# ---------------------------------------------------------------------------
# VizPayload
# ---------------------------------------------------------------------------


class VizPayload(BaseModel):
    """Plotly chart payload for inline data visualization.

    The frontend passes data and layout directly to a Plotly React component.
    chart_type is a hint for the frontend to select the right component
    (downhole strip log, cross-section, grade-tonnage curve, etc.).

    Internal structure will be finalized in Milestone 2.
    """

    chart_type: Literal[
        "downhole_strip",
        "cross_section",
        "grade_tonnage",
        "assay_histogram",
        "graph_viz",
        "drill_trace_3d",
        "stereonet",
        # ADR-0007 PR-1 chat-card chart types:
        #   technique_timeline — horizontal swimlanes by technique × year
        #     (TimelineCard reads plotly_layout.meta.swimlanes + breakdown_table)
        #   coverage_table     — per-attribute coverage rollup
        #     (CoverageTableCard reads plotly_layout.meta.rows)
        "technique_timeline",
        "coverage_table",
        "custom",
    ] = "custom"
    plotly_data: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Plotly trace objects (passed directly to Plotly.react)",
    )
    plotly_layout: dict[str, Any] = Field(
        default_factory=dict,
        description="Plotly layout object",
    )
    title: str | None = None


# ---------------------------------------------------------------------------
# GeoRAGResponse
# ---------------------------------------------------------------------------


class GeoRAGResponse(BaseModel):
    """Final assembled RAG response returned from the Pydantic AI agent.

    This is the payload that FastAPI sends back to Laravel (Section 07d),
    which in turn broadcasts it as an SSE 'completed' event via Reverb.

    Every response MUST include at least one Citation with a valid
    source_chunk_id. Responses with an empty citations list or any Citation
    missing source_chunk_id are rejected at the Pydantic AI output validation
    stage (hallucination prevention Layer 2).

    confidence is a composite score calculated by the agent based on:
    - Average cross-encoder relevance of cited chunks (Layer 1)
    - Numerical claim verification pass rate (Layer 3)
    - Entity resolution confidence (Layer 4)
    - Chunk provenance similarity (Layer 5)

    sources_used lists all chunk IDs and [DATA-X] references that
    contributed to the response, including those not surfaced as inline
    citations. This supports audit and debugging workflows.
    """

    text: str = Field(..., min_length=1, description="The LLM-generated answer text with inline citation markers")
    # Persisted silver.answer_runs.answer_run_id once the orchestrator has
    # INSERTed the row. None on the pre-INSERT refusal paths (LLM health
    # probe / out-of-scope classifier) where no row is written. The Reverb
    # `completed` SSE frame surfaces this so the Retrieval Inspector can
    # deep-link to /retrieval/{id} with a real PK rather than the
    # streaming-session UUID stamped by EventStamper (which is NOT the DB id).
    answer_run_id: UUID | None = Field(
        default=None,
        description=(
            "silver.answer_runs.answer_run_id for this run. Stamped by the "
            "orchestrator after the row is persisted. Frontend uses this for "
            "the Retrieval Inspector deep link."
        ),
    )
    citations: list[Citation] = Field(
        ...,
        min_length=1,
        description="All citations supporting claims in text. Must be non-empty.",
    )
    map_payload: MapPayload | None = Field(
        default=None,
        description="Optional GeoJSON map layer; present when the answer references spatial features",
    )
    viz_payload: VizPayload | None = Field(
        default=None,
        description="Optional Plotly chart; present when the answer includes quantitative visualizations",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Composite confidence score across all 6 hallucination prevention layers",
    )
    sources_used: list[str] = Field(
        ...,
        min_length=1,
        description="All chunk IDs and [DATA-X] references that contributed to this response",
    )
    # C7 — retrieval-surface health signal. When a tool errored or timed out
    # mid-query, the orchestrator falls through with partial data
    # (tools.py:18-20 "partial data is always preferable to a hard failure").
    # Without a surface like this, the UI renders the thin answer as if
    # everything succeeded. Populate with human-readable source labels —
    # e.g. ["Qdrant (timeout)", "Neo4j (connection error)"] — so the
    # frontend can show a "degraded sources" warning chip in the citation
    # panel. Empty list means all sources succeeded.
    degraded_sources: list[str] = Field(
        default_factory=list,
        description=(
            "Retrieval sources that errored or timed out during this query. "
            "Empty when all sources returned cleanly. Frontend renders a "
            "degraded-sources warning when non-empty."
        ),
    )
    # Plan §4b — typed guard codes that fired during post-assembly
    # validation + demotion + composite-signal checks. Each value is a
    # `GuardErrorCode.value` string (see app.agent.guards). Empty when
    # all guards passed. The frontend reads this to dispatch the right
    # renderer per code (RefusalBanner / AmbiguityPicker / etc., see
    # docs/architecture/user_facing_error_catalog.md). Stable enum
    # values let the Laravel side resolve user-facing text via
    # `__('guard_errors.' . $code)` without parsing free-form strings.
    guard_error_codes: list[str] = Field(
        default_factory=list,
        description=(
            "Typed plan §4b GuardErrorCode values that fired for this "
            "query (e.g. ['NUMERIC_GROUNDING_FAILED', 'CITATION_INCOMPLETE']). "
            "Empty when all guards passed. Frontend dispatches a per-code "
            "renderer; Laravel resolves user-facing text via i18n. Mirror "
            "of silver.query_traces.guard_failure_codes for the request "
            "response path."
        ),
    )
    # D3 — post-answer follow-up suggestions. Rendered as clickable chips
    # under the completed assistant message; each is a fully-formed query
    # that can be sent as-is. Empty list means no useful follow-ups were
    # generatable from the current response (refusal, too-narrow context).
    followups: list[str] = Field(
        default_factory=list,
        description=(
            "Up to 3 follow-up query suggestions synthesised from the answer "
            "and tool results. Clicking one sends it as a new user query."
        ),
    )
    # 2026-06-01 — sentence-level grounding report. Optional advisory
    # output of the NLI-style verifier (see app.services.sentence_grounding).
    # When SENTENCE_GROUNDING_ENABLED is on, every cited sentence is checked
    # against its cited chunks and tagged supported / unsupported /
    # uncited / unverified. Frontend renders a per-sentence "may not be
    # supported by sources" indicator on unsupported sentences. None when
    # the verifier is disabled.
    # Shape: {sentences: [{text, verdict, cited_chunk_ids, rationale}], summary: {verdict: count}, verifier_ran, verifier_error}
    grounding_report: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Sentence-level grounding verification report. Advisory only — "
            "answer text is not modified. Renderer surfaces per-sentence "
            "support badges; None when SENTENCE_GROUNDING_ENABLED is off."
        ),
    )
    # Module 6 Phase B Chunk 4a — structured refusal payload (spec B4).
    # Present when citation guards rejected the answer or an LLM / timeout
    # error occurred.  None on successful committed answers.
    # Shape: {type, reason_code, searched, missing, message, failed_guards}
    # Module 7 branches on reason_code for the refusal UI rendering path.
    refusal_payload: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Structured refusal payload (spec B4). Present when type='refusal'. "
            "Carries reason_code, searched block, missing block, and message. "
            "Module 7 branches on reason_code for refusal UI rendering."
        ),
    )
    # Module 6 Phase B Chunk 4b — conflict detection (spec B7).
    # Side-by-side evidence conflicts surfaced for Module 7 rendering.
    # Each entry: {entity_key, property_name, evidence_ids, values}.
    # Global Invariant 7: NEVER silently pick a winner — always surface both.
    # None when no conflicts were detected (normal case).
    conflicting_evidence: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Structured-record and graph-edge evidence conflicts detected in "
            "the bound evidence set. Each entry carries entity_key, property_name, "
            "evidence_ids, and values (parallel lists). Module 7 renders side-by-side "
            "conflict cards. None when no conflicts are present."
        ),
    )
    # Module 6 Phase B Chunk 4b — freshness metadata (spec B7).
    # Carries the data_version values snapshotted at query time so Module 7
    # can compute staleness at render time by comparing against current
    # workspaces.data_version / projects.data_version.
    freshness: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Per-answer freshness metadata. Keys: workspace_data_version_at_query "
            "(int), project_data_version_at_query (int | None), answered_at (ISO8601 str). "
            "Module 7 compares workspace_data_version_at_query against current "
            "workspaces.data_version to compute staleness banners."
        ),
    )

    # Phase 1 / Step 1.2 — OIUR structured answer.
    # Populated when ``settings.GEO_ANSWER_OIUR_ENABLED=True`` AND the LLM
    # output parses cleanly into the four-section contract. Carries its own
    # nested citation_ids (referencing this response's ``citations`` list);
    # the existing flat ``text`` field remains the always-present fallback.
    # See ``app/agent/schemas/geo_answer.py`` for the contract.
    geo_answer: GeoAnswer | None = Field(
        default=None,
        description=(
            "OIUR structured answer (Observations / Interpretations / Uncertainty / "
            "Recommended actions). Optional during Phase 1 rollout — None when the "
            "OIUR flag is off or when OIUR parsing falls back to the flat-text path."
        ),
    )

    # Plan §3a/§3b — typed evidence packet.
    # Populated by ``persist_node`` after the agentic graph builds an
    # ``EvidencePacket`` from ``tool_results`` (via the
    # ``app.agent.evidence_converter`` bridge, then
    # ``annotate_evidence_packet_with_authority`` +
    # ``rank_evidence_by_authority``). Frontend reads:
    #
    #   - ``evidence_packet.evidence[].kind == "spatial"`` → mount MapLibre
    #     card with the geometry hint.
    #   - ``evidence_packet.evidence[].kind == "table"`` → mount table card.
    #   - ``evidence_packet.evidence[0]`` (the authority-ranked first entry)
    #     → the "primary source" claim block in §3b's hierarchy.
    #   - ``evidence_packet.remaining_budget`` → a budget-pressure pill in
    #     the citation panel when the value is small.
    #
    # Serialised as `dict[str, Any]` rather than the typed Pydantic model
    # so the JSON contract stays loose at the wire boundary — additive
    # changes to the typed model (a new kind, a new field on an existing
    # kind) don't require a coordinated frontend deploy. The typed model
    # lives in ``app.agent.evidence`` and is the source of truth on the
    # Python side; the dict here is its ``.model_dump()`` form.
    #
    # None when the agentic graph wasn't engaged (legacy deterministic
    # path) or when the converter failed for this query.
    evidence_packet: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Plan §3a/§3b typed evidence packet (model_dump form). Carries "
            "authority-ranked evidence list + remaining_budget + tool_plan. "
            "Frontend reads .evidence[].kind to dispatch the right card "
            "(spatial → map, table → table card, document → citation). "
            "None when the agentic graph wasn't engaged."
        ),
    )

    # Plan §3e — multi-turn resolution audit (subset of what lands in
    # silver.query_traces.multi_turn_resolution). Stamped by persist_node
    # when resolve_node rewrote the query. Frontend reads:
    #   - original_query   → rendered as "you asked: …"
    #   - rewritten_query  → rendered as "I interpreted as: …"
    #   - overall_confidence → drives chip colour (high/medium/low)
    # None when resolve_node was off OR made no changes.
    multi_turn_resolution: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Plan §3e — multi-turn resolution audit for the chat UI's "
            "'Interpreted as:' preview chip. Shape: {original_query, "
            "rewritten_query, trace, overall_confidence}. None when no "
            "rewrite happened."
        ),
    )
