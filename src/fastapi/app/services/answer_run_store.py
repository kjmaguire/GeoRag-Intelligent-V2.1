"""Asyncpg INSERT helpers for silver.answer_runs and silver.answer_retrieval_items.

Module 4 Phase B Chunk 3 -- B11/B12 answer_runs + answer_retrieval_items INSERT wiring.

Design
------
Both functions accept a live asyncpg pool and Pydantic CREATE models (from
app.models.answer_run).  They are fire-and-forget from the orchestrator's
perspective: errors are caught and logged, never propagated to the caller.
This preserves the invariant that observability writes never fail a user query.

Parameterization
----------------
All values are passed as asyncpg positional parameters ($1, $2, ...) — no
string interpolation.  asyncpg reuses prepared statements per connection;
PgBouncer transaction-mode means prepared statements are not cached across
transactions, but asyncpg's statement_cache_size=0 is set in the pool config
(see main.py) so we parse on each call.  This is the correct tradeoff for
PgBouncer transaction-mode deployments.

Stages
------
Module 4 Chunk 3 owns the 'retrieved' and 'reranked' stage writes.
'in_context' stage is Module 5 scope.
'cited' stage is Module 6 scope.

Both are provided here so the orchestrator can call them without importing
from multiple modules.

answer_run_id
-------------
The DB generates a UUID via gen_random_uuid() (DEFAULT on the column).
insert_answer_run() returns the generated UUID by using RETURNING.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

logger = logging.getLogger(__name__)


async def insert_answer_run(pool: object, run: object) -> UUID | None:
    """Insert one row into silver.answer_runs and return the generated UUID.

    Args:
        pool:  asyncpg Pool (app.state.pg_pool).
        run:   AnswerRunCreate instance.

    Returns:
        The generated answer_run_id UUID, or None if the insert failed.
        Failures are logged at WARNING level and never raised.
    """
    if pool is None:
        logger.warning("insert_answer_run: pg_pool is None — skipping insert")
        return None

    try:
        # Phase B addendum: cache_hit_of_run_id column added in migration batch 18.
        # When set, this run reused a CachedRetrievalContext from a prior run.
        #
        # 2026-05-25: confidence + latency_ms added (migration 2026_05_25_200000)
        # — populated for fresh INSERTs by the refusal-row helper below; the
        # post-INSERT UPDATE block in run_deterministic_rag overwrites them on
        # the normal path after the answer is assembled.
        sql = """
            INSERT INTO silver.answer_runs (
                workspace_id,
                project_id,
                user_id,
                query_text,
                query_class,
                embedding_model,
                embedding_model_version,
                sparse_model,
                sparse_model_version,
                fusion_method,
                sparse_boost_applied,
                reranker_version,
                retrieval_strategy_version,
                workspace_data_version_at_query,
                project_data_version_at_query,
                backend_used,
                backend_chain,
                model_name,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_creation_tokens,
                speculative_acceptance_rate_sample,
                evidence_truncated_count,
                citation_lifecycle_state,
                citation_mode,
                trace_id,
                root_span_id,
                partial_failure_details,
                cache_hit_of_run_id,
                confidence,
                latency_ms,
                rejection_reason
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
                $21, $22, $23, $24, $25, $26, $27, $28, $29, $30,
                $31, $32, $33
            )
            RETURNING answer_run_id
        """
        # Serialize JSONB fields
        backend_chain_arr = getattr(run, "backend_chain", None)
        partial_failure_details = getattr(run, "partial_failure_details", None)
        partial_failure_json = (
            json.dumps(partial_failure_details) if partial_failure_details is not None else None
        )
        cache_hit_of_run_id = getattr(run, "cache_hit_of_run_id", None)

        async with pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(
                sql,
                str(getattr(run, "workspace_id", "")),
                str(getattr(run, "project_id", "")) if getattr(run, "project_id", None) else None,
                getattr(run, "user_id", None),
                getattr(run, "query_text", ""),
                getattr(run, "query_class", "unknown"),
                getattr(run, "embedding_model", None),
                getattr(run, "embedding_model_version", None),
                getattr(run, "sparse_model", None),
                getattr(run, "sparse_model_version", None),
                getattr(run, "fusion_method", None),
                getattr(run, "sparse_boost_applied", None),
                getattr(run, "reranker_version", None),
                getattr(run, "retrieval_strategy_version", None),
                getattr(run, "workspace_data_version_at_query", 1),
                getattr(run, "project_data_version_at_query", None),
                getattr(run, "backend_used", None),
                backend_chain_arr,
                getattr(run, "model_name", None),
                getattr(run, "input_tokens", None),
                getattr(run, "output_tokens", None),
                getattr(run, "cache_read_tokens", None),
                getattr(run, "cache_creation_tokens", None),
                getattr(run, "speculative_acceptance_rate_sample", None),
                getattr(run, "evidence_truncated_count", None),
                getattr(run, "citation_lifecycle_state", None),
                getattr(run, "citation_mode", None),
                getattr(run, "trace_id", None),
                getattr(run, "root_span_id", None),
                partial_failure_json,
                str(cache_hit_of_run_id) if cache_hit_of_run_id is not None else None,
                getattr(run, "confidence", None),
                getattr(run, "latency_ms", None),
                getattr(run, "rejection_reason", None),
            )

        if row:
            answer_run_id = UUID(str(row["answer_run_id"]))
            logger.debug(
                "insert_answer_run: inserted answer_run_id=%s query_class=%s",
                answer_run_id,
                getattr(run, "query_class", "?"),
            )
            return answer_run_id

    except Exception:
        logger.warning(
            "insert_answer_run: INSERT failed (non-fatal — observability write skipped)",
            exc_info=True,
        )
    return None


async def insert_refusal_answer_run(
    pool: object,
    *,
    workspace_id: "UUID | str",
    project_id: "UUID | str | None",
    query_text: str,
    rejection_reason: str,
    latency_ms: int,
    workspace_data_version: int = 0,
    project_data_version: int | None = None,
) -> "UUID | None":
    """Write a minimal `rejected` row for an orchestrator early-refusal path.

    Background — `run_deterministic_rag` has two early returns that fire
    *before* the normal draft-INSERT (LLM health probe failure and the
    out-of-scope classifier refusal). Without a row in `silver.answer_runs`
    the Retrieval Inspector deep link resolves to nothing, so the user
    can't see *why* the run refused. This helper writes a compact row with
    `citation_lifecycle_state='rejected'`, `confidence=0.0`, the measured
    `latency_ms`, and a free-text `rejection_reason` so the inspector page
    has something useful to render.

    Defensive defaults are used for the freshness columns — the refusal
    paths fire before `_fetch_data_versions` runs, so we accept whatever
    the caller passes (0 fallback is fine; the row is for forensics only,
    not for cache lookups).

    Failures are logged and swallowed — observability writes never break
    the user-facing refusal.
    """
    if pool is None:
        logger.warning(
            "insert_refusal_answer_run: pg_pool is None — skipping INSERT"
        )
        return None

    try:
        # Local import avoids a circular boot-time dependency when the model
        # module needs answer_run_store at import time (it doesn't today, but
        # the lazy import is cheap and keeps both modules independent).
        from app.models.answer_run import AnswerRunCreate  # noqa: PLC0415

        run = AnswerRunCreate(
            workspace_id=workspace_id,  # type: ignore[arg-type]
            project_id=project_id,  # type: ignore[arg-type]
            user_id=None,
            query_text=query_text,
            query_class="unknown",
            workspace_data_version_at_query=workspace_data_version,
            project_data_version_at_query=project_data_version,
            citation_lifecycle_state="rejected",
            citation_mode="posthoc_span_resolution",
            confidence=0.0,
            latency_ms=latency_ms,
            rejection_reason=rejection_reason,
        )
        return await insert_answer_run(pool, run)

    except Exception:
        logger.warning(
            "insert_refusal_answer_run: helper raised (non-fatal)",
            exc_info=True,
        )
        return None


async def batch_insert_retrieval_items(pool: object, items: list[object]) -> None:
    """Batch-insert rows into silver.answer_retrieval_items.

    Args:
        pool:  asyncpg Pool (app.state.pg_pool).
        items: List of AnswerRetrievalItemCreate instances.

    Failures are logged at WARNING level and never raised.
    Each row is {answer_run_id, workspace_id, stage, source_store, scores, flags}.
    """
    if pool is None or not items:
        return

    sql = """
        INSERT INTO silver.answer_retrieval_items (
            answer_run_id,
            workspace_id,
            stage,
            source_store,
            document_revision_id,
            passage_id,
            candidate_ref,
            retriever_score,
            reranker_score,
            rrf_rank,
            rrf_score,
            included_in_context,
            used_in_citation
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
        )
    """

    try:
        rows: list[tuple] = []
        for item in items:
            candidate_ref = getattr(item, "candidate_ref", None)
            candidate_ref_json = (
                json.dumps(candidate_ref) if candidate_ref is not None else None
            )
            doc_rev_id = getattr(item, "document_revision_id", None)
            passage_id = getattr(item, "passage_id", None)
            rows.append((
                str(getattr(item, "answer_run_id", "")),
                str(getattr(item, "workspace_id", "")),
                getattr(item, "stage", "retrieved"),
                getattr(item, "source_store", "qdrant"),
                str(doc_rev_id) if doc_rev_id else None,
                str(passage_id) if passage_id else None,
                candidate_ref_json,
                getattr(item, "retriever_score", None),
                getattr(item, "reranker_score", None),
                getattr(item, "rrf_rank", None),
                getattr(item, "rrf_score", None),
                getattr(item, "included_in_context", False),
                getattr(item, "used_in_citation", False),
            ))

        async with pool.acquire() as conn:  # type: ignore[union-attr]
            await conn.executemany(sql, rows)

        logger.debug(
            "batch_insert_retrieval_items: inserted %d rows (stage=%s)",
            len(items),
            getattr(items[0], "stage", "?") if items else "?",
        )

    except Exception:
        logger.warning(
            "batch_insert_retrieval_items: batch INSERT failed (non-fatal — observability write skipped)",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Module 6 Phase B Chunk 1 — citation_items / citation_spans stubs
#
# These function signatures define the import surface that Chunk 2 will
# implement.  Calling them before Chunk 2 lands raises NotImplementedError.
# The orchestrator must NOT call these in Chunk 1 (no rows in either table
# until the span resolver is wired).
# ---------------------------------------------------------------------------


async def insert_citation_item(pool: object, item: object) -> "UUID | None":
    """Insert one row into silver.answer_citation_items.

    Args:
        pool: asyncpg Pool (app.state.pg_pool).
        item: AnswerCitationItemCreate instance.

    Returns:
        The generated answer_citation_item_id UUID, or None on failure.
        Failures are logged at WARNING level and never raised.

    Note:
        Implemented in Module 6 Phase B Chunk 2.
        Called once per unique resolved marker per answer run after span
        resolution.  The returned UUID is used to back-fill
        answer_citation_spans.answer_citation_item_id.
    """
    if pool is None:
        logger.warning("insert_citation_item: pg_pool is None — skipping INSERT")
        return None

    sql = """
        INSERT INTO silver.answer_citation_items (
            answer_run_id,
            workspace_id,
            evidence_id,
            passage_id,
            marker_text,
            source_store,
            confidence,
            rejection_reason
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8
        )
        RETURNING answer_citation_item_id
    """

    try:
        evidence_id = getattr(item, "evidence_id", None)
        passage_id = getattr(item, "passage_id", None)

        async with pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(
                sql,
                str(getattr(item, "answer_run_id", "")),
                str(getattr(item, "workspace_id", "")),
                str(evidence_id) if evidence_id is not None else None,
                str(passage_id) if passage_id is not None else None,
                getattr(item, "marker_text", ""),
                getattr(item, "source_store", None),
                getattr(item, "confidence", None),
                getattr(item, "rejection_reason", None),
            )

        if row:
            item_id = UUID(str(row["answer_citation_item_id"]))
            logger.debug(
                "insert_citation_item: inserted marker=%s item_id=%s",
                getattr(item, "marker_text", "?"),
                item_id,
            )
            return item_id

    except Exception:
        logger.warning(
            "insert_citation_item: INSERT failed (non-fatal — citation audit write skipped)",
            exc_info=True,
        )
    return None


async def insert_citation_items_with_spans(
    pool: object,
    items: list[object],
    spans_by_item: list[list[object]],
) -> list["UUID"]:
    """Atomically insert citation items + their spans in a single transaction.

    items[i] produces answer_citation_item_id = returned_ids[i]; spans_by_item[i]
    is then written with answer_citation_item_id = returned_ids[i].

    If any INSERT fails, the entire batch rolls back — no orphan rows.

    Args:
        pool:          asyncpg Pool (app.state.pg_pool).
        items:         Ordered list of AnswerCitationItemCreate instances.
        spans_by_item: Parallel list of span lists; spans_by_item[i] is all
                       AnswerCitationSpanCreate objects for items[i].

    Returns:
        List of generated answer_citation_item_id UUIDs in input order.
        Returns [] if pool is None or items is empty.
        On transaction failure, the entire batch is rolled back and [] is
        returned (all-or-nothing atomicity guarantee).

    C3: Module 6 Phase B Chunk 3 — replaces the non-atomic two-call pattern
    (insert_citation_items + batch_insert_citation_spans) that left orphan
    items with no spans on crash between the two calls.
    """
    if pool is None:
        logger.warning(
            "insert_citation_items_with_spans: pg_pool is None — skipping INSERT"
        )
        return []
    if not items:
        return []

    item_sql = """
        INSERT INTO silver.answer_citation_items (
            answer_run_id,
            workspace_id,
            evidence_id,
            passage_id,
            marker_text,
            source_store,
            confidence,
            rejection_reason
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8
        )
        RETURNING answer_citation_item_id
    """
    span_sql = """
        INSERT INTO silver.answer_citation_spans (
            answer_run_id,
            answer_citation_item_id,
            workspace_id,
            span_start,
            span_end
        ) VALUES (
            $1, $2, $3, $4, $5
        )
    """

    try:
        async with pool.acquire() as conn:  # type: ignore[union-attr]
            async with conn.transaction():
                item_ids: list[UUID] = []

                for item in items:
                    evidence_id = getattr(item, "evidence_id", None)
                    passage_id = getattr(item, "passage_id", None)
                    row = await conn.fetchrow(
                        item_sql,
                        str(getattr(item, "answer_run_id", "")),
                        str(getattr(item, "workspace_id", "")),
                        str(evidence_id) if evidence_id is not None else None,
                        str(passage_id) if passage_id is not None else None,
                        getattr(item, "marker_text", ""),
                        getattr(item, "source_store", None),
                        getattr(item, "confidence", None),
                        getattr(item, "rejection_reason", None),
                    )
                    item_ids.append(UUID(str(row["answer_citation_item_id"])))

                for item_id, spans in zip(item_ids, spans_by_item):
                    for span in spans:
                        await conn.execute(
                            span_sql,
                            str(getattr(span, "answer_run_id", "")),
                            str(item_id),
                            str(getattr(span, "workspace_id", "")),
                            getattr(span, "span_start", 0),
                            getattr(span, "span_end", 0),
                        )

        logger.debug(
            "insert_citation_items_with_spans: committed %d item(s) with spans "
            "(transactional — no orphans possible)",
            len(item_ids),
        )
        return item_ids

    except Exception:
        logger.warning(
            "insert_citation_items_with_spans: transaction failed — rolled back "
            "(non-fatal — citation audit write skipped)",
            exc_info=True,
        )
        return []


async def batch_insert_citation_spans(pool: object, spans: list[object]) -> None:
    """Batch-insert rows into silver.answer_citation_spans.

    Args:
        pool:  asyncpg Pool (app.state.pg_pool).
        spans: List of AnswerCitationSpanCreate instances.  The
               ``answer_citation_item_id`` field must be the real UUID
               returned from ``insert_citation_items_with_spans`` — not the
               sentinel nil UUID emitted by ``resolve_spans``.

    Failures are logged at WARNING level and never raised.
    """
    if pool is None or not spans:
        return

    # Filter out spans whose item_id is still the nil sentinel (insert failed).
    NIL_STR = "00000000-0000-0000-0000-000000000000"
    valid_spans = [
        s for s in spans
        if str(getattr(s, "answer_citation_item_id", NIL_STR)) != NIL_STR
    ]
    if not valid_spans:
        logger.debug("batch_insert_citation_spans: no valid spans to insert (all sentinels)")
        return

    sql = """
        INSERT INTO silver.answer_citation_spans (
            answer_run_id,
            answer_citation_item_id,
            workspace_id,
            span_start,
            span_end
        ) VALUES (
            $1, $2, $3, $4, $5
        )
    """

    try:
        rows: list[tuple] = []
        for span in valid_spans:
            rows.append((
                str(getattr(span, "answer_run_id", "")),
                str(getattr(span, "answer_citation_item_id", "")),
                str(getattr(span, "workspace_id", "")),
                getattr(span, "span_start", 0),
                getattr(span, "span_end", 0),
            ))

        async with pool.acquire() as conn:  # type: ignore[union-attr]
            await conn.executemany(sql, rows)

        logger.debug(
            "batch_insert_citation_spans: inserted %d span row(s)",
            len(valid_spans),
        )

    except Exception:
        logger.warning(
            "batch_insert_citation_spans: batch INSERT failed (non-fatal — span audit write skipped)",
            exc_info=True,
        )
