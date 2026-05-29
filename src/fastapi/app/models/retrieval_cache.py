"""CachedRetrievalContext — the only shape ever written to the Redis retrieval cache.

Per arch §05c and the Global Invariant: no answer-level caching in V1.
Only the retrieval context (candidates after RRF + reranking) is cached.
Synthesis always runs fresh on every query.

Schema version: 1. Bump `schema_version` and the cache key prefix (v5 →
v6, etc.) whenever the shape changes incompatibly. Old entries TTL out
naturally — do not flush Redis manually.

Cache key prefix: georag:rag_cache:v5:<sha256_first16>
(bumped from v4 on 2026-04-21 to disambiguate from old answer-level entries)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class CachedRetrievalCandidate(BaseModel):
    """One retrieval candidate as stored in Redis.

    Carries everything synthesis needs to build an answer and citations
    from a cache hit:
    - which store it came from (for provenance labelling)
    - the raw chunk text (fed to the LLM context)
    - scoring metadata (reranker score, RRF rank/score) so hallucination
      layers 1 and 5 can verify quality thresholds at synthesis time
    - optional UUID pointers so answer_retrieval_items rows can be written
      without a second retrieval pass

    Fields intentionally excluded: synthesized_answer, citation spans,
    LLM token counts, citation_lifecycle_state. Those are computed per-query.
    """

    source_store: str = Field(
        ...,
        description="Origin store: 'qdrant' | 'neo4j' | 'postgis' | 'hybrid'",
    )
    document_revision_id: UUID | None = Field(
        default=None,
        description="FK → silver.document_revisions; populated for Qdrant candidates",
    )
    passage_id: UUID | None = Field(
        default=None,
        description="FK → silver.document_passages; populated for Qdrant passage-level candidates",
    )
    candidate_ref: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Opaque provenance for non-passage candidates "
            "(PostGIS rows, Neo4j edges, map features). "
            "Example: {\"store\": \"postgis\", \"canonical_id\": \"collar:abc123\"}"
        ),
    )
    text: str = Field(
        ...,
        min_length=1,
        description="Chunk or record text as consumed by the LLM synthesis prompt",
    )
    retriever_score: float | None = Field(
        default=None,
        description="Raw score from the retriever (cosine similarity, BM25, etc.)",
    )
    reranker_score: float | None = Field(
        default=None,
        description="Cross-encoder reranker score; None if reranker failed/skipped",
    )
    rrf_rank: int | None = Field(
        default=None,
        ge=1,
        description="RRF rank position (1-based)",
    )
    rrf_score: float | None = Field(
        default=None,
        description="RRF score = sum(1/(k + rank_i)) across stores",
    )
    payload: dict[str, Any] | None = Field(
        default=None,
        description="Full Qdrant point payload if available (includes chunk_id, document_title, etc.)",
    )


class CachedRetrievalContext(BaseModel):
    """Full retrieval context stored in Redis after a cache miss.

    Contains everything a fresh synthesis call needs:
    - versioning metadata (schema_version, strategy versions) so stale
      entries from a different model version are detectable
    - data_version fingerprints so the orchestrator can cross-check that
      the cached retrieval is still valid for the current workspace state
    - the final top-N candidates after RRF fusion and cross-encoder reranking
    - partial failure details so synthesis can surface degraded_sources

    What is NOT stored here:
    - synthesized answer text
    - citation spans or citation_id assignments
    - LLM token counts or backend_used
    - citation_lifecycle_state
    All of the above are computed fresh per query.

    schema_version must be bumped whenever the shape changes in a way that
    would cause model_validate_json to fail on an old entry. Old entries
    TTL out within 5 minutes — no flush needed.
    """

    schema_version: int = Field(
        default=1,
        description="Bump if shape changes incompatibly. Old entries TTL out naturally.",
    )
    cached_at: datetime = Field(
        ...,
        description="UTC timestamp when this context was written to Redis",
    )
    workspace_id: UUID = Field(
        ...,
        description="Workspace that owns this retrieval context",
    )
    project_id: UUID | None = Field(
        default=None,
        description="Project scope; None for cross-project workspace queries",
    )
    workspace_data_version_at_cache: int = Field(
        ...,
        ge=0,
        description=(
            "silver.workspaces.data_version at the moment this context was cached. "
            "Informational — the data_version is already baked into the cache key, "
            "so a version bump produces a new key (cache miss) automatically. "
            "Stored here for audit and debugging."
        ),
    )
    project_data_version_at_cache: int | None = Field(
        default=None,
        ge=0,
        description="silver.projects.data_version at cache time; None for cross-project queries",
    )

    # Query classification metadata
    query_class: str = Field(
        ...,
        min_length=1,
        description="Spec query class resolved at retrieval time (e.g. 'spatial', 'document')",
    )

    # Retrieval strategy fingerprint — lets synthesis verify this context
    # was produced by the same retrieval pipeline as the current request.
    sparse_boost_applied: bool = Field(
        ...,
        description="True if identifier-boost detection widened the sparse prefetch limit",
    )
    fusion_method: str = Field(
        default="rrf",
        description="Fusion algorithm applied: always 'rrf' in V1",
    )
    retrieval_strategy_version: str = Field(
        ...,
        min_length=1,
        description=(
            "RETRIEVAL_STRATEGY_VERSION constant from query_classifier.py at cache time. "
            "Current value: v2-retrieval-only-cache-2026-04-21"
        ),
    )
    embedding_model_version: str = Field(
        ...,
        min_length=1,
        description="Embedding model identifier used to produce dense vectors for this retrieval",
    )
    sparse_model_version: str = Field(
        ...,
        min_length=1,
        description="Sparse encoder model version (e.g. SPLADE++ SHA) used for sparse prefetch",
    )
    reranker_version: str | None = Field(
        default=None,
        description="Cross-encoder reranker version; None if reranker was skipped or failed",
    )
    reranker_failed: bool = Field(
        default=False,
        description="True if the reranker was attempted but errored; False if skipped intentionally or succeeded",
    )

    # Partial failure details from parallel fan-out
    partial_failure_details: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Which stores failed during retrieval fan-out and why. "
            "Example: {\"neo4j\": \"ServiceUnavailable\", \"qdrant\": \"TimeoutError\"}. "
            "None means all stores returned cleanly."
        ),
    )

    # The actual retrieval output — this is what synthesis consumes.
    candidates_reranked: list[CachedRetrievalCandidate] = Field(
        default_factory=list,
        description=(
            "Final top-N candidates after RRF fusion and cross-encoder reranking, "
            "in descending relevance order. Synthesis feeds these directly to the LLM context."
        ),
    )

    # Back-reference to the answer_run that originated this cached context.
    # Populated after insert_answer_run() returns on the cache-miss path
    # (requires a second Redis SET to update the entry). Used by cache-hit
    # paths to populate answer_runs.cache_hit_of_run_id for audit.
    # None when the originating run's ID was not yet available at cache-write
    # time (e.g., if the cache write happened before the INSERT completed,
    # or on early V1 entries written before this field was added).
    original_answer_run_id: UUID | None = Field(
        default=None,
        description=(
            "answer_run_id of the first run whose retrieval produced this context. "
            "Set via a post-INSERT Redis update on the cache-miss path. "
            "Used to populate cache_hit_of_run_id on subsequent cache-hit runs."
        ),
    )

    # Phase H continued — auxiliary tool results that aren't part of the
    # RRF candidate pool but DO contribute to the synthesis context.
    # ``candidates_reranked`` covers ``query_spatial_collars``,
    # ``search_documents``, ``search_public_geoscience``, and graph
    # traversal results. The tools below feed the LLM context directly
    # without going through cross-store RRF:
    #
    #   - query_project_overview  →  ProjectOverviewResult
    #   - query_downhole_logs     →  DownholeLogsResult
    #   - query_assay_data        →  AssayDataResult
    #   - drill_targeting         →  list[TargetRecommendation]
    #
    # We store them as raw `dataclasses.asdict` dicts keyed by tool
    # name. Phase H rehydration in `orchestrator/run_cache.py` reads
    # these back via the matching dataclass constructor. This is what
    # lifts the partial-source fallback rate from ~40% (any query
    # touching project_overview goes cache-miss) toward near-zero.
    auxiliary_tool_results: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Tool results not in the RRF candidate pool but still "
            "consumed by synthesis. Keyed by tool name; values are "
            "dataclasses.asdict dicts of the original tool-result "
            "dataclass instance. Examples: project_overview, "
            "downhole_logs, assay_data, drill_targeting."
        ),
    )
