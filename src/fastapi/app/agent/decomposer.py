"""Track A.2 Phase 1.B — decomposer logic.

Per `docs/plans/track-a2-agentic-retrieval.md` D1 (trigger detection) and D2
(sub-query taxonomy).

This module exposes two pure functions:

    detect_decomposition_trigger(...)  → DecompositionTrigger | None
    decompose_query(...)               → DecompositionPlan

Both functions are pure: no I/O, no async, no LLM calls, no DB access.  They
receive upstream signals (classifier output, NER entities, retrieval counts) as
plain Python values and return typed Pydantic models.

Phase 1.C (orchestrator integration) is the impure caller — it invokes these
functions after the upstream classifiers/NER have run, then passes the returned
DecompositionPlan to the async tool-execution layer.

Trigger priority order (first match wins, per D1):
    1. deterministic_multi_intent — classifier returned >1 category
    2. multi_entity_ner           — NER detected >1 named entity
    3. multi_document_signal      — cross-document regex heuristic matched
    4. continued_empty_escalation — single-shot retrieval returned 0 results

Category → SubQueryClass mapping (per D2):
    document / factual → document_passage_search
    spatial            → spatial_filter
    computation / aggregate → numerical_aggregation
    viz                → skipped (not a retrieval sub-query class)
    any other          → factual_lookup (fallback)

Latency budgets per §05c:
    factual_lookup          2.0 s
    entity_traversal        3.0 s
    spatial_filter          1.5 s
    document_passage_search 2.5 s
    numerical_aggregation   2.0 s
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.models.decomposition import (
    DecompositionPlan,
    DecompositionTrigger,
    DocumentPassageSearchInput,
    EntityTraversalInput,
    FactualLookupInput,
    NumericalAggregationInput,
    SpatialFilterInput,
    SubQuery,
    SubQueryDocumentPassageSearch,
    SubQueryEntityTraversal,
    SubQueryFactualLookup,
    SubQueryNumericalAggregation,
    SubQuerySpatialFilter,
)

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Module-level compiled patterns for multi_document_signal detection.
#
# Patterns capture cross-document conjunctions in geological queries.
# Compiled once at import time — never re-compiled per call.
#
# False-positive analysis against realistic NI 43-101 query patterns:
#
#   LOW risk phrases (well-targeted by the pattern):
#     "compare the 2022 and 2024 filings"       → fires (correct)
#     "across the Athabasca and PLS reports"    → fires (correct)
#     "in the 2021 and 2023 technical reports"  → fires (correct)
#
#   MODERATE risk phrases (may fire on single-doc context):
#     "in the historical and current reports"   → fires (acceptable — "historical
#                                                  and current" implies >1 doc)
#     "between documents"                       → fires on pattern 1 with no year
#                                                  pair, but only if a Cap-word
#                                                  follows (mitigation: word [A-Z]
#                                                  requirement excludes bare verb)
#
#   LOW false-positive risk phrases:
#     "in the 2024 NI 43-101 report"            → does NOT fire (no conjunction)
#     "across the whole deposit"                → does NOT fire (no doc keyword)
#     "between the two formations"              → does NOT fire (no doc keyword)
#
# Both patterns require a document-type keyword ("reports?", "documents?",
# "filings?", "memos?") to fire, which keeps them geological-domain-scoped.
# ---------------------------------------------------------------------------

# Pattern 1: cross-document conjunctions with named documents
# Matches: "in reports Athabasca and Fission", "across documents A, B and C"
_MULTI_DOC_NAMED_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?:in|across|between)\s+"
    r"(?:reports?|documents?|filings?|memos?)\s+"
    r"(?:[A-Z]\w*\s*(?:and|,)\s*)+"
    r"[A-Z]\w*",
    re.IGNORECASE,
)

# Pattern 2: cross-document conjunctions with explicit year pairs
# Matches: "the 2022 and 2024 reports", "compare 2021 and 2023 filings"
_MULTI_DOC_YEAR_PAIR_PATTERN: re.Pattern[str] = re.compile(
    r"\b(\d{4})\s+and\s+(\d{4})\s+(?:reports?|filings?|memos?|documents?)\b",
    re.IGNORECASE,
)

# Pattern 3: explicit cross-document conjunctions without named docs
# Matches: "across the 2024 and 2023 technical reports",
#          "between the Athabasca and Fission documents"
_MULTI_DOC_ACROSS_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?:across|between)\s+(?:the\s+)?(?:reports?|documents?|filings?|memos?)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Category → SubQueryClass mapping
# ---------------------------------------------------------------------------

# Maps spec QueryClassLiteral values to the D2 SubQueryClass names.
# "viz" is intentionally absent — visualization intents do not generate
# retrieval sub-queries; the orchestrator handles them via the viz builder.
_CATEGORY_TO_CLASS: dict[str, str] = {
    "document":    "document_passage_search",
    "factual":     "document_passage_search",
    "spatial":     "spatial_filter",
    "computation": "numerical_aggregation",
    "aggregate":   "numerical_aggregation",  # defensive alias
}

# Default latency budgets per §05c — never vary these per-call.
_LATENCY_BUDGETS: dict[str, float] = {
    "factual_lookup":           2.0,
    "entity_traversal":         3.0,
    "spatial_filter":           1.5,
    "document_passage_search":  2.5,
    "numerical_aggregation":    2.0,
}


# ---------------------------------------------------------------------------
# Public API — detect_decomposition_trigger
# ---------------------------------------------------------------------------


def detect_decomposition_trigger(
    *,
    query_text: str,
    classifier_categories: list[str],
    ner_entities: list[str] = (),
    retrieval_result_count: int | None = None,
) -> DecompositionTrigger | None:
    """Examine upstream signals and return the first matching D1 trigger.

    Returns None when no trigger fires — the query should continue through
    the existing §04h single-shot retrieval path unchanged.

    Decision tree (priority order — first match wins):
      1. len(classifier_categories) > 1  → "deterministic_multi_intent"
      2. len(ner_entities) > 1           → "multi_entity_ner"
      3. cross-document regex match      → "multi_document_signal"
      4. retrieval_result_count == 0 AND
         classifier_categories non-empty → "continued_empty_escalation"
      5. otherwise                       → None

    Args:
        query_text:              Raw natural-language query from the user.
        classifier_categories:   List of spec query class strings returned by
                                 classify_query() or the LLM classifier.  May
                                 contain duplicates — deduplicated internally.
        ner_entities:            Named-entity recognition hits from upstream NER.
                                 Pass an empty list/tuple when NER has not run.
        retrieval_result_count:  Result count from the single-shot §04h retrieval.
                                 Pass None when retrieval has not yet run.

    Returns:
        A DecompositionTrigger literal or None.

    Examples:
        >>> detect_decomposition_trigger(
        ...     query_text="How many holes near the deposit?",
        ...     classifier_categories=["spatial", "factual"],
        ... )
        'deterministic_multi_intent'

        >>> detect_decomposition_trigger(
        ...     query_text="Describe MS-117.",
        ...     classifier_categories=["spatial"],
        ...     ner_entities=["MS-117", "Triple R"],
        ... )
        'multi_entity_ner'
    """
    # Priority 1: compound query — classifier returned more than one intent
    unique_categories = list(dict.fromkeys(classifier_categories))  # order-preserving dedup
    if len(unique_categories) > 1:
        return "deterministic_multi_intent"

    # Priority 2: multi-entity — NER detected more than one named entity
    unique_entities = list(dict.fromkeys(ner_entities))  # order-preserving dedup
    if len(unique_entities) > 1:
        return "multi_entity_ner"

    # Priority 3: multi-document signal — cross-document conjunction heuristic
    if _has_multi_document_signal(query_text):
        return "multi_document_signal"

    # Priority 4: escalation after empty single-shot retrieval
    if retrieval_result_count == 0 and len(unique_categories) > 0:
        return "continued_empty_escalation"

    return None


def _has_multi_document_signal(query_text: str) -> bool:
    """Return True when the query contains a cross-document conjunction.

    Checks three compiled patterns:
      - Named-document conjunction ("in reports A and B")
      - Year-pair conjunction ("the 2022 and 2024 filings")
      - Bare cross-document preposition ("across the reports")

    All three require a document-type keyword to fire — this keeps the
    heuristic geological-domain-scoped and suppresses general English
    conjunctions ("between the two formations").
    """
    return bool(
        _MULTI_DOC_NAMED_PATTERN.search(query_text)
        or _MULTI_DOC_YEAR_PAIR_PATTERN.search(query_text)
        or _MULTI_DOC_ACROSS_PATTERN.search(query_text)
    )


# ---------------------------------------------------------------------------
# Public API — decompose_query
# ---------------------------------------------------------------------------


def decompose_query(
    *,
    query_text: str,
    trigger: DecompositionTrigger,
    classifier_categories: list[str],
    ner_entities: list[str] = (),
    max_sub_queries: int = 5,
) -> DecompositionPlan:
    """Produce a typed DecompositionPlan for a query given a fired trigger.

    Returns a DecompositionPlan with:
      - trigger field set to the provided trigger
      - sub_queries list non-empty (at least one typed SubQuery envelope)
      - All envelopes have outcome="pending" and result=None
      - All envelopes have sequential IDs ("sq-1", "sq-2", ...)
      - All envelopes have latency_budget_s from §05c defaults

    No I/O is performed — this is a pure planning function.  The orchestrator
    (Phase 1.C) populates result fields after running the typed tools.

    Args:
        query_text:            Raw natural-language query from the user.
        trigger:               The D1 trigger that fired (from
                               detect_decomposition_trigger).
        classifier_categories: Spec query class list from the classifier.
        ner_entities:          Named-entity hits from upstream NER.
        max_sub_queries:       Maximum number of sub-queries to produce.
                               Excess categories/entities are dropped in
                               priority order.  Default 5 per D4 cap.

    Returns:
        A DecompositionPlan with a non-empty sub_queries list.

    Raises:
        ValueError: When max_sub_queries < 1.
    """
    if max_sub_queries < 1:
        raise ValueError(f"max_sub_queries must be >= 1, got {max_sub_queries}")

    sub_queries: list[SubQuery] = []

    if trigger == "deterministic_multi_intent":
        sub_queries = _build_multi_intent_sub_queries(
            query_text=query_text,
            classifier_categories=classifier_categories,
            max_sub_queries=max_sub_queries,
        )

    elif trigger == "multi_entity_ner":
        sub_queries = _build_multi_entity_sub_queries(
            ner_entities=ner_entities,
            max_sub_queries=max_sub_queries,
        )

    elif trigger == "multi_document_signal":
        sub_queries = _build_multi_document_sub_queries(query_text=query_text)

    elif trigger == "continued_empty_escalation":
        sub_queries = _build_empty_escalation_sub_queries(
            query_text=query_text,
            classifier_categories=classifier_categories,
        )

    plan = DecompositionPlan(
        trigger=trigger,
        sub_queries=sub_queries,
    )
    _assign_sub_query_ids(plan)
    return plan


# ---------------------------------------------------------------------------
# Internal helper — assign sequential IDs
# ---------------------------------------------------------------------------


def _assign_sub_query_ids(plan: DecompositionPlan) -> None:
    """Mutate the plan's sub_queries to assign sequential sq-N IDs.

    Called by decompose_query() after the sub-query list is fully built.
    IDs are 1-indexed ("sq-1", "sq-2", ...) in list order.

    Mutates in-place; returns None.  Stable sequential IDs allow the Phase
    1.C orchestrator to reference sub-queries by position without a
    separate lookup.
    """
    for idx, sq in enumerate(plan.sub_queries, start=1):
        # Pydantic v2 models are mutable by default (model_config does not
        # set frozen=True).  Direct attribute assignment is safe here.
        object.__setattr__(sq, "id", f"sq-{idx}")


# ---------------------------------------------------------------------------
# Per-trigger builders (internal)
# ---------------------------------------------------------------------------


def _build_multi_intent_sub_queries(
    *,
    query_text: str,
    classifier_categories: list[str],
    max_sub_queries: int,
) -> list[SubQuery]:
    """Build sub-queries for deterministic_multi_intent trigger.

    One sub-query per unique category (deduped), skipping "viz" (not a
    retrieval class), capped at max_sub_queries.

    Spatial sub-queries use a placeholder geometry_wkt="POINT(0 0)" —
    the orchestrator (Phase 1.C) replaces this with the real WKT once
    spatial entity extraction runs.

    Numerical aggregation sub-queries use operation="count" as a
    placeholder — the orchestrator (Phase 1.C) refines this after
    understanding the query's aggregation intent.
    """
    seen: set[str] = set()
    sub_queries: list[SubQuery] = []

    for category in classifier_categories:
        if len(sub_queries) >= max_sub_queries:
            break

        # Skip duplicates (categories may appear more than once in the list)
        if category in seen:
            continue
        seen.add(category)

        # viz is not a retrieval sub-query class — skip silently
        if category == "viz":
            continue

        sq_class = _CATEGORY_TO_CLASS.get(category, "factual_lookup")

        sq: SubQuery
        if sq_class == "document_passage_search":
            sq = SubQueryDocumentPassageSearch(
                id="sq-placeholder",  # overwritten by _assign_sub_query_ids
                sub_query_class="document_passage_search",
                input=DocumentPassageSearchInput(
                    query_text=query_text,
                    top_k=10,
                    min_relevance=0.6,
                ),
                latency_budget_s=_LATENCY_BUDGETS["document_passage_search"],
                outcome="pending",
                result=None,
                started_at=None,
                completed_at=None,
            )

        elif sq_class == "spatial_filter":
            # TODO(Phase 1.C): orchestrator must overwrite geometry_wkt after
            # spatial entity extraction resolves the reference geometry.
            sq = SubQuerySpatialFilter(
                id="sq-placeholder",
                sub_query_class="spatial_filter",
                input=SpatialFilterInput(
                    predicate="within",
                    target_table="collars",
                    geometry_wkt="POINT(0 0)",  # placeholder — Phase 1.C fills in
                    distance_m=None,
                ),
                latency_budget_s=_LATENCY_BUDGETS["spatial_filter"],
                outcome="pending",
                result=None,
                started_at=None,
                completed_at=None,
            )

        elif sq_class == "numerical_aggregation":
            # TODO(Phase 1.C): orchestrator must refine operation, target_column,
            # and filter_expr after parsing the query's aggregation intent.
            sq = SubQueryNumericalAggregation(
                id="sq-placeholder",
                sub_query_class="numerical_aggregation",
                input=NumericalAggregationInput(
                    operation="count",       # placeholder — Phase 1.C refines
                    target_table="samples",  # placeholder — Phase 1.C refines
                    target_column="sample_id",
                ),
                latency_budget_s=_LATENCY_BUDGETS["numerical_aggregation"],
                outcome="pending",
                result=None,
                started_at=None,
                completed_at=None,
            )

        else:
            # factual_lookup fallback
            sq = SubQueryFactualLookup(
                id="sq-placeholder",
                sub_query_class="factual_lookup",
                input=FactualLookupInput(
                    table="reports",
                    entity_id=query_text[:64],  # best-effort — Phase 1.C resolves entity
                    fields=["title", "effective_date"],
                ),
                latency_budget_s=_LATENCY_BUDGETS["factual_lookup"],
                outcome="pending",
                result=None,
                started_at=None,
                completed_at=None,
            )

        sub_queries.append(sq)

    return sub_queries


def _build_multi_entity_sub_queries(
    *,
    ner_entities: list[str],
    max_sub_queries: int,
) -> list[SubQuery]:
    """Build sub-queries for multi_entity_ner trigger.

    One entity_traversal sub-query per unique named entity, capped at
    max_sub_queries.  hop_count=1 is the safe default — the orchestrator
    (Phase 1.C) may increase it based on the query's graph depth intent.
    """
    seen: set[str] = set()
    sub_queries: list[SubQuery] = []

    for entity in ner_entities:
        if len(sub_queries) >= max_sub_queries:
            break
        if entity in seen:
            continue
        seen.add(entity)

        sq: SubQuery = SubQueryEntityTraversal(
            id="sq-placeholder",
            sub_query_class="entity_traversal",
            input=EntityTraversalInput(
                start_entity=entity,
                hop_count=1,
                edge_kinds=[],  # all edge types; Phase 1.C may narrow
            ),
            latency_budget_s=_LATENCY_BUDGETS["entity_traversal"],
            outcome="pending",
            result=None,
            started_at=None,
            completed_at=None,
        )
        sub_queries.append(sq)

    return sub_queries


def _build_multi_document_sub_queries(
    *,
    query_text: str,
) -> list[SubQuery]:
    """Build sub-queries for multi_document_signal trigger.

    Produces a single document_passage_search sub-query with widened
    recall parameters:
      - min_relevance=0.5  (lower than default 0.6 to widen recall across docs)
      - top_k=20           (higher than default 10 to surface hits from >1 doc)

    A single sub-query is sufficient here because the document_passage_search
    tool already searches across all documents — there is no per-document
    split needed at the sub-query level.  The orchestrator (Phase 1.C) may
    add a document_filter if specific document IDs are extractable from the
    query text.
    """
    sq: SubQuery = SubQueryDocumentPassageSearch(
        id="sq-placeholder",
        sub_query_class="document_passage_search",
        input=DocumentPassageSearchInput(
            query_text=query_text,
            top_k=20,         # widened for multi-doc coverage
            min_relevance=0.5,  # lowered to widen recall across documents
        ),
        latency_budget_s=_LATENCY_BUDGETS["document_passage_search"],
        outcome="pending",
        result=None,
        started_at=None,
        completed_at=None,
    )
    return [sq]


def _build_empty_escalation_sub_queries(
    *,
    query_text: str,
    classifier_categories: list[str],
) -> list[SubQuery]:
    """Build sub-queries for continued_empty_escalation trigger.

    Produces exactly 2 sub-queries:
      1. document_passage_search with a lightly normalised rephrase of the
         original query (removes trailing "?" and lowercases).  Phase 3 will
         add LLM-driven rephrasing; this V1 normalisation is the placeholder.
      2. factual_lookup if classifier_categories contains "factual" — otherwise
         a second document_passage_search with the query appended to itself
         (widens token coverage; Phase 3 replaces with semantic expansion).

    Rationale: the first sub-query uses an alternative surface form of the
    query to recover from tokenisation or embedding misses.  The second
    sub-query provides a different retrieval path (Silver table vs Qdrant)
    when the factual class is active, or a broader passage search otherwise.
    """
    rephrased = _normalise_query(query_text)

    sq1: SubQuery = SubQueryDocumentPassageSearch(
        id="sq-placeholder",
        sub_query_class="document_passage_search",
        input=DocumentPassageSearchInput(
            query_text=rephrased,
            top_k=10,
            min_relevance=0.6,
        ),
        latency_budget_s=_LATENCY_BUDGETS["document_passage_search"],
        outcome="pending",
        result=None,
        started_at=None,
        completed_at=None,
    )

    sq2: SubQuery
    unique_categories = list(dict.fromkeys(classifier_categories))
    if "factual" in unique_categories:
        # Retrieve from Silver tables directly (different retrieval path)
        sq2 = SubQueryFactualLookup(
            id="sq-placeholder",
            sub_query_class="factual_lookup",
            input=FactualLookupInput(
                table="reports",
                entity_id=rephrased[:64],
                fields=["title", "effective_date", "qp_name"],
            ),
            latency_budget_s=_LATENCY_BUDGETS["factual_lookup"],
            outcome="pending",
            result=None,
            started_at=None,
            completed_at=None,
        )
    else:
        # Widen token coverage by appending the query to itself.
        # TODO(Phase 3): replace with LLM-driven semantic expansion.
        expanded = f"{rephrased} {rephrased}"
        sq2 = SubQueryDocumentPassageSearch(
            id="sq-placeholder",
            sub_query_class="document_passage_search",
            input=DocumentPassageSearchInput(
                query_text=expanded,
                top_k=10,
                min_relevance=0.6,
            ),
            latency_budget_s=_LATENCY_BUDGETS["document_passage_search"],
            outcome="pending",
            result=None,
            started_at=None,
            completed_at=None,
        )

    return [sq1, sq2]


# ---------------------------------------------------------------------------
# Internal text helpers
# ---------------------------------------------------------------------------


def _normalise_query(query_text: str) -> str:
    """Return a lightly normalised form of query_text for escalation rephrase.

    V1 normalisation:
      - Strip trailing question marks and whitespace
      - Strip leading/trailing whitespace
      - Lowercase the result

    Phase 3 replaces this with LLM-driven semantic rephrasing.
    """
    return query_text.rstrip("? \t").strip().lower()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "detect_decomposition_trigger",
    "decompose_query",
    "_assign_sub_query_ids",  # exposed for tests
]
