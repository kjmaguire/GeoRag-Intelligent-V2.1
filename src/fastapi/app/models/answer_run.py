"""Pydantic models for silver.answer_runs and silver.answer_retrieval_items.

These models mirror the two tables introduced in Module 4 Phase B Chunk 1
(migrations 2026_04_21_100000 and 2026_04_21_110000).

Design
------
Literal types mirror the DB CHECK constraints exactly.  If a new value is added
to a CHECK constraint, update the Literal here in the same PR — the types are
the contract surface for the orchestrator, test harness, and any future admin
API.

FK handling
-----------
    AnswerRunCreate.workspace_id  → silver.workspaces.workspace_id (UUID)
    AnswerRunCreate.project_id    → silver.projects.project_id     (UUID, nullable)
    AnswerRunCreate.user_id       → public.users.id                (BIGINT, nullable)
    AnswerRetrievalItemCreate.answer_run_id        → silver.answer_runs
    AnswerRetrievalItemCreate.workspace_id         → silver.workspaces
    AnswerRetrievalItemCreate.document_revision_id → silver.document_revisions (nullable)
    AnswerRetrievalItemCreate.passage_id           → silver.document_passages  (nullable)

Status
------
Tables exist post-migration 2026-04-21.  Orchestrator INSERT wiring is Phase B
Chunk 2.  No endpoints currently read from these tables (Module 6 scope for
answer inspector UI).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Literal types — mirror DB CHECK constraints exactly
# ---------------------------------------------------------------------------

QueryClassLiteral = Literal[
    "factual",
    "spatial",
    "document",
    "computation",
    "viz",
    "unknown",
]

FusionMethodLiteral = Literal["rrf", "dbsf"]

BackendLiteral = Literal["vllm", "ollama", "anthropic"]

CitationLifecycleStateLiteral = Literal[
    "draft",
    "generated",
    "validated",
    "committed",
    "rejected",
]

CitationModeLiteral = Literal[
    "posthoc_span_resolution",
    "hybrid_delayed_attachment",
]

StageLiteral = Literal["retrieved", "reranked", "in_context", "cited"]

SourceStoreLiteral = Literal["qdrant", "neo4j", "postgis", "hybrid"]

# ---------------------------------------------------------------------------
# RefusalReasonCode — Module 6 Phase B Chunk 4a (spec B4)
# Module 7 refusal UI branches on this enum.
# ---------------------------------------------------------------------------

RefusalReasonCode = Literal[
    "insufficient_evidence",
    "guard_numeric_fail",
    "guard_entity_fail",
    "guard_completeness_fail",
    "llm_unavailable",
    "budget_exhausted",
]


# ---------------------------------------------------------------------------
# AnswerRun
# ---------------------------------------------------------------------------


class AnswerRunCreate(BaseModel):
    """Payload to insert one row into silver.answer_runs.

    All nullable fields default to None and are filled progressively as the
    query runs (embedding stage → LLM stage → citation stage).  The
    orchestrator inserts the row once the full answer is assembled so a single
    INSERT carries all populated fields.

    workspace_data_version_at_query is mandatory — it is read from
    silver.workspaces.data_version immediately before the cache-key lookup and
    must always be present.  project_data_version_at_query is nullable because
    some queries are workspace-scoped (no single active project).
    """

    workspace_id: UUID = Field(..., description="FK → silver.workspaces.workspace_id")
    project_id: UUID | None = Field(
        default=None,
        description="FK → silver.projects.project_id; NULL for cross-project queries",
    )
    user_id: int | None = Field(
        default=None,
        description="FK → public.users.id (BIGINT); NULL for anonymous or system-initiated queries",
    )
    query_text: str = Field(..., min_length=1, description="Raw natural-language query text")
    query_class: QueryClassLiteral = Field(..., description="Spec query class per §04h taxonomy")

    # Retrieval metadata
    embedding_model: str | None = Field(default=None, max_length=128)
    embedding_model_version: str | None = Field(default=None, max_length=64)
    sparse_model: str | None = Field(default=None, max_length=128)
    sparse_model_version: str | None = Field(default=None, max_length=64)
    fusion_method: FusionMethodLiteral | None = Field(default=None)
    sparse_boost_applied: bool | None = Field(default=None)
    reranker_version: str | None = Field(default=None, max_length=64)
    retrieval_strategy_version: str | None = Field(default=None, max_length=32)

    # Freshness at query time (addendum §05d)
    workspace_data_version_at_query: int = Field(
        ...,
        ge=0,
        description="silver.workspaces.data_version read immediately before cache-key build",
    )
    project_data_version_at_query: int | None = Field(
        default=None,
        ge=0,
        description="silver.projects.data_version; NULL when query is not project-scoped",
    )

    # LLM backend
    backend_used: BackendLiteral | None = Field(default=None)
    backend_chain: list[str] | None = Field(
        default=None,
        description="Ordered list of backends attempted; last element is the one that responded",
    )
    model_name: str | None = Field(default=None, max_length=128)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_creation_tokens: int | None = Field(default=None, ge=0)
    speculative_acceptance_rate_sample: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Sampled speculative-decoding acceptance rate for vLLM backend",
    )
    evidence_truncated_count: int | None = Field(
        default=None,
        ge=0,
        description="Number of retrieval candidates dropped due to context-window budget",
    )

    # Citation lifecycle
    citation_lifecycle_state: CitationLifecycleStateLiteral | None = Field(default=None)
    citation_mode: CitationModeLiteral | None = Field(default=None)

    # OTel (addendum §07f)
    trace_id: str | None = Field(default=None, max_length=64)
    root_span_id: str | None = Field(default=None, max_length=32)

    # Parallel fan-out failure tracking (B4)
    partial_failure_details: dict[str, str] | None = Field(
        default=None,
        description=(
            "JSONB dict of {store: exception_class} for any store that failed "
            "during parallel fan-out. NULL means all stores responded successfully. "
            "Example: {\"qdrant\": \"TimeoutError\", \"neo4j\": \"ServiceUnavailable\"}"
        ),
    )

    # Cache-scope fix — Phase B addendum (2026-04-21, migration batch 18)
    # When this run reused a cached retrieval context from a previous run,
    # cache_hit_of_run_id points at the original run whose retrieval was cached.
    # NULL means retrieval ran fresh (cache miss or cache unavailable).
    # Enables audit queries like "which runs reused which retrieval?"
    cache_hit_of_run_id: UUID | None = Field(
        default=None,
        description=(
            "FK → silver.answer_runs.answer_run_id. "
            "Set when this run reused a CachedRetrievalContext from a previous run. "
            "NULL on cache miss."
        ),
    )

    # RetrievalInspector follow-up — surface composite confidence and
    # wall-clock latency on every persisted run. Both columns landed in
    # migration 2026_05_25_200000. NULL on refusal/legacy rows.
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Composite §04i hallucination-prevention confidence on the "
            "assembled answer (0.0-1.0). Mirrors GeoRAGResponse.confidence. "
            "NULL on rows written before the answer was assembled."
        ),
    )
    latency_ms: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Wall-clock duration of run_deterministic_rag in milliseconds. "
            "Measured via time.monotonic() at function entry."
        ),
    )

    # Refusal-row support — populated by insert_refusal_answer_run when the
    # orchestrator returns early before assembling a real answer (LLM health
    # probe failure, out-of-scope classifier). Mirrors the existing column
    # silver.answer_runs.rejection_reason (migration 2026_04_22_100000).
    rejection_reason: str | None = Field(
        default=None,
        description=(
            "Human-readable reason for refusal. Set on early-refusal rows so "
            "the Retrieval Inspector can render why the run never produced an "
            "answer."
        ),
    )


class AnswerRunRead(AnswerRunCreate):
    """Full silver.answer_runs record as returned from the database."""

    answer_run_id: UUID
    created_at: datetime
    updated_at: datetime


class AnswerRunUpdate(BaseModel):
    """Partial update payload for silver.answer_runs.

    Used by the orchestrator to patch a row after the LLM completes
    (token counts, backend_used, citation_lifecycle_state).  Only fields
    that are not None are applied.
    """

    backend_used: BackendLiteral | None = None
    backend_chain: list[str] | None = None
    model_name: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    speculative_acceptance_rate_sample: float | None = None
    evidence_truncated_count: int | None = None
    citation_lifecycle_state: CitationLifecycleStateLiteral | None = None
    citation_mode: CitationModeLiteral | None = None
    reranker_version: str | None = None
    retrieval_strategy_version: str | None = None
    fusion_method: FusionMethodLiteral | None = None
    sparse_boost_applied: bool | None = None
    confidence: float | None = None
    latency_ms: int | None = None


# ---------------------------------------------------------------------------
# AnswerRetrievalItem
# ---------------------------------------------------------------------------


class AnswerRetrievalItemCreate(BaseModel):
    """Payload to insert one row into silver.answer_retrieval_items.

    One row per retrieval candidate per stage per answer run.  The orchestrator
    emits rows at each stage boundary:
      - After Qdrant / PostGIS / Neo4j returns candidates: stage='retrieved'
      - After cross-encoder rerank: stage='reranked' for survivors
      - After context packing: stage='in_context' for included chunks
      - After LLM synthesizes and cites: stage='cited' for referenced chunks

    At least one of (document_revision_id, passage_id, candidate_ref) should
    be populated.  candidate_ref carries the opaque provenance for structured
    candidates (PostGIS rows, Neo4j edges, map features).
    """

    answer_run_id: UUID = Field(..., description="FK → silver.answer_runs.answer_run_id")
    workspace_id: UUID = Field(..., description="FK → silver.workspaces.workspace_id")
    stage: StageLiteral
    source_store: SourceStoreLiteral

    # What the candidate points at (mutually optional; at least one populated)
    document_revision_id: UUID | None = Field(
        default=None,
        description="FK → silver.document_revisions.document_revision_id; SET NULL on delete",
    )
    passage_id: UUID | None = Field(
        default=None,
        description="FK → silver.document_passages.passage_id; SET NULL on delete",
    )
    candidate_ref: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Opaque provenance for non-passage candidates. "
            "Example: {\"schema\": \"silver\", \"table\": \"collars\", \"pk\": {\"collar_id\": \"...\"}}"
        ),
    )

    # Scores
    retriever_score: float | None = Field(
        default=None,
        description="Raw score from the retriever (cosine similarity, BM25, etc.)",
    )
    reranker_score: float | None = Field(
        default=None,
        description="Cross-encoder reranker logit score; NULL before rerank stage",
    )
    rrf_rank: int | None = Field(
        default=None,
        ge=1,
        description="Reciprocal rank fusion rank position (1-based); NULL if RRF not applied",
    )
    rrf_score: float | None = Field(
        default=None,
        description="RRF score = sum(1/(k + rank_i)) across stores; NULL if RRF not applied",
    )

    # Inclusion flags
    included_in_context: bool = Field(
        default=False,
        description="True if this candidate was packed into the LLM prompt context window",
    )
    used_in_citation: bool = Field(
        default=False,
        description="True if this candidate was referenced in a citation in the final answer",
    )


class AnswerRetrievalItemRead(AnswerRetrievalItemCreate):
    """Full silver.answer_retrieval_items record as returned from the database."""

    retrieval_item_id: UUID
    created_at: datetime


# ---------------------------------------------------------------------------
# Module 6 Phase B Chunk 1 — Citation lifecycle types
# ---------------------------------------------------------------------------

# Re-export the literals as module-level names so callers can use either
# the original Literal definition (CitationLifecycleStateLiteral) or the
# shorter alias (CitationLifecycleState / CitationMode) per spec §09b.
CitationLifecycleState = CitationLifecycleStateLiteral
CitationMode = CitationModeLiteral


# ---------------------------------------------------------------------------
# AnswerCitationItem
# ---------------------------------------------------------------------------


class AnswerCitationItemCreate(BaseModel):
    """Payload to insert one row into silver.answer_citation_items.

    One row per unique citation-marker per answer run.  Binds a marker string
    (e.g. [DATA-1] or [ev:a1b2c3d4]) to the evidence_id / passage_id that
    backs the claim.

    Chunk 1 accepts both legacy [DATA-N]/[NI43-N]/[PUB-N]/[PGEO-N] markers and
    the future [ev:<uuid-short>] format.  Chunk 2 migrates the pipeline to
    evidence-id markers.

    At least one of (evidence_id, passage_id) must be non-None.  The
    @model_validator mirrors the DB CHECK constraint so malformed payloads are
    rejected before they hit the database (resolves SCHEMA-03 for this model).
    """

    answer_run_id: UUID = Field(..., description="FK → silver.answer_runs.answer_run_id")
    workspace_id: UUID = Field(..., description="FK → silver.workspaces.workspace_id")

    # Target of the citation — at least one must be non-None.
    evidence_id: UUID | None = Field(
        default=None,
        description=(
            "FK → silver.evidence_items.evidence_id (SET NULL on delete). "
            "Canonical target once evidence write path is active (B8.5+)."
        ),
    )
    passage_id: UUID | None = Field(
        default=None,
        description=(
            "FK → silver.document_passages.passage_id (SET NULL on delete). "
            "Populated during Chunk 2 dual-support window for legacy passage citations."
        ),
    )

    # Marker as emitted in the answer text.
    marker_text: str = Field(
        ...,
        max_length=64,
        description=(
            "Citation marker as it appears in the answer text. "
            "Chunk 1 accepts [DATA-N], [NI43-N], [PUB-N], [PGEO-N], [ev:<uuid-short>]."
        ),
    )

    # Source-store hint mirrors answer_retrieval_items.source_store.
    source_store: SourceStoreLiteral | None = Field(
        default=None,
        description=(
            "Which retrieval store backed this citation. "
            "Nullable because legacy markers may predate populated evidence_id."
        ),
    )

    # Per-citation confidence score emitted by the span resolver (Chunk 2).
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Per-citation confidence score [0, 1]; None until Chunk 2 span resolver populates it.",
    )

    # Rejection reason set by Chunk 3 guards when a marker fails validation.
    rejection_reason: str | None = Field(
        default=None,
        max_length=128,
        description="Guard failure reason; None for accepted citations.",
    )

    @model_validator(mode="after")
    def has_target(self) -> AnswerCitationItemCreate:
        """Enforce that at least one of (evidence_id, passage_id) is non-None.

        Mirrors the DB CHECK constraint answer_citation_items_has_target so
        malformed payloads are rejected at the Pydantic layer before reaching
        the database.  Resolves SCHEMA-03 for this model.
        """
        if self.evidence_id is None and self.passage_id is None:
            raise ValueError(
                "AnswerCitationItem requires at least one of evidence_id or passage_id"
            )
        return self


class AnswerCitationItemRead(AnswerCitationItemCreate):
    """Full silver.answer_citation_items record as returned from the database."""

    answer_citation_item_id: UUID
    created_at: datetime


# ---------------------------------------------------------------------------
# AnswerCitationSpan
# ---------------------------------------------------------------------------


class AnswerCitationSpanCreate(BaseModel):
    """Payload to insert one row into silver.answer_citation_spans.

    One row per citation-marker occurrence (by character offset) within the
    final answer text.  Chunk 2 span resolver writes these rows by walking
    the LLM output for each resolved marker.

    span_start and span_end are 0-based character offsets (exclusive end)
    within the answer text string.  The @model_validator mirrors the DB CHECK
    constraint so invalid ranges are rejected before hitting the database.
    """

    answer_run_id: UUID = Field(..., description="FK → silver.answer_runs.answer_run_id")
    answer_citation_item_id: UUID = Field(
        ...,
        description="FK → silver.answer_citation_items.answer_citation_item_id",
    )
    workspace_id: UUID = Field(..., description="FK → silver.workspaces.workspace_id")

    # Character offsets (0-based, exclusive end) within the final answer text.
    span_start: int = Field(..., ge=0, description="Inclusive start character offset (0-based).")
    span_end: int = Field(..., ge=1, description="Exclusive end character offset; must be > span_start.")

    @model_validator(mode="after")
    def range_valid(self) -> AnswerCitationSpanCreate:
        """Enforce span_end > span_start.

        Mirrors the DB CHECK constraint answer_citation_spans_range_valid.
        """
        if self.span_end <= self.span_start:
            raise ValueError(
                f"span_end ({self.span_end}) must be strictly greater than span_start ({self.span_start})"
            )
        return self


class AnswerCitationSpanRead(AnswerCitationSpanCreate):
    """Full silver.answer_citation_spans record as returned from the database."""

    answer_citation_span_id: UUID
    created_at: datetime
