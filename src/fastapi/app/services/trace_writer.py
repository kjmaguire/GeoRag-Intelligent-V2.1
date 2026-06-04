"""Plan §0e retrieval trace writer.

Writes verbatim plan-§0e trace objects to ``silver.query_traces``.
Denormalises the JSONB payload into queryable columns per
``docs/architecture/trace_logging_design.md`` §5.

Q1 (write strategy) — buffered: the orchestrator calls
``enqueue_trace()`` which appends to an asyncio queue; a background
flush coroutine drains the queue every 5 s or 50 traces (whichever
comes first) by calling ``flush_buffer()``. Direct ``write_trace()``
is also exported for non-buffered callers (tests, replay tools).

Q4 (OTel pass-through) — ``otel_trace_id`` is denormalised from
``silver.answer_runs.trace_id`` on insert. The migration adds the
column; the writer reads it from the trace payload's optional
``otel_trace_id`` field.

Design
------
The orchestrator assembles a trace dict by node, ending in the
``persist_node`` of ``agentic_retrieval/graph.py`` which calls
``enqueue_trace(pool, trace_dict)``. Writes are fire-and-forget:
failures log at WARNING and never propagate. This preserves the
invariant that observability never fails a user query.

Wiring TODO (NOT in this commit — depends on Kyle's WIP):
  1. Add ``trace_payload_builder: dict`` field to
     ``AgenticRetrievalState`` (state.py).
  2. Each node mutates ``state.trace_payload_builder`` with its slice
     of the trace.
  3. ``persist_node`` (graph.py:50) calls
     ``await enqueue_trace(deps.pg_pool, state.trace_payload_builder)``
     before returning.
  4. Start the flush coroutine at FastAPI lifespan startup (main.py).
"""

from __future__ import annotations
from app.db import bind_workspace_scope

import asyncio
import json
import logging
import time
from contextlib import suppress
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic model — verbatim plan §0e shape
# ---------------------------------------------------------------------------


class LatencyBreakdown(BaseModel):
    routing: int | None = None
    vocab_enrichment: int | None = None
    entity_resolution: int | None = None
    retrieval_fan_out: int | None = None
    reranking: int | None = None
    context_assembly: int | None = None
    generation: int | None = None
    guard_evaluation: int | None = None
    total: int | None = None


class RawResultsPerSource(BaseModel):
    qdrant_dense: int = 0
    qdrant_sparse: int = 0
    postgis: int = 0
    neo4j: int = 0


class GuardResults(BaseModel):
    numeric_grounding: bool = True
    entity_grounding: bool = True
    citation_completeness: bool = True
    refusal_triggered: bool = False


class RetrievalTrace(BaseModel):
    """Plan §0e verbatim trace object."""

    # Identification
    query_id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID | None = None
    user_id: int | None = None
    answer_run_id: UUID | None = None
    otel_trace_id: str | None = None

    # Query
    user_query: str
    normalized_query: str | None = None
    conversation_turn: int = 1

    # Context budgeting
    system_prompt_tokens: int | None = None
    remaining_context_budget: int | None = None
    final_token_count: int | None = None

    # Routing
    router_decision: str | None = None
    router_confidence: float | None = None
    effective_intent: str | None = None
    tool_plan: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    generated_filters: dict[str, Any] = Field(default_factory=dict)
    vocab_terms_matched: list[str] = Field(default_factory=list)
    entities_resolved: list[dict[str, Any]] = Field(default_factory=list)

    # Retrieval
    raw_results_per_source: RawResultsPerSource = Field(default_factory=RawResultsPerSource)
    candidate_count_pre_rerank: int | None = None
    reranker_scores: list[float] = Field(default_factory=list)
    selected_context_groups: int | None = None
    dropped_candidates_with_score: list[dict[str, Any]] = Field(default_factory=list)
    evidence_types_in_context: list[str] = Field(default_factory=list)

    # Answer
    answer: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)

    # Guards + repair
    guard_results: GuardResults = Field(default_factory=GuardResults)
    guard_failure_codes: list[str] = Field(default_factory=list)
    repair_attempts: int = 0
    repair_strategies_used: list[str] = Field(default_factory=list)
    death_loop_triggered: bool = False

    # Cache (plan §2h)
    cache_hit: bool = False
    cache_type: str | None = None  # 'semantic' | 'retrieval' | 'miss'

    # Latency (ms)
    latency_ms: LatencyBreakdown = Field(default_factory=LatencyBreakdown)

    # Plan §3 context-prep audit (silver.query_traces.context_prep_audit JSONB)
    # Populated by persist_node when CONTEXT_PREP_ENABLED is True. None
    # when the flag was off OR the packet was empty.
    context_prep_audit: dict[str, Any] | None = None

    # Plan §3e multi-turn resolution (silver.query_traces.multi_turn_resolution JSONB)
    # Populated when MULTI_TURN_RESOLUTION_ENABLED is True AND the
    # resolver made changes. None otherwise.
    multi_turn_resolution: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Buffered queue
# ---------------------------------------------------------------------------


_QUEUE: asyncio.Queue[RetrievalTrace] | None = None
_FLUSH_INTERVAL_S = 5.0
_FLUSH_BATCH_SIZE = 50
_QUEUE_MAX = 1000  # hard cap; further enqueues drop with a WARNING


def _ensure_queue() -> asyncio.Queue[RetrievalTrace]:
    global _QUEUE
    if _QUEUE is None:
        _QUEUE = asyncio.Queue(maxsize=_QUEUE_MAX)
    return _QUEUE


async def enqueue_trace(pool: object, trace: RetrievalTrace | dict[str, Any]) -> None:
    """Append a trace to the buffer. Non-blocking; never raises.

    Args:
        pool: asyncpg Pool (unused by the enqueue path but kept in the
            signature so callers don't need to know whether buffered
            or hot-path is active).
        trace: a :class:`RetrievalTrace` or a dict that fits its shape.
    """
    if not isinstance(trace, RetrievalTrace):
        try:
            trace = RetrievalTrace.model_validate(trace)
        except Exception:
            logger.warning("trace_writer.enqueue_trace: invalid trace payload", exc_info=True)
            return

    q = _ensure_queue()
    try:
        q.put_nowait(trace)
    except asyncio.QueueFull:
        logger.warning(
            "trace_writer.enqueue_trace: queue full (max=%d) — dropping trace %s",
            _QUEUE_MAX,
            trace.query_id,
        )


# ---------------------------------------------------------------------------
# Flush loop
# ---------------------------------------------------------------------------


async def flush_buffer(pool: object) -> int:
    """Drain up to :data:`_FLUSH_BATCH_SIZE` traces and write them.

    Returns the number of traces written. Errors on individual rows are
    logged and do not block the rest of the batch.
    """
    if pool is None:
        return 0

    q = _ensure_queue()
    batch: list[RetrievalTrace] = []
    while len(batch) < _FLUSH_BATCH_SIZE:
        try:
            batch.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break

    if not batch:
        return 0

    written = 0
    for trace in batch:
        try:
            await write_trace(pool, trace)
            written += 1
        except Exception:
            logger.warning(
                "trace_writer.flush_buffer: write_trace failed for %s",
                trace.query_id,
                exc_info=True,
            )

    return written


async def run_flush_loop(pool: object, *, stop_event: asyncio.Event | None = None) -> None:
    """Long-running coroutine that flushes the buffer every
    :data:`_FLUSH_INTERVAL_S` seconds (or sooner when the queue
    reaches batch size).

    Start at FastAPI lifespan startup; stop via ``stop_event.set()``
    at shutdown. Catches all exceptions so the loop never dies on a
    transient DB hiccup.
    """
    stop_event = stop_event or asyncio.Event()
    logger.info("trace_writer.run_flush_loop: starting (interval=%.1fs, batch=%d)",
                _FLUSH_INTERVAL_S, _FLUSH_BATCH_SIZE)

    while not stop_event.is_set():
        try:
            written = await flush_buffer(pool)
            if written > 0:
                logger.debug("trace_writer.flush_buffer: wrote %d traces", written)
        except Exception:
            logger.exception("trace_writer.run_flush_loop: unexpected error in flush")

        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=_FLUSH_INTERVAL_S)

    # Drain on shutdown — one last flush so we lose ≤5 s of traces
    # not zero. Q1 acceptance contract.
    try:
        await flush_buffer(pool)
    except Exception:
        logger.exception("trace_writer.run_flush_loop: final-drain flush failed")

    logger.info("trace_writer.run_flush_loop: stopped")


# ---------------------------------------------------------------------------
# Direct write
# ---------------------------------------------------------------------------


_INSERT_SQL = """
    INSERT INTO silver.query_traces (
        answer_run_id, workspace_id, project_id, user_id,
        query_id, query_text, normalized_query, conversation_turn,
        system_prompt_tokens, remaining_context_budget, final_token_count,
        router_decision, router_confidence, effective_intent, otel_trace_id,
        qdrant_dense_count, qdrant_sparse_count, postgis_count, neo4j_count,
        candidate_count_pre_rerank, selected_context_groups,
        guard_pass, guard_failure_codes, repair_attempts, death_loop_triggered,
        cache_hit, cache_type,
        latency_total_ms, latency_routing_ms, latency_retrieval_ms,
        latency_reranking_ms, latency_generation_ms, latency_guards_ms,
        trace_payload,
        context_prep_audit, multi_turn_resolution
    ) VALUES (
        $1, $2, $3, $4,
        $5, $6, $7, $8,
        $9, $10, $11,
        $12, $13, $14, $15,
        $16, $17, $18, $19,
        $20, $21,
        $22, $23, $24, $25,
        $26, $27,
        $28, $29, $30,
        $31, $32, $33,
        $34,
        $35::jsonb, $36::jsonb
    )
    RETURNING trace_id
"""


async def write_trace(pool: object, trace: RetrievalTrace) -> UUID | None:
    """Insert one trace row. Returns the generated ``trace_id`` or
    ``None`` on failure.

    Errors are logged at WARNING and never propagated. The trace
    payload is the verbatim plan §0e JSON; denormalised columns are
    computed from it on the way in (see trace_logging_design.md §5).
    """
    if pool is None:
        logger.warning("trace_writer.write_trace: pg_pool is None — skipping insert")
        return None

    started = time.monotonic()
    try:
        payload_json = trace.model_dump_json()
        # guard_pass = "no GuardErrorCode fired anywhere". The
        # GuardResults sub-booleans (numeric_grounding / entity_grounding /
        # citation_completeness / refusal_triggered) only cover 4 of the
        # 16 plan §4b codes; relying on them alone would mark e.g.
        # NO_EVIDENCE_FOUND as guard_pass=true. Derive from the full
        # guard_failure_codes list instead.
        guard_pass = (
            len(trace.guard_failure_codes) == 0
            and trace.guard_results.numeric_grounding
            and trace.guard_results.entity_grounding
            and trace.guard_results.citation_completeness
            and not trace.guard_results.refusal_triggered
        )

        params = (
            str(trace.answer_run_id) if trace.answer_run_id else None,
            str(trace.workspace_id),
            str(trace.project_id) if trace.project_id else None,
            trace.user_id,
            str(trace.query_id),
            trace.user_query,
            trace.normalized_query,
            trace.conversation_turn,
            trace.system_prompt_tokens,
            trace.remaining_context_budget,
            trace.final_token_count,
            trace.router_decision,
            float(trace.router_confidence) if trace.router_confidence is not None else None,
            trace.effective_intent,
            trace.otel_trace_id,
            trace.raw_results_per_source.qdrant_dense,
            trace.raw_results_per_source.qdrant_sparse,
            trace.raw_results_per_source.postgis,
            trace.raw_results_per_source.neo4j,
            trace.candidate_count_pre_rerank,
            trace.selected_context_groups,
            guard_pass,
            trace.guard_failure_codes or None,
            trace.repair_attempts,
            trace.death_loop_triggered,
            trace.cache_hit,
            trace.cache_type,
            trace.latency_ms.total,
            trace.latency_ms.routing,
            trace.latency_ms.retrieval_fan_out,
            trace.latency_ms.reranking,
            trace.latency_ms.generation,
            trace.latency_ms.guard_evaluation,
            payload_json,
            (
                json.dumps(trace.context_prep_audit)
                if trace.context_prep_audit is not None
                else None
            ),
            (
                json.dumps(trace.multi_turn_resolution)
                if trace.multi_turn_resolution is not None
                else None
            ),
        )

        async with pool.acquire() as conn:  # type: ignore[union-attr]
            # silver.query_traces enables FORCE ROW LEVEL SECURITY with the
            # canonical app.workspace_id GUC policy. Bind it for this
            # transaction before the INSERT so the policy passes. SET LOCAL
            # requires being inside a tx; the explicit transaction also
            # gives us atomicity around the INSERT.
            async with conn.transaction():
                await bind_workspace_scope(conn, workspace_id=str(trace.workspace_id, site="trace_writer"),
                )
                row = await conn.fetchrow(_INSERT_SQL, *params)
            trace_id: UUID | None = row["trace_id"] if row else None

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.debug(
            "trace_writer.write_trace: inserted trace_id=%s for query_id=%s in %d ms",
            trace_id,
            trace.query_id,
            elapsed_ms,
        )
        return trace_id

    except Exception:
        logger.warning(
            "trace_writer.write_trace: insert failed for query_id=%s",
            trace.query_id,
            exc_info=True,
        )
        return None


__all__ = [
    "RetrievalTrace",
    "LatencyBreakdown",
    "RawResultsPerSource",
    "GuardResults",
    "enqueue_trace",
    "flush_buffer",
    "run_flush_loop",
    "write_trace",
]
