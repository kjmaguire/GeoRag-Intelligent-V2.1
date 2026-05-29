"""Track A.2 Phase 1.A — typed sub-query decomposition models.

Per `docs/plans/track-a2-agentic-retrieval.md` D2 (sub-query taxonomy, locked
2026-04-29) and D3 (plan persistence into silver.answer_runs.plan_json JSONB).

This module defines the complete Pydantic v2 contract for the agentic retrieval
planner introduced in A.2 Phase 1.  It is a pure model definition — no
orchestrator wiring, no migration, no FastAPI router.  Phase 1.B wires the
decomposer logic; Phase 1.C integrates into the orchestrator.

Sub-query taxonomy (D2)
-----------------------
Five typed classes with distinct input + output shapes:

    factual_lookup          → Silver-table single-entity point lookups
    entity_traversal        → Neo4j multi-hop graph traversal
    spatial_filter          → PostGIS spatial predicates
    document_passage_search → Qdrant semantic search with citation provenance
    numerical_aggregation   → Compute over Silver-table result sets

Every output model carries source_chunk_id (mandatory, not Optional) per §04i
hallucination prevention Layer 2 (typed output validation).

Plan persistence (D3)
---------------------
DecompositionPlan is the top-level envelope that marshals into the
silver.answer_runs.plan_json JSONB column added by migration
2026_05_11_120000_add_plan_json_to_answer_runs.php.

Use serialize_plan_for_jsonb(plan) to convert to a JSON-safe dict before
storage; Pydantic v2 mode='json' handles UUID→str and datetime→isoformat
automatically.

Forward compatibility
---------------------
All models use extra='forbid'.  schema_version is a forward-compat marker —
bump it whenever a required field is added so downstream readers can detect
the version before attempting deserialization.

Phase 0 validation framework ships separately in
src/fastapi/tests/a2_validation/.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Literal aliases — locked per D1, D2, D4
# ---------------------------------------------------------------------------

SubQueryClass = Literal[
    "factual_lookup",
    "entity_traversal",
    "spatial_filter",
    "document_passage_search",
    "numerical_aggregation",
]
"""Five typed sub-query classes per A.2 D2 (locked 2026-04-29).

Each class maps to a distinct backend:
  factual_lookup          → asyncpg (Silver PostgreSQL tables)
  entity_traversal        → Neo4j async driver
  spatial_filter          → asyncpg / PostGIS
  document_passage_search → async Qdrant client
  numerical_aggregation   → asyncpg (Silver PostgreSQL tables)
"""

DecompositionTrigger = Literal[
    "deterministic_multi_intent",
    "multi_entity_ner",
    "multi_document_signal",
    "continued_empty_escalation",
]
"""Four decomposition trigger conditions per A.2 D1 (locked 2026-04-29).

Decomposition fires only on these explicit signals — single-intent simple
queries continue through the existing §04h linear retrieval path unchanged.

  deterministic_multi_intent   — classifier returned >1 intent (compound query)
  multi_entity_ner             — query parse detected >1 named entity
  multi_document_signal        — cross-document conjunctions detected
                                  ("in reports A and B", "across 2024 and 2025")
  continued_empty_escalation   — single-shot retrieval returned empty after
                                  reranking (existing R9 escalation path)
"""

DecisionPoint = Literal[
    "post_decomposition",
    "post_retrieval",
    "post_binding",
]
"""Three conditional branching decision points per A.2 D4 (locked 2026-04-29).

  post_decomposition — sub-query count cap hit (default N=5); reduce scope or
                       refuse
  post_retrieval     — empty result on a sub-query; try alternative phrasing or
                       mark failed + continue
  post_binding       — cross-sub-query conflict detected (different values for
                       the same fact); surface via §10t precedence rules
"""

# ---------------------------------------------------------------------------
# Silver table literals — shared across FactualLookupInput and
# NumericalAggregationInput.  Must stay in sync with the Silver schema.
# ---------------------------------------------------------------------------

_SilverTable = Literal[
    "collars",
    "lithology_logs",
    "samples",
    "drill_traces",
    "seismic_surveys",
    "mineral_claims",
    "reports",
    "structures",
    "alterations",
    "geochemistry",
]

# ---------------------------------------------------------------------------
# Sub-query INPUT models — one per class, extra='forbid'
# ---------------------------------------------------------------------------


class FactualLookupInput(BaseModel):
    """Input for a factual_lookup sub-query.

    Single-entity, single-fact lookup against a Silver PostgreSQL table.
    Example: "What is the total depth of hole MS-117?"

    table restricts to the 10 wired Silver tables.  Phase 1.B must add new
    tables to the _SilverTable Literal before wiring a new tool for them —
    this constraint is intentional to prevent unbounded schema sprawl.
    """

    table: _SilverTable = Field(
        ...,
        description="Silver table to query; must be one of the 10 wired tables",
    )
    entity_id: str | UUID = Field(
        ...,
        description=(
            "Primary-key or natural-key of the entity to look up.  "
            "Accepts string natural keys (e.g. 'MS-117') or UUID PKs."
        ),
    )
    fields: list[str] = Field(
        ...,
        min_length=1,
        description="Column names to retrieve; at least one required",
    )

    model_config = {"extra": "forbid"}


class EntityTraversalInput(BaseModel):
    """Input for an entity_traversal sub-query.

    Multi-hop graph traversal against Neo4j.
    Example: "What formations does hole MS-117 intersect, and which deposits are
    those formations associated with?"

    edge_kinds is an open list — Neo4j relationship type names (e.g.
    'INTERSECTS', 'ASSOCIATED_WITH').  Empty list means traverse any edge type.

    hop_count is bounded [1, 5] per §05c latency budget: 5 hops against a
    community-edition Neo4j instance will hit the 3s timeout at current corpus
    sizes.
    """

    start_entity: str = Field(
        ...,
        min_length=1,
        description=(
            "Neo4j node identifier for the traversal start point.  "
            "Accepts natural keys (e.g. 'MS-117') resolved via entity_focus or NER."
        ),
    )
    hop_count: int = Field(
        default=1,
        ge=1,
        le=5,
        description="Maximum traversal depth [1, 5]; bounded by §05c Neo4j 3s timeout",
    )
    edge_kinds: list[str] = Field(
        default_factory=list,
        description=(
            "Neo4j relationship type names to follow (e.g. ['INTERSECTS', 'ASSOCIATED_WITH']).  "
            "Empty list = traverse all edge types."
        ),
    )

    model_config = {"extra": "forbid"}


class SpatialFilterInput(BaseModel):
    """Input for a spatial_filter sub-query.

    PostGIS spatial predicate against a Silver table.
    Example: "Show me all collars within 500m of the western boundary."

    Cross-field rules (enforced by model_validator):
      - predicate='near' REQUIRES distance_m to be set
      - predicate != 'near' MUST NOT have distance_m set
    """

    predicate: Literal["within", "intersects", "near", "contains"] = Field(
        ...,
        description="PostGIS spatial predicate to apply",
    )
    target_table: _SilverTable = Field(
        ...,
        description="Silver table to filter spatially",
    )
    geometry_wkt: str = Field(
        ...,
        min_length=1,
        description=(
            "Reference geometry as WKT (SRID assumed WGS84 / EPSG:4326 unless the "
            "WKT string contains an embedded SRID).  "
            "Example: 'POINT(-122.4194 37.7749)', 'POLYGON((...))'"
        ),
    )
    distance_m: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Search radius in metres.  "
            "REQUIRED when predicate='near'; must be absent for all other predicates."
        ),
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _validate_distance_m(self) -> SpatialFilterInput:
        """Enforce the near/distance_m cross-field rule.

        predicate='near'   → distance_m MUST be set (positive float)
        predicate!='near'  → distance_m MUST be None
        """
        if self.predicate == "near" and self.distance_m is None:
            raise ValueError("distance_m is required when predicate='near'")
        if self.predicate != "near" and self.distance_m is not None:
            raise ValueError(
                f"distance_m must be None when predicate='{self.predicate}' "
                "(only valid for predicate='near')"
            )
        return self


class DocumentPassageSearchInput(BaseModel):
    """Input for a document_passage_search sub-query.

    Qdrant semantic search bounded by citation provenance per §04i.
    Example: "What does the 2024 NI 43-101 say about the resource estimate?"

    min_relevance is the post-rerank relevance gate (§04i Layer 1: Retrieval
    quality gate).  Passages scoring below this threshold are dropped before
    synthesis.

    document_filter is an optional Qdrant filter payload (dict) to scope the
    search to specific documents (e.g. by document_type='NI43-101').
    """

    query_text: str = Field(
        ...,
        min_length=1,
        description="Natural-language query text for embedding + semantic search",
    )
    top_k: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of passages to retrieve from Qdrant",
    )
    min_relevance: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum cross-encoder reranker score [0.0, 1.0] per §04i Layer 1 "
            "retrieval quality gate.  Passages below this score are dropped."
        ),
    )
    document_filter: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional Qdrant filter payload to scope the search.  "
            "Example: {'must': [{'key': 'document_type', 'match': {'value': 'NI43-101'}}]}"
        ),
    )

    model_config = {"extra": "forbid"}


class NumericalAggregationInput(BaseModel):
    """Input for a numerical_aggregation sub-query.

    SQL aggregation over Silver-table results.
    Example: "What is the average gold grade across the assays in the western block?"

    filter_expr is an optional structured filter (column→value dict or nested
    AND/OR structure) that the Phase 1.B tool translates into a parameterised
    WHERE clause.  Free-text SQL is NOT accepted here — that would reintroduce
    the injection surface that parameterised queries eliminate.
    """

    operation: Literal["count", "sum", "avg", "min", "max", "stddev"] = Field(
        ...,
        description="SQL aggregate function to compute",
    )
    target_table: _SilverTable = Field(
        ...,
        description="Silver table to aggregate over",
    )
    target_column: str = Field(
        ...,
        min_length=1,
        description=(
            "Column name to aggregate.  Must be a valid column in target_table; "
            "Phase 1.B tool validates against the Silver schema at execution time."
        ),
    )
    group_by: list[str] = Field(
        default_factory=list,
        description="Column names to GROUP BY; empty list = no grouping (scalar result)",
    )
    filter_expr: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Structured WHERE predicate as a nested dict.  "
            "Translated to parameterised SQL by the Phase 1.B execution tool.  "
            "Free-text SQL is rejected — use structured filter only."
        ),
    )

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Sub-query OUTPUT models — one per class, source_chunk_id MANDATORY per §04i
# ---------------------------------------------------------------------------


class FactualLookupOutput(BaseModel):
    """Output of a factual_lookup sub-query.

    source_chunk_id is mandatory (not Optional) per §04i Layer 2 typed output
    validation.  For Silver-table results this is a synthetic identifier of the
    form 'silver:{table}:{pk}' so the claim is traceable to a specific row.
    """

    value: Any = Field(
        ...,
        description=(
            "The looked-up field value.  Type is Any because Silver columns span "
            "scalars (str, int, float), nullable types, and JSON arrays."
        ),
    )
    passage_id: UUID | None = Field(
        default=None,
        description=(
            "FK → silver.document_passages.passage_id when the source row is "
            "backed by a parsed passage.  None for structured Silver rows without "
            "a corresponding passage."
        ),
    )
    source_chunk_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Mandatory provenance identifier per §04i Layer 2.  "
            "Format: 'silver:{table}:{pk}' for Silver-table results.  "
            "Never empty — a missing source_chunk_id is a hallucination prevention failure."
        ),
    )
    retrieved_at: datetime = Field(
        ...,
        description="UTC timestamp when the result was fetched from the database",
    )

    model_config = {"extra": "forbid"}


class NodeRef(BaseModel):
    """A single node in a Neo4j traversal result path."""

    neo4j_id: str = Field(
        ...,
        description="Neo4j internal node element ID (string form for forward compat with Neo4j 5+)",
    )
    label: str = Field(
        ...,
        description="Primary Neo4j node label (e.g. 'DrillHole', 'Formation', 'Deposit')",
    )
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Subset of node properties returned by the traversal query",
    )

    model_config = {"extra": "forbid"}


class EdgeRef(BaseModel):
    """A single edge in a Neo4j traversal result path."""

    neo4j_id: str = Field(
        ...,
        description="Neo4j internal relationship element ID (string form)",
    )
    label: str = Field(
        ...,
        description="Neo4j relationship type label (e.g. 'INTERSECTS', 'ASSOCIATED_WITH')",
    )
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Subset of relationship properties returned by the traversal query",
    )

    model_config = {"extra": "forbid"}


class GraphPath(BaseModel):
    """A single traversal path from a Neo4j entity_traversal result.

    Alternating sequence: node → edge → node → edge → ... → node.
    nodes[i] is connected to nodes[i+1] via edges[i].
    len(nodes) == len(edges) + 1 invariant is NOT enforced here to avoid
    breaking empty-path results; Phase 1.B tool enforces it at query time.
    """

    nodes: list[NodeRef] = Field(
        ...,
        description="Ordered list of nodes along the path (start → end)",
    )
    edges: list[EdgeRef] = Field(
        ...,
        description="Ordered list of edges along the path; len(edges) == len(nodes) - 1",
    )

    model_config = {"extra": "forbid"}


class EntityTraversalOutput(BaseModel):
    """Output of an entity_traversal sub-query.

    source_chunk_id is mandatory per §04i Layer 2.  For Neo4j results this is
    a synthetic identifier of the form 'neo4j:{start_entity}:{hop_count}:
    {edge_kind_hash}' constructed by the Phase 1.B execution tool.
    """

    paths: list[GraphPath] = Field(
        ...,
        description=(
            "Traversal paths from the start entity.  Empty list when the "
            "entity exists but has no matching relationships within hop_count."
        ),
    )
    passage_id: UUID | None = Field(
        default=None,
        description=(
            "FK → silver.document_passages.passage_id when the graph result "
            "is backed by a passage (e.g. a mention-edge from a NI 43-101).  "
            "None for purely structural graph relationships."
        ),
    )
    source_chunk_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Mandatory provenance identifier per §04i Layer 2.  "
            "Format: 'neo4j:{start_entity}:{hop_count}'."
        ),
    )

    model_config = {"extra": "forbid"}


class SpatialFilterOutput(BaseModel):
    """Output of a spatial_filter sub-query.

    matching_rows contains the raw Silver-table rows matching the spatial
    predicate.  source_chunk_id is mandatory per §04i Layer 2.
    """

    matching_rows: list[dict[str, Any]] = Field(
        ...,
        description=(
            "Silver-table rows satisfying the spatial predicate.  "
            "Each dict is a column→value map for the requested fields."
        ),
    )
    result_count: int = Field(
        ...,
        ge=0,
        description="Total row count (len(matching_rows) unless server-side pagination applied)",
    )
    source_chunk_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Mandatory provenance identifier per §04i Layer 2.  "
            "Format: 'postgis:{target_table}:{predicate}:{wkt_hash}'."
        ),
    )

    model_config = {"extra": "forbid"}


class PassageHit(BaseModel):
    """A single passage returned from a document_passage_search sub-query."""

    passage_id: UUID = Field(
        ...,
        description="FK → silver.document_passages.passage_id",
    )
    text: str = Field(
        ...,
        description="Passage text as stored in Qdrant / Silver",
    )
    relevance: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Cross-encoder reranker score [0.0, 1.0] after reranking",
    )
    document_id: str = Field(
        ...,
        description="Parent document identifier (silver.document_revisions PK or slug)",
    )
    page_number: int | None = Field(
        default=None,
        ge=1,
        description="1-indexed page number in the source PDF; None for non-PDF sources",
    )

    model_config = {"extra": "forbid"}


class DocumentPassageSearchOutput(BaseModel):
    """Output of a document_passage_search sub-query.

    passages are sorted by descending relevance after cross-encoder reranking.
    source_chunk_id is mandatory per §04i Layer 2.

    For document_passage_search, source_chunk_id is the Qdrant vector ID of the
    top-scored passage (the primary evidence anchor for this sub-query result).
    """

    passages: list[PassageHit] = Field(
        ...,
        description="Ranked passage hits, sorted by descending relevance score",
    )
    source_chunk_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Mandatory provenance identifier per §04i Layer 2.  "
            "Set to the Qdrant vector ID of the top-ranked passage.  "
            "Never empty — no passages means the sub-query result is empty."
        ),
    )

    model_config = {"extra": "forbid"}


class NumericalAggregationOutput(BaseModel):
    """Output of a numerical_aggregation sub-query.

    result is either a scalar (float | int) for ungrouped aggregations, or a
    dict of {group_key → aggregate_value} for GROUP BY results.

    computation_sql carries the parameterised SQL template (with $1 placeholders
    for asyncpg, NOT with values substituted in) per §04i Layer 3 Numeric
    grounding — the audit trail must show what query was executed, not just
    the result.

    source_chunk_id is mandatory per §04i Layer 2.
    """

    result: float | int | dict[str, Any] = Field(
        ...,
        description=(
            "Aggregate result.  Scalar for ungrouped aggregations; "
            "dict of {group_key: value} for GROUP BY queries."
        ),
    )
    source_chunk_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Mandatory provenance identifier per §04i Layer 2.  "
            "Format: 'silver:{target_table}:{operation}:{column_hash}'."
        ),
    )
    computation_sql: str = Field(
        ...,
        min_length=1,
        description=(
            "Parameterised SQL template used to produce the result (asyncpg $1 style).  "
            "Values are NOT substituted in — this is the template only.  "
            "Required for §04i Layer 3 Numeric grounding audit trail."
        ),
    )

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# SubQuery discriminated union envelope
# ---------------------------------------------------------------------------

# Typed input union — discriminated on sub_query_class
_FactualLookupSubQuery = Annotated[
    "SubQueryFactualLookup",
    Field(discriminator="sub_query_class"),
]


class SubQueryFactualLookup(BaseModel):
    """Discriminated SubQuery envelope for class='factual_lookup'."""

    id: str = Field(
        ...,
        min_length=1,
        description="Planner-assigned identifier for this sub-query within the plan (e.g. 'sq-1')",
    )
    sub_query_class: Literal["factual_lookup"]
    input: FactualLookupInput
    latency_budget_s: float = Field(
        ...,
        gt=0.0,
        description="Maximum wall-clock seconds budgeted for this sub-query per §05c",
    )
    outcome: Literal["pending", "ok", "empty", "error", "timeout"] = Field(
        default="pending",
        description="Execution outcome set by the Phase 1.C orchestrator after the tool call returns",
    )
    result: FactualLookupOutput | None = Field(
        default=None,
        description="Typed output; None until the sub-query completes successfully",
    )
    error_message: str | None = Field(
        default=None,
        description="Error detail when outcome='error' or outcome='timeout'",
    )
    started_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when execution began",
    )
    completed_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when execution completed (success or failure)",
    )

    model_config = {"extra": "forbid"}


class SubQueryEntityTraversal(BaseModel):
    """Discriminated SubQuery envelope for class='entity_traversal'."""

    id: str = Field(..., min_length=1)
    sub_query_class: Literal["entity_traversal"]
    input: EntityTraversalInput
    latency_budget_s: float = Field(..., gt=0.0)
    outcome: Literal["pending", "ok", "empty", "error", "timeout"] = Field(default="pending")
    result: EntityTraversalOutput | None = Field(default=None)
    error_message: str | None = Field(default=None)
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)

    model_config = {"extra": "forbid"}


class SubQuerySpatialFilter(BaseModel):
    """Discriminated SubQuery envelope for class='spatial_filter'."""

    id: str = Field(..., min_length=1)
    sub_query_class: Literal["spatial_filter"]
    input: SpatialFilterInput
    latency_budget_s: float = Field(..., gt=0.0)
    outcome: Literal["pending", "ok", "empty", "error", "timeout"] = Field(default="pending")
    result: SpatialFilterOutput | None = Field(default=None)
    error_message: str | None = Field(default=None)
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)

    model_config = {"extra": "forbid"}


class SubQueryDocumentPassageSearch(BaseModel):
    """Discriminated SubQuery envelope for class='document_passage_search'."""

    id: str = Field(..., min_length=1)
    sub_query_class: Literal["document_passage_search"]
    input: DocumentPassageSearchInput
    latency_budget_s: float = Field(..., gt=0.0)
    outcome: Literal["pending", "ok", "empty", "error", "timeout"] = Field(default="pending")
    result: DocumentPassageSearchOutput | None = Field(default=None)
    error_message: str | None = Field(default=None)
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)

    model_config = {"extra": "forbid"}


class SubQueryNumericalAggregation(BaseModel):
    """Discriminated SubQuery envelope for class='numerical_aggregation'."""

    id: str = Field(..., min_length=1)
    sub_query_class: Literal["numerical_aggregation"]
    input: NumericalAggregationInput
    latency_budget_s: float = Field(..., gt=0.0)
    outcome: Literal["pending", "ok", "empty", "error", "timeout"] = Field(default="pending")
    result: NumericalAggregationOutput | None = Field(default=None)
    error_message: str | None = Field(default=None)
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)

    model_config = {"extra": "forbid"}


# The discriminated union — Pydantic v2 selects the right variant based on the
# literal value of sub_query_class.  All five variants are registered here.
SubQuery = Annotated[
    SubQueryFactualLookup
    | SubQueryEntityTraversal
    | SubQuerySpatialFilter
    | SubQueryDocumentPassageSearch
    | SubQueryNumericalAggregation,
    Field(discriminator="sub_query_class"),
]
"""Discriminated union of all five sub-query classes.

Pydantic v2 dispatches to the correct typed variant (input + output shapes)
based on the literal value of sub_query_class.  Use this type for
list[SubQuery] fields in DecompositionPlan.
"""

# Registry used by the exhaustiveness test and Phase 1.B tool lookup.
# Keys are SubQueryClass literal values; values are (InputModel, OutputModel).
_SUB_QUERY_IO_REGISTRY: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "factual_lookup": (FactualLookupInput, FactualLookupOutput),
    "entity_traversal": (EntityTraversalInput, EntityTraversalOutput),
    "spatial_filter": (SpatialFilterInput, SpatialFilterOutput),
    "document_passage_search": (DocumentPassageSearchInput, DocumentPassageSearchOutput),
    "numerical_aggregation": (NumericalAggregationInput, NumericalAggregationOutput),
}

# ---------------------------------------------------------------------------
# Decision model (D4 conditional branching)
# ---------------------------------------------------------------------------


class Decision(BaseModel):
    """A single conditional branching decision recorded during plan execution.

    Per A.2 D4 (locked 2026-04-29), three decision points may produce Decision
    records: post_decomposition, post_retrieval, and post_binding.

    branch_taken is free-text describing which branch the planner took.
    Recommended values (not enforced):
      "scope_reduced"                  — post_decomposition, fan-out capped
      "refused"                        — post_decomposition, hard refuse
      "alternative_phrasing_attempted" — post_retrieval, re-phrased sub-query
      "marked_failed_continued"        — post_retrieval, skipped empty sub-query
      "surfaced_via_section_10t"       — post_binding, conflict surfaced to user
    """

    point: DecisionPoint = Field(
        ...,
        description="Which decision point triggered this branch",
    )
    branch_taken: str = Field(
        ...,
        min_length=1,
        description=(
            "Free-text description of the branch the planner chose.  "
            "Used for audit + debug replay; not enum to allow evolving branch names."
        ),
    )
    rationale: str | None = Field(
        default=None,
        description=(
            "Optional human-readable explanation of why this branch was taken.  "
            "Populated by the planner when the branch is non-obvious."
        ),
    )
    decided_at: datetime = Field(
        ...,
        description="UTC timestamp when the decision was recorded",
    )

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Phase 5 spatial / temporal verification models (A.2 D7 minimal V1 surface)
# ---------------------------------------------------------------------------


class ClaimSpatialVerification(BaseModel):
    """Spatial verification record for a single generated claim.

    Per A.2 Phase 5 / D7 (minimal V1 surface): checks that cited spatial
    relationships are consistent with PostGIS geometry for the source chunk.

    Lifecycle:
      1. Phase 4 guard_status='passed' claims with spatial signals are checked.
      2. verify_spatial_claim() computes ST_Distance against spatial_focus.
      3. status='inconsistent' → revise path if budget remains, else refuse.

    status='indeterminate' is NOT a refusal trigger — it means the geometry
    data was not available to confirm or contradict the claim.

    No SQL migration needed: slots into silver.answer_runs.plan_json JSONB.
    """

    claim_text: str = Field(
        ...,
        min_length=1,
        description="Verbatim claim text that contained a spatial signal",
    )
    status: Literal["consistent", "inconsistent", "indeterminate"] = Field(
        ...,
        description=(
            "Verification outcome.  "
            "'consistent' — geometry supports the claim.  "
            "'inconsistent' — geometry contradicts the claim (hard refusal signal).  "
            "'indeterminate' — insufficient data to verify; NOT a refusal trigger."
        ),
    )
    distance_m: float | None = Field(
        default=None,
        description=(
            "Computed ST_Distance in metres between the source chunk's geometry "
            "and the conversation's spatial_focus centroid.  "
            "None when geometry was unavailable."
        ),
    )
    focus_summary: str = Field(
        ...,
        description=(
            "Human-readable summary of the spatial_focus used for verification "
            "(e.g. 'bbox(-122.5,37.7,-122.4,37.8)' or 'point(37.77,-122.42) r=500m').  "
            "Stored for audit trail and debug replay."
        ),
    )

    model_config = {"extra": "forbid"}


class ClaimTemporalVerification(BaseModel):
    """Temporal verification record for a single generated claim.

    Per A.2 Phase 5 / D7 (minimal V1 surface): checks that cited temporal
    assertions are consistent with the source document's date fields
    (published_at / effective_date / report_date from §04e schema).

    status='indeterminate' is NOT a refusal trigger.

    No SQL migration needed: slots into silver.answer_runs.plan_json JSONB.
    """

    claim_text: str = Field(
        ...,
        min_length=1,
        description="Verbatim claim text that contained a temporal signal",
    )
    status: Literal["consistent", "inconsistent", "indeterminate"] = Field(
        ...,
        description=(
            "Verification outcome.  "
            "'consistent' — document date supports the claim's timeframe.  "
            "'inconsistent' — document date contradicts the claim.  "
            "'indeterminate' — document date unavailable or no temporal signal found."
        ),
    )
    document_date: str | None = Field(
        default=None,
        description=(
            "ISO-8601 date (YYYY-MM-DD) resolved from the source document's "
            "published_at / effective_date / report_date fields.  "
            "None when the date could not be resolved."
        ),
    )
    focus_summary: str = Field(
        ...,
        description=(
            "Human-readable summary of the temporal_focus used for verification "
            "(e.g. '[2022-01-01, 2024-01-01]' or 'no temporal focus').  "
            "Stored for audit trail and debug replay."
        ),
    )

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# ClaimVerification model (D5 — bounded 1-revise budget; Phase 4 execution
# territory but shape locked here for forward-compat with plan_json schema)
# ---------------------------------------------------------------------------


class ClaimVerification(BaseModel):
    """Verification record for a single generated claim.

    Per A.2 D5 (locked 2026-04-29): bounded 1-revise budget per claim.

    Lifecycle:
      1. Generation produces claim C → guard_status starts as 'passed' (optimistic)
      2. §04i guards check claim C against bound evidence
      3. If guard fails: re-bind + re-generate ONCE → revise_count becomes 1
      4. If guard fails again: guard_status='refused'; surfaced via §10u path

    passage_ids lists the silver.document_passages UUIDs that were checked
    against the claim text during verification.  Empty list means no passage
    was found that could verify or refute the claim (upstream evidence gap).

    source_chunk_id carries the citation provenance from the binding step so
    Phase 5 spatial/temporal verifiers can resolve geometry and document dates
    from the source chunk.  None when the claim is unbound (revised/refused
    path) or when the citation carried no source_chunk_id (upstream gap).
    Phase 6.A wire-up: _build_claim_verifications in orchestrator.py now
    populates this field from response.citations on every passed citation.
    """

    claim_text: str = Field(
        ...,
        min_length=1,
        description="Verbatim claim text as generated by the LLM",
    )
    guard_status: Literal["passed", "revised", "refused"] = Field(
        ...,
        description=(
            "Outcome of the §04i guard check.  "
            "'passed' — claim survived on first generation.  "
            "'revised' — claim was revised once (revise_count=1) and then passed.  "
            "'refused' — claim failed both generation passes; surfaced via §10u."
        ),
    )
    revise_count: Literal[0, 1] = Field(
        ...,
        description=(
            "Number of revise passes applied: 0 (passed first time) or 1 (one revise).  "
            "Bounded at 1 per D5 to prevent unbounded LLM calling."
        ),
    )
    passage_ids: list[UUID] = Field(
        default_factory=list,
        description=(
            "UUIDs of silver.document_passages checked during verification.  "
            "Empty when no passage-backed evidence was available for this claim."
        ),
    )
    source_chunk_id: str | None = Field(
        default=None,
        description=(
            "Citation provenance from the binding step.  "
            "Set to Citation.source_chunk_id for 'passed' claims; None for "
            "'revised' / 'refused' (unbound) claims.  "
            "Consumed by Phase 5 spatial/temporal verifiers to resolve geometry "
            "and document dates (replaces the old source_chunk_id_hint getattr "
            "workaround in spatial_temporal_verify.py).  "
            "Optional for backward compat — existing JSONB rows without this "
            "field deserialise correctly (None default)."
        ),
    )

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# DecompositionPlan — top-level envelope that marshals into plan_json JSONB
# ---------------------------------------------------------------------------


class DecompositionPlan(BaseModel):
    """Top-level agentic retrieval plan persisted to silver.answer_runs.plan_json.

    Per A.2 D3 (locked 2026-04-29): the plan_json JSONB column stores the full
    typed plan as authored by the Phase 1.B decomposer, plus per-step results
    captured during execution.  Replay/debugging reads plan_json to reconstruct
    the execution sequence without re-running the LLM.

    Use serialize_plan_for_jsonb(plan) for JSONB storage — Pydantic v2's
    mode='json' serialisation converts UUID→str and datetime→isoformat
    automatically.

    schema_version is a forward-compat marker.  Bump it when adding a required
    field so existing plan_json rows can be identified before attempting
    deserialization with the new schema.
    """

    schema_version: str = Field(
        default="1",
        description=(
            "Forward-compat marker.  '1' = Phase 1.A shape.  "
            "Bump in the same commit as any required-field addition."
        ),
    )
    trigger: DecompositionTrigger = Field(
        ...,
        description="Which D1 condition caused decomposition to fire",
    )
    sub_queries: list[SubQuery] = Field(
        default_factory=list,
        description=(
            "Typed sub-query envelopes.  Each element is one of the five "
            "discriminated SubQuery variants.  Max 5 per D4 post-decomposition cap."
        ),
    )
    decisions: list[Decision] = Field(
        default_factory=list,
        description="Conditional branching decisions recorded during plan execution (D4)",
    )
    verification: list[ClaimVerification] = Field(
        default_factory=list,
        description=(
            "Per-claim verification records (D5 — 1-revise budget).  "
            "Empty until Phase 4 wires the check-revise loop."
        ),
    )
    spatial_verifications: list[ClaimSpatialVerification] = Field(
        default_factory=list,
        description=(
            "Per-claim spatial verification records (Phase 5 / D7).  "
            "Populated after Phase 4 for claims with spatial signals when "
            "ConversationState.spatial_focus is set.  "
            "Empty when no spatial claims were detected or agentic flag is off."
        ),
    )
    temporal_verifications: list[ClaimTemporalVerification] = Field(
        default_factory=list,
        description=(
            "Per-claim temporal verification records (Phase 5 / D7).  "
            "Populated after Phase 4 for claims with temporal signals.  "
            "Empty when no temporal claims were detected or agentic flag is off."
        ),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp when the plan was created by the decomposer",
    )

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Helper — serialize for JSONB storage
# ---------------------------------------------------------------------------


def serialize_plan_for_jsonb(plan: DecompositionPlan) -> dict[str, Any]:
    """Convert a DecompositionPlan to a JSON-safe dict for JSONB storage.

    Uses Pydantic v2's model_dump(mode='json') which:
      - Converts UUID values to str (hex with hyphens)
      - Converts datetime values to ISO-8601 strings
      - Recursively serialises nested models

    The result is suitable for direct use as a JSONB value in an asyncpg
    query parameter (asyncpg accepts dict for JSONB columns).

    Example::

        plan = DecompositionPlan(trigger="multi_entity_ner", sub_queries=[...])
        payload = serialize_plan_for_jsonb(plan)
        await conn.execute(
            "UPDATE silver.answer_runs SET plan_json = $1 WHERE answer_run_id = $2",
            payload,
            run_id,
        )
    """
    return plan.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    # Literal type aliases
    "SubQueryClass",
    "DecompositionTrigger",
    "DecisionPoint",
    # Input models
    "FactualLookupInput",
    "EntityTraversalInput",
    "SpatialFilterInput",
    "DocumentPassageSearchInput",
    "NumericalAggregationInput",
    # Output sub-models
    "NodeRef",
    "EdgeRef",
    "GraphPath",
    "PassageHit",
    # Output models
    "FactualLookupOutput",
    "EntityTraversalOutput",
    "SpatialFilterOutput",
    "DocumentPassageSearchOutput",
    "NumericalAggregationOutput",
    # SubQuery discriminated union variants
    "SubQueryFactualLookup",
    "SubQueryEntityTraversal",
    "SubQuerySpatialFilter",
    "SubQueryDocumentPassageSearch",
    "SubQueryNumericalAggregation",
    "SubQuery",
    # Decision + verification
    "Decision",
    "ClaimVerification",
    "ClaimSpatialVerification",
    "ClaimTemporalVerification",
    # Top-level plan envelope
    "DecompositionPlan",
    # Helper
    "serialize_plan_for_jsonb",
    # Internal registry (exposed for exhaustiveness tests)
    "_SUB_QUERY_IO_REGISTRY",
]
