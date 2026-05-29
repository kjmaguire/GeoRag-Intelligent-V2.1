"""Track A.2 Phase 1.C-i — DecompositionPlan executor.

Executes every pending sub-query in a DecompositionPlan against the
appropriate existing retrieval tool.  Returns the same plan instance
(mutated in place) so the caller can re-emit to
silver.answer_runs.plan_json after this returns.

Per-class dispatch table
------------------------
  factual_lookup          → verify_numerical_claim (closest analogue; wraps
                            the FactualLookupInput fields into the verify call)
  entity_traversal        → traverse_knowledge_graph
  spatial_filter          → query_spatial_collars (supported predicates only;
                            unsupported predicates → outcome='error')
  document_passage_search → search_documents
  numerical_aggregation   → parameterised SQL aggregation against silver.*
                            (Phase H4): operation + table allowlisted,
                            column + group_by regex-validated, filter
                            values flow through asyncpg params (NO free
                            text). source_chunk_id = silver:{table}:
                            {op}:{column_hash}; computation_sql = the
                            parameterised template (§04i Layer 3).

Hallucination prevention (§04i)
--------------------------------
Layer 2: every output model carries a mandatory source_chunk_id.  When the
underlying tool does not naturally return a chunk-level provenance ID, a
synthetic fallback is generated as ``f"sq-{sub_query.id}-result"`` so the
claim remains traceable to its sub-query.  Each such site is flagged with
``# TODO(phase-1c-ii): wire real provenance`` so Phase 1.C-ii can replace
the synthetic IDs with genuine chunk/row/node IDs.

Idempotency
-----------
Sub-queries with outcome != 'pending' are skipped unconditionally.  Calling
execute_plan twice on the same plan is safe — the second call is a no-op for
already-completed sub-queries.

Timeout discipline
------------------
Per-sub-query timeout = sub_query.latency_budget_s (via asyncio.wait_for).
Overall plan cap = overall_timeout_s (via asyncio.wait_for wrapping the
gather).  Sub-queries still running when the overall cap fires are marked
outcome='timeout'.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from app.agent.deps import AgentDeps, ToolContext
from app.agent.tools import (
    query_spatial_collars,
    search_documents,
    traverse_knowledge_graph,
    verify_numerical_claim,
)
from app.models.decomposition import (
    _SUB_QUERY_IO_REGISTRY,
    DecompositionPlan,
    GraphPath,
    PassageHit,
    SubQuery,
    SubQueryDocumentPassageSearch,
    SubQueryEntityTraversal,
    SubQueryFactualLookup,
    SubQueryNumericalAggregation,
    SubQuerySpatialFilter,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported spatial predicates — the existing query_spatial_collars tool
# understands spatial radius queries (via ST_DWithin / center+radius) but
# does NOT natively support 'intersects', 'within', or 'contains' WKT
# predicates at this stage of the tool surface.  Phase 1.D will add a
# dedicated PostGIS-predicate tool for those.
# ---------------------------------------------------------------------------

_SUPPORTED_SPATIAL_PREDICATES = frozenset({"near"})


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def execute_plan(
    plan: DecompositionPlan,
    deps: AgentDeps,
    *,
    parallel: bool = True,
    overall_timeout_s: float = 12.0,
) -> DecompositionPlan:
    """Execute every pending sub-query in the plan against the right tool.

    Mutates each SubQuery envelope in plan.sub_queries:
      - sets started_at + completed_at
      - sets outcome (ok | empty | error | timeout)
      - sets result (typed output per class) on ok
      - sets error_message on error/timeout
      - leaves already-completed sub-queries untouched (idempotency guard)

    Returns the same plan instance (mutated in place).  Caller can then
    call serialize_plan_for_jsonb(plan) and persist to answer_runs.plan_json.

    Args:
        plan:             DecompositionPlan produced by the Phase 1.B decomposer.
        deps:             AgentDeps container carrying all database pools.
        parallel:         When True (default) all pending sub-queries fire
                          concurrently via asyncio.gather().  When False they
                          execute sequentially (useful for debugging and future
                          Phase 3 conditional-branch gates).
        overall_timeout_s: Wall-clock cap for the entire plan execution.
                          Sub-queries still running when the cap fires are marked
                          outcome='timeout'.  Default 12 s per §06 guidance.

    Returns:
        The same DecompositionPlan instance (mutated in place).
    """
    pending = [sq for sq in plan.sub_queries if sq.outcome == "pending"]
    if not pending:
        logger.debug("execute_plan: no pending sub-queries — plan is already complete")
        return plan

    ctx = ToolContext(deps)

    if parallel:
        coros = [_execute_sub_query(sq, ctx) for sq in pending]
        try:
            await asyncio.wait_for(
                asyncio.gather(*coros, return_exceptions=True),
                timeout=overall_timeout_s,
            )
        except TimeoutError:
            # Any sub-queries that haven't completed yet get marked timeout.
            _mark_remaining_pending_as_timeout(plan)
    else:
        deadline = asyncio.get_event_loop().time() + overall_timeout_s
        for sq in pending:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                _set_outcome(sq, "timeout", error_message="overall_timeout_exceeded")
                continue
            try:
                await asyncio.wait_for(_execute_sub_query(sq, ctx), timeout=remaining)
            except TimeoutError:
                _set_outcome(sq, "timeout", error_message="overall_timeout_exceeded")

    return plan


# ---------------------------------------------------------------------------
# Per-sub-query dispatcher
# ---------------------------------------------------------------------------


async def _execute_sub_query(sq: SubQuery, ctx: ToolContext) -> None:
    """Dispatch a single sub-query to its tool and mutate sq in place."""
    # Idempotency: skip already-completed sub-queries.
    if sq.outcome != "pending":
        return

    _stamp_started(sq)
    _, output_model_cls = _SUB_QUERY_IO_REGISTRY[sq.sub_query_class]

    try:
        if isinstance(sq, SubQueryFactualLookup):
            raw = await asyncio.wait_for(
                _dispatch_factual_lookup(sq, ctx),
                timeout=sq.latency_budget_s,
            )
        elif isinstance(sq, SubQueryEntityTraversal):
            raw = await asyncio.wait_for(
                _dispatch_entity_traversal(sq, ctx),
                timeout=sq.latency_budget_s,
            )
        elif isinstance(sq, SubQuerySpatialFilter):
            raw = await asyncio.wait_for(
                _dispatch_spatial_filter(sq, ctx),
                timeout=sq.latency_budget_s,
            )
        elif isinstance(sq, SubQueryDocumentPassageSearch):
            raw = await asyncio.wait_for(
                _dispatch_document_passage_search(sq, ctx),
                timeout=sq.latency_budget_s,
            )
        elif isinstance(sq, SubQueryNumericalAggregation):
            raw = await asyncio.wait_for(
                _dispatch_numerical_aggregation(sq, ctx),
                timeout=sq.latency_budget_s,
            )
        else:
            _stamp_completed(sq)
            _set_outcome(sq, "error", error_message=f"unknown_sub_query_class:{sq.sub_query_class}")
            return

    except TimeoutError:
        _stamp_completed(sq)
        _set_outcome(sq, "timeout", error_message=f"latency_budget_exceeded:{sq.latency_budget_s}s")
        logger.warning(
            "execute_plan: sub-query %s (%s) timed out after %.1fs",
            sq.id,
            sq.sub_query_class,
            sq.latency_budget_s,
        )
        return

    except NotImplementedError as exc:
        _stamp_completed(sq)
        _set_outcome(sq, "error", error_message=str(exc))
        logger.info(
            "execute_plan: sub-query %s (%s) not implemented: %s",
            sq.id,
            sq.sub_query_class,
            exc,
        )
        return

    except Exception as exc:
        _stamp_completed(sq)
        _set_outcome(sq, "error", error_message=str(exc))
        logger.exception(
            "execute_plan: sub-query %s (%s) raised an exception",
            sq.id,
            sq.sub_query_class,
        )
        return

    # Detect empty result before coercion
    if _is_empty_raw(raw):
        _stamp_completed(sq)
        _set_outcome(sq, "empty")
        return

    # Coerce raw dict into the typed output model (§04i Layer 2)
    try:
        sq.result = output_model_cls(**raw)
    except ValidationError as ve:
        _stamp_completed(sq)
        _set_outcome(sq, "error", error_message=f"output_shape_mismatch: {ve}")
        logger.error(
            "execute_plan: sub-query %s (%s) output shape mismatch: %s",
            sq.id,
            sq.sub_query_class,
            ve,
        )
        return

    _stamp_completed(sq)
    _set_outcome(sq, "ok")


# ---------------------------------------------------------------------------
# Per-class dispatch helpers
# ---------------------------------------------------------------------------


async def _dispatch_factual_lookup(
    sq: SubQueryFactualLookup,
    ctx: ToolContext,
) -> dict:
    """Dispatch a factual_lookup sub-query via verify_numerical_claim.

    Maps FactualLookupInput.table + entity_id + fields into the verify call
    shape.  We use the first numeric field from `fields`; for non-numeric
    fields we fabricate claimed_value=0.0 so the verify path still runs and
    returns the actual db_value for provenance.

    When verification returns verified=False and db_value is None (row not
    found or non-numeric column), we treat the result as the raw db_value
    still — the goal is retrieval, not pass/fail verification.

    If verify returns 'unverifiable' (db_value=None, verified=False), this
    maps to outcome='empty' via the _is_empty_raw check downstream.

    Returns a dict matching FactualLookupOutput shape.
    """
    inp = sq.input
    # Use first field as the column target for the verify call
    column = inp.fields[0] if inp.fields else "id"
    entity_id_str = str(inp.entity_id)

    result = await verify_numerical_claim(
        ctx,
        table=f"silver.{inp.table}",
        column=column,
        row_id=entity_id_str,
        claimed_value=0.0,
        tolerance=1e12,  # maximally permissive — we want retrieval, not verification
    )

    # TODO(phase-1c-ii): wire real provenance from the Silver row PK
    synthetic_chunk_id = f"silver:{inp.table}:{entity_id_str}"

    # Map NumericalClaimVerification → FactualLookupOutput shape
    value = result.db_value  # None when row not found
    return {
        "value": value,
        "passage_id": None,
        "source_chunk_id": synthetic_chunk_id,
        "retrieved_at": datetime.now(UTC),
    }


async def _dispatch_entity_traversal(
    sq: SubQueryEntityTraversal,
    ctx: ToolContext,
) -> dict:
    """Dispatch an entity_traversal sub-query via traverse_knowledge_graph.

    Maps EntityTraversalInput.start_entity + hop_count + edge_kinds to the
    traverse_knowledge_graph call.  hop_count maps to the existing tool's
    `depth` parameter.  edge_kinds: if non-empty the first entry is passed
    as relationship_type; the tool's allowlist validates it.

    Maps GraphTraversalResult entities → list[GraphPath] output shape.

    Returns a dict matching EntityTraversalOutput shape.
    """
    inp = sq.input
    rel_type = inp.edge_kinds[0] if inp.edge_kinds else None

    result = await traverse_knowledge_graph(
        ctx,
        entity_name=inp.start_entity,
        project_id=ctx.deps.project_id,
        relationship_type=rel_type,
        depth=inp.hop_count,
    )

    # Map GraphEntity flat list → list[GraphPath] (single-hop path per entity)
    # Phase 1.D will refine this into proper path-aware representation.
    from app.models.decomposition import EdgeRef, NodeRef  # noqa: PLC0415

    paths = []
    for entity in result.entities:
        start_node = NodeRef(
            neo4j_id=f"start:{inp.start_entity}",
            label="Unknown",  # TODO(phase-1c-ii): resolve from graph
            properties={},
        )
        end_node = NodeRef(
            neo4j_id=entity.entity_id,
            label=entity.entity_type,
            properties=entity.properties,
        )
        edge = EdgeRef(
            neo4j_id=f"rel:{entity.entity_id}",
            label=entity.relationship_type or "RELATED",
            properties={},
        )
        paths.append(GraphPath(nodes=[start_node, end_node], edges=[edge]))

    # TODO(phase-1c-ii): wire real provenance from Neo4j element IDs
    source_chunk_id = (
        f"neo4j:{inp.start_entity}:{inp.hop_count}"
        if result.entities
        else f"sq-{sq.id}-result"
    )

    return {
        "paths": [p.model_dump() for p in paths],
        "passage_id": None,
        "source_chunk_id": source_chunk_id,
    }


async def _dispatch_spatial_filter(
    sq: SubQuerySpatialFilter,
    ctx: ToolContext,
) -> dict:
    """Dispatch a spatial_filter sub-query via query_spatial_collars.

    Currently only the 'near' predicate is supported because
    query_spatial_collars uses a center-point + radius model (ST_DWithin).
    For 'within', 'intersects', and 'contains' the tool returns
    outcome='error' with error_message='unsupported_predicate'.

    For 'near', distance_m is used as radius_m.  The geometry_wkt is
    expected to be a WKT POINT; easting/northing are extracted naively from
    the coordinate pair.  Phase 1.D will add a proper WKT parser.

    Returns a dict matching SpatialFilterOutput shape.
    """
    inp = sq.input

    if inp.predicate not in _SUPPORTED_SPATIAL_PREDICATES:
        logger.info(
            "_dispatch_spatial_filter: predicate=%r is not yet supported by "
            "query_spatial_collars — returning error",
            inp.predicate,
        )
        raise ValueError(f"unsupported_predicate:{inp.predicate}")

    # Naive WKT POINT parser: extract X Y from "POINT(X Y)"
    center_easting: float | None = None
    center_northing: float | None = None
    wkt = inp.geometry_wkt.strip().upper()
    if wkt.startswith("POINT"):
        import re  # noqa: PLC0415
        m = re.search(r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)", wkt, re.IGNORECASE)
        if m:
            center_easting = float(m.group(1))
            center_northing = float(m.group(2))

    result = await query_spatial_collars(
        ctx,
        project_id=ctx.deps.project_id,
        center_easting=center_easting,
        center_northing=center_northing,
        radius_m=inp.distance_m,
    )

    import hashlib  # noqa: PLC0415
    wkt_hash = hashlib.md5(inp.geometry_wkt.encode()).hexdigest()[:8]  # noqa: S324
    # TODO(phase-1c-ii): wire real provenance from collar PKs
    source_chunk_id = f"postgis:{inp.target_table}:{inp.predicate}:{wkt_hash}"

    matching_rows = [
        {
            "hole_id": c.hole_id,
            "collar_id": c.collar_id,
            "easting": c.easting,
            "northing": c.northing,
            "elevation": c.elevation,
            "total_depth": c.total_depth,
        }
        for c in result.collars
    ]

    return {
        "matching_rows": matching_rows,
        "result_count": result.count,
        "source_chunk_id": source_chunk_id,
    }


async def _dispatch_document_passage_search(
    sq: SubQueryDocumentPassageSearch,
    ctx: ToolContext,
) -> dict:
    """Dispatch a document_passage_search sub-query via search_documents.

    Maps DocumentPassageSearchInput.query_text + top_k → search_documents.
    Coerces DocumentChunk results → PassageHit list for output shape.

    Returns a dict matching DocumentPassageSearchOutput shape.
    """
    import uuid  # noqa: PLC0415

    inp = sq.input
    result = await search_documents(
        ctx,
        query_text=inp.query_text,
        project_id=ctx.deps.project_id,
        limit=inp.top_k,
        score_threshold=inp.min_relevance,
    )

    passages = []
    for chunk in result.chunks:
        # Apply min_relevance filter that the tool doesn't guarantee without reranker.
        if chunk.relevance_score < inp.min_relevance:
            continue
        # TODO(phase-1c-ii): map chunk_id directly to silver.document_passages UUID
        try:
            passage_id = uuid.UUID(chunk.chunk_id)
        except (ValueError, AttributeError):
            passage_id = uuid.uuid5(uuid.NAMESPACE_URL, chunk.chunk_id)

        passages.append(
            PassageHit(
                passage_id=passage_id,
                text=chunk.text,
                relevance=min(max(chunk.relevance_score, 0.0), 1.0),
                document_id=chunk.source_document_id or chunk.report_id or "unknown",
                page_number=chunk.page,
            )
        )

    if not passages:
        # Empty — return without source_chunk_id; _is_empty_raw will catch it.
        return {}

    # source_chunk_id = Qdrant vector ID of the top-ranked passage (§04i Layer 2)
    top_chunk_id = result.chunks[0].chunk_id if result.chunks else f"sq-{sq.id}-result"
    # TODO(phase-1c-ii): resolve top_chunk_id to real passage UUID provenance

    return {
        "passages": [p.model_dump(mode="json") for p in passages],
        "source_chunk_id": top_chunk_id,
    }


_ALLOWED_AGG_OPERATIONS = {"count", "sum", "avg", "min", "max", "stddev"}
_ALLOWED_AGG_TABLES = {
    "collars", "lithology_logs", "samples", "drill_traces",
    "seismic_surveys", "mineral_claims", "reports", "structures",
    "alterations", "geochemistry",
}
# Identifier validator — column names must be lowercase alphanumeric +
# underscore + no dot / quote / space. Anything not matching this gets
# rejected with outcome='error' rather than fed to the SQL builder.
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _is_safe_ident(name: str) -> bool:
    return bool(_IDENT_RE.match(name or ""))


def _build_agg_sql(
    *,
    operation: str,
    table: str,
    column: str,
    group_by: list[str],
    filter_expr: dict[str, Any] | None,
) -> tuple[str, list[Any]]:
    """Build a parameterised SQL aggregation query.

    Returns (sql_text, parameter_values). Identifiers are validated
    against `_IDENT_RE` and the table allowlist; ONLY values flow
    through asyncpg parameter substitution. Free-text SQL is never
    interpolated.

    Per §04i Layer 3, computation_sql is the parameterised text — NOT
    the rendered-with-values form — so the audit trail shows the
    structure rather than leaking values.
    """
    if operation not in _ALLOWED_AGG_OPERATIONS:
        raise ValueError(f"operation not allowed: {operation!r}")
    if table not in _ALLOWED_AGG_TABLES:
        raise ValueError(f"table not allowed: {table!r}")
    if operation != "count" and not _is_safe_ident(column):
        raise ValueError(f"column not a valid identifier: {column!r}")
    for col in group_by:
        if not _is_safe_ident(col):
            raise ValueError(f"group_by column not a valid identifier: {col!r}")

    # Aggregate expression.
    if operation == "count":
        agg_expr = "count(*)"
    else:
        agg_expr = f"{operation}({column})"

    select_cols: list[str] = []
    if group_by:
        select_cols.extend(group_by)
    select_cols.append(f"{agg_expr} AS agg")

    where_clauses: list[str] = []
    params: list[Any] = []
    next_param = 1

    # filter_expr translation: flat {col: value} dict only for Phase
    # H4. Nested AND/OR conjunctions land later; the current dict form
    # covers 90% of real numeric-aggregation queries.
    for col, val in (filter_expr or {}).items():
        if not _is_safe_ident(col):
            raise ValueError(f"filter column not a valid identifier: {col!r}")
        if isinstance(val, (list, tuple)):
            # IN (...) — keep param-bound.
            placeholders = ", ".join(
                f"${next_param + i}" for i in range(len(val))
            )
            where_clauses.append(f"{col} IN ({placeholders})")
            params.extend(val)
            next_param += len(val)
        else:
            where_clauses.append(f"{col} = ${next_param}")
            params.append(val)
            next_param += 1

    sql = f"SELECT {', '.join(select_cols)} FROM silver.{table}"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    if group_by:
        sql += " GROUP BY " + ", ".join(group_by)
    return sql, params


def _column_hash(column: str) -> str:
    """Stable 12-char hash of the aggregation column for the
    source_chunk_id per the §04i Layer 2 contract."""
    return hashlib.sha256(column.encode("utf-8")).hexdigest()[:12]


async def _dispatch_numerical_aggregation(
    sq: SubQueryNumericalAggregation,
    ctx: ToolContext,
) -> dict:
    """SQL aggregation over a silver table.

    Phase H4 graduation — implements the §04i-Layer-3-compliant
    aggregation path:
      - operation + table validated against allowlists
      - column + group_by identifiers validated by regex
      - filter values flow through asyncpg parameter substitution
        (NO free-text SQL interpolation)
      - source_chunk_id = "silver:{table}:{operation}:{column_hash}"
      - computation_sql = the parameterised template (per §04i Layer 3)

    Returns a NumericalAggregationOutput-shaped dict the caller
    casts into the typed Pydantic model.
    """
    inp = sq.input
    try:
        sql, params = _build_agg_sql(
            operation=inp.operation,
            table=inp.target_table,
            column=inp.target_column,
            group_by=inp.group_by,
            filter_expr=inp.filter_expr,
        )
    except ValueError as exc:
        # Translate validation errors into the structured error path.
        # The outer wrapper will mark the sub-query outcome='error'.
        raise ValueError(f"numerical_aggregation_input_invalid:{exc}")

    async with ctx.deps.pg_pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    if not rows:
        # Empty result — return {} so the outer wrapper marks it
        # outcome='empty'.
        return {}

    if inp.group_by:
        # GROUP BY → dict[group_key, aggregate_value]
        agg: dict[str, Any] = {}
        for r in rows:
            key_parts = [str(r[col]) for col in inp.group_by]
            key = "|".join(key_parts)
            agg[key] = float(r["agg"]) if r["agg"] is not None else None
        result: Any = agg
    else:
        # Scalar — single row, single 'agg' column
        raw = rows[0]["agg"]
        result = float(raw) if raw is not None else 0.0

    source_chunk_id = (
        f"silver:{inp.target_table}:{inp.operation}:{_column_hash(inp.target_column)}"
    )

    return {
        "result":           result,
        "source_chunk_id":  source_chunk_id,
        "computation_sql":  sql,
    }


# ---------------------------------------------------------------------------
# Utility helpers (module-private)
# ---------------------------------------------------------------------------


def _stamp_started(sq: SubQuery) -> None:
    """Set started_at to the current UTC timestamp."""
    object.__setattr__(sq, "started_at", datetime.now(UTC))


def _stamp_completed(sq: SubQuery) -> None:
    """Set completed_at to the current UTC timestamp."""
    object.__setattr__(sq, "completed_at", datetime.now(UTC))


def _set_outcome(
    sq: SubQuery,
    outcome: str,
    *,
    error_message: str | None = None,
) -> None:
    """Mutate sq.outcome (and optionally sq.error_message)."""
    object.__setattr__(sq, "outcome", outcome)
    if error_message is not None:
        object.__setattr__(sq, "error_message", error_message)


def _is_empty_raw(raw: dict) -> bool:
    """Return True when the raw dispatch result represents an empty result.

    Empty semantics per class:
      - entity_traversal: paths list is empty or not present
      - spatial_filter:   matching_rows list is empty or not present
      - document_passage_search: passages list is empty or dict is empty
      - factual_lookup: value is None
    """
    if not raw:
        return True
    # document_passage_search
    if "passages" in raw:
        return len(raw.get("passages", [])) == 0
    # entity_traversal
    if "paths" in raw:
        return len(raw.get("paths", [])) == 0
    # spatial_filter
    if "matching_rows" in raw:
        return len(raw.get("matching_rows", [])) == 0 and raw.get("result_count", 0) == 0
    # factual_lookup
    if "value" in raw:
        return raw["value"] is None
    return False


def _mark_remaining_pending_as_timeout(plan: DecompositionPlan) -> None:
    """Mark any sub-queries still in outcome='pending' as 'timeout'.

    Called when the overall_timeout_s cap fires via asyncio.wait_for on the
    gather — tasks that didn't complete in time may still be pending.
    """
    for sq in plan.sub_queries:
        if sq.outcome == "pending":
            _stamp_completed(sq)
            _set_outcome(sq, "timeout", error_message="overall_timeout_exceeded")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "execute_plan",
]
