"""Structured refusal payload builder — Module 6 Phase B Chunk 4a.

Implements spec B4: the full ``reason_code``/``searched``/``missing`` shape
that Module 7's refusal UI branches on.

Architecture references
-----------------------
  Module spec 06-citation-hallucination-guards.md §6 B4
  georag-architecture-addendum-v1.10.html §04j (evidence model)
  Section 04i hallucination prevention (layer 4 — refusal meta-guard)

Design
------
``build_refusal_payload()`` is async because it queries:
  - ``silver.answer_retrieval_items`` for candidates_considered count
  - ``silver.answer_retrieval_items`` for top-3 nearest candidates
  - ``silver.answer_runs`` for query_class and partial_failure_details

All queries use the asyncpg pool from the caller. If the pool is None or any
query fails, the builder falls back to the guard-only context that is always
available synchronously. This ensures a valid ``searched`` block is always
present (Invariant 2: refusal is a first-class product state; empty refusals
are bugs).

reason_code mapping
-------------------
  numeric guard failed           → "guard_numeric_fail"
  entity guard failed            → "guard_entity_fail"
  completeness guard failed      → "guard_completeness_fail"
  no guards fired + 0 markers    → "insufficient_evidence"
  LLM backend exhausted (FB-02)  → "llm_unavailable"
  overall asyncio.timeout hit    → "budget_exhausted"

The caller selects the appropriate factory function:
  - ``build_guard_refusal_payload()`` — guard failures (most common)
  - ``build_llm_unavailable_payload()`` — FB-02 (all backends exhausted)
  - ``build_budget_exhausted_payload()`` — TIMEOUT_GATHER_S exceeded
  - ``build_insufficient_evidence_payload()`` — 0 markers, no guard firing

All return the same ``RefusalPayload`` shape so Module 7 only branches on
``reason_code``, not on which function was called.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public type
# ---------------------------------------------------------------------------

RefusalReasonCode = str  # narrowed by callers to the Literal from answer_run.py

_STORES_QUERIED_FALLBACK: list[str] = ["qdrant", "neo4j", "postgis"]

# Max nearest candidates to include in the missing block.
_MAX_NEAREST_CANDIDATES = 3

# Preview text length in characters for nearest-candidate snippets.
_PREVIEW_LEN = 120


# ---------------------------------------------------------------------------
# Internal DB helpers
# ---------------------------------------------------------------------------


async def _fetch_candidates_count(pg_pool: object, answer_run_id: UUID) -> int:
    """SELECT COUNT(*) from answer_retrieval_items at stage='retrieved'."""
    sql = """
        SELECT count(*)
        FROM silver.answer_retrieval_items
        WHERE answer_run_id = $1 AND stage = 'retrieved'
    """
    try:
        async with pg_pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(sql, answer_run_id)
        return int(row["count"]) if row else 0
    except Exception:
        logger.warning(
            "refusal_builder: candidates_count query failed (non-fatal)",
            exc_info=True,
        )
        return 0


async def _fetch_nearest_candidates(
    pg_pool: object, answer_run_id: UUID
) -> list[dict[str, Any]]:
    """Top-3 answer_retrieval_items ordered by reranker_score DESC (rrf_score fallback)."""
    sql = """
        SELECT
            ari.source_store,
            ari.reranker_score,
            ari.rrf_score,
            ari.passage_id,
            ari.candidate_ref,
            aci.marker_text
        FROM silver.answer_retrieval_items ari
        LEFT JOIN silver.answer_citation_items aci
            ON aci.answer_run_id = ari.answer_run_id
            AND aci.passage_id = ari.passage_id
        WHERE ari.answer_run_id = $1
        ORDER BY
            COALESCE(ari.reranker_score, ari.rrf_score, 0) DESC
        LIMIT $2
    """
    try:
        async with pg_pool.acquire() as conn:  # type: ignore[union-attr]
            rows = await conn.fetch(sql, answer_run_id, _MAX_NEAREST_CANDIDATES)
        candidates: list[dict[str, Any]] = []
        for i, row in enumerate(rows):
            # Build a marker label: prefer real marker_text, else synthesise.
            marker_text = row["marker_text"]
            if not marker_text:
                store = row["source_store"] or "unknown"
                marker_text = f"[{store.upper()}:{i + 1}]"

            # Relevance score: prefer reranker, then rrf.
            relevance = row["reranker_score"] or row["rrf_score"] or 0.0

            # Preview: pull first _PREVIEW_LEN chars from candidate_ref JSONB.
            preview = ""
            cref = row["candidate_ref"]
            if cref:
                try:
                    cref_dict = json.loads(cref) if isinstance(cref, str) else cref
                    # Best-effort: grab any text-like field.
                    for key in ("text", "chunk_text", "title", "description"):
                        val = cref_dict.get(key, "")
                        if val:
                            preview = str(val)[:_PREVIEW_LEN]
                            break
                    if not preview:
                        preview = str(cref_dict)[:_PREVIEW_LEN]
                except Exception:
                    preview = str(cref)[:_PREVIEW_LEN]

            candidates.append(
                {
                    "marker": marker_text,
                    "source_store": row["source_store"] or "unknown",
                    "relevance_score": round(float(relevance), 4),
                    "preview": preview,
                }
            )
        return candidates
    except Exception:
        logger.warning(
            "refusal_builder: nearest_candidates query failed (non-fatal)",
            exc_info=True,
        )
        return []


async def _fetch_query_class(pg_pool: object, answer_run_id: UUID) -> str:
    """Fetch answer_runs.query_class for this run."""
    sql = "SELECT query_class FROM silver.answer_runs WHERE answer_run_id = $1"
    try:
        async with pg_pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(sql, answer_run_id)
        return str(row["query_class"]) if row else "unknown"
    except Exception:
        logger.warning(
            "refusal_builder: query_class fetch failed (non-fatal)",
            exc_info=True,
        )
        return "unknown"


async def _fetch_stores_queried(pg_pool: object, answer_run_id: UUID) -> list[str]:
    """Derive stores queried from partial_failure_details on answer_runs.

    Falls back to the canonical default [qdrant, neo4j, postgis] when the
    column is absent or unparseable.
    """
    sql = "SELECT partial_failure_details FROM silver.answer_runs WHERE answer_run_id = $1"
    try:
        async with pg_pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(sql, answer_run_id)
        if not row or not row["partial_failure_details"]:
            return list(_STORES_QUERIED_FALLBACK)
        details = row["partial_failure_details"]
        if isinstance(details, str):
            details = json.loads(details)
        # partial_failure_details is a list of [store, reason] pairs.
        if isinstance(details, list):
            stores_with_failures = {
                item[0] for item in details if isinstance(item, (list, tuple)) and item
            }
            # All canonical stores minus the failed ones are "succeeded";
            # we report all that were attempted (success + partial failure).
            all_canonical = set(_STORES_QUERIED_FALLBACK)
            return sorted(all_canonical | stores_with_failures)
        return list(_STORES_QUERIED_FALLBACK)
    except Exception:
        logger.warning(
            "refusal_builder: stores_queried fetch failed (non-fatal)",
            exc_info=True,
        )
        return list(_STORES_QUERIED_FALLBACK)


# ---------------------------------------------------------------------------
# reason_code → guard_name helpers
# ---------------------------------------------------------------------------


def _reason_code_from_guard_names(guard_names: list[str]) -> str:
    """Map the first failed guard_name to a stable reason_code enum value.

    Priority: numeric > entity > completeness.  If multiple guards failed,
    we pick the most specific one.  Caller can use the full ``failed_guards``
    list for the rejection_reason column.
    """
    if "numeric" in guard_names:
        return "guard_numeric_fail"
    if "entity" in guard_names:
        return "guard_entity_fail"
    if "completeness" in guard_names:
        return "guard_completeness_fail"
    return "guard_completeness_fail"  # safe default if guard_names is unexpected


def _what_was_needed(
    reason_code: str,
    guard_bundle: object | None = None,
    query_context: str | None = None,
) -> str:
    """Derive a human-readable 'what was needed' string for the missing block.

    For guard failures: describe the failing tokens/entities/sentences.
    For other codes: canonical description.
    """
    if reason_code == "guard_numeric_fail" and guard_bundle is not None:
        tokens = getattr(guard_bundle.numeric, "failed_tokens", [])[:3]
        if tokens:
            return (
                f"Verified numerical values in the corpus to ground: "
                f"{', '.join(str(t) for t in tokens)}"
            )
        return "Verified numerical values in the corpus to ground ungrounded numbers in the answer."

    if reason_code == "guard_entity_fail" and guard_bundle is not None:
        entities = getattr(guard_bundle.entity, "failed_entities", [])[:3]
        if entities:
            return (
                f"Confirmed entity names in the corpus: "
                f"{', '.join(str(e) for e in entities)}"
            )
        return "Confirmed entity names (drill holes, formations, projects) referenced in the answer."

    if reason_code == "guard_completeness_fail" and guard_bundle is not None:
        uncited = getattr(guard_bundle.completeness, "uncited_sentences", [])[:2]
        if uncited:
            snippet = uncited[0][:120]
            return (
                f"Citation markers on all factual claims. "
                f"First uncited sentence: \"{snippet}\""
            )
        return "Citation markers on every factual claim sentence in the answer."

    if reason_code == "insufficient_evidence":
        if query_context:
            return f"A citable passage supporting: \"{query_context[:120]}\""
        return "A citable passage grounding the queried claim in the project corpus."

    if reason_code == "llm_unavailable":
        return "A functioning LLM backend to synthesise the answer."

    if reason_code == "budget_exhausted":
        return "A query that completes within the response time budget."

    return "Sufficient grounded evidence to support the answer."


# ---------------------------------------------------------------------------
# Public factory functions
# ---------------------------------------------------------------------------


async def build_guard_refusal_payload(
    *,
    guard_bundle: object,
    answer_run_id: UUID | None = None,
    pg_pool: object = None,
    query_context: str | None = None,
) -> dict[str, Any]:
    """Build the full B4 refusal payload for a guard failure.

    Args:
        guard_bundle:   GuardBundle from evaluate_guards(). Provides
                        failed_guards list and per-guard failure details.
        answer_run_id:  UUID of the answer_runs row (None if INSERT failed).
        pg_pool:        asyncpg Pool for DB lookups (None → fallback values).
        query_context:  Raw query text — used to populate what_was_needed.
                        Must be ≤140 chars in the refusal (truncated here).

    Returns:
        dict conforming to spec B4 shape.
    """
    failed_guards = getattr(guard_bundle, "failed_guards", [])
    guard_names = [getattr(g, "guard_name", "") for g in failed_guards]
    reason_code = _reason_code_from_guard_names(guard_names)

    # DB lookups — all best-effort.
    candidates_count = 0
    nearest_candidates: list[dict[str, Any]] = []
    query_class = "unknown"
    stores_queried: list[str] = list(_STORES_QUERIED_FALLBACK)

    if pg_pool is not None and answer_run_id is not None:
        import asyncio  # noqa: PLC0415
        try:
            (
                candidates_count,
                nearest_candidates,
                query_class,
                stores_queried,
            ) = await asyncio.gather(
                _fetch_candidates_count(pg_pool, answer_run_id),
                _fetch_nearest_candidates(pg_pool, answer_run_id),
                _fetch_query_class(pg_pool, answer_run_id),
                _fetch_stores_queried(pg_pool, answer_run_id),
                return_exceptions=True,
            )
            # Replace exceptions with fallback values.
            if isinstance(candidates_count, BaseException):
                candidates_count = 0
            if isinstance(nearest_candidates, BaseException):
                nearest_candidates = []
            if isinstance(query_class, BaseException):
                query_class = "unknown"
            if isinstance(stores_queried, BaseException):
                stores_queried = list(_STORES_QUERIED_FALLBACK)
        except Exception:
            logger.warning(
                "refusal_builder: DB gather failed (non-fatal)", exc_info=True
            )

    what_was_needed = _what_was_needed(reason_code, guard_bundle, query_context)

    # Human-readable guard failure summary.
    guard_summary_parts: list[str] = []
    for g in failed_guards:
        name = getattr(g, "guard_name", "unknown")
        if name == "numeric":
            tokens = getattr(g, "failed_tokens", [])[:3]
            guard_summary_parts.append(
                f"numeric guard: {len(getattr(g, 'failed_tokens', []))} ungrounded number(s)"
                + (f" [{', '.join(str(t) for t in tokens)}]" if tokens else "")
            )
        elif name == "entity":
            guard_summary_parts.append(
                f"entity guard: {len(getattr(g, 'failed_entities', []))} unresolved entity(ies)"
            )
        elif name == "completeness":
            guard_summary_parts.append(
                f"completeness guard: {len(getattr(g, 'uncited_sentences', []))} uncited sentence(s)"
            )
        else:
            guard_summary_parts.append(f"{name} guard: failed")

    message = (
        "We can't answer this from your corpus. "
        "The answer failed citation quality checks: "
        + ("; ".join(guard_summary_parts) if guard_summary_parts else "guard failure")
        + "."
    )

    return {
        "type": "refusal",
        "reason_code": reason_code,
        "searched": {
            "stores_queried": stores_queried,
            "candidates_considered": int(candidates_count),
            "query_class": str(query_class),
        },
        "missing": {
            "what_was_needed": what_was_needed,
            "nearest_candidates": nearest_candidates,
        },
        "message": message,
        # Diagnostic — not rendered by Module 7 UI but useful for logs.
        "failed_guards": guard_names,
    }


async def build_llm_unavailable_payload(
    *,
    answer_run_id: UUID | None = None,
    pg_pool: object = None,
    backend_chain: list[str] | None = None,
) -> dict[str, Any]:
    """Build the B4 refusal payload for the FB-02 case (all backends exhausted).

    Args:
        answer_run_id:  UUID of the answer_runs row.
        pg_pool:        asyncpg Pool (for searched block).
        backend_chain:  List of backend attempts (e.g. ["ollama:failed:timeout"]).
    """
    query_class = "unknown"
    stores_queried: list[str] = list(_STORES_QUERIED_FALLBACK)

    if pg_pool is not None and answer_run_id is not None:
        import asyncio  # noqa: PLC0415
        try:
            query_class, stores_queried = await asyncio.gather(
                _fetch_query_class(pg_pool, answer_run_id),
                _fetch_stores_queried(pg_pool, answer_run_id),
                return_exceptions=True,
            )
            if isinstance(query_class, BaseException):
                query_class = "unknown"
            if isinstance(stores_queried, BaseException):
                stores_queried = list(_STORES_QUERIED_FALLBACK)
        except Exception:
            logger.warning(
                "refusal_builder: llm_unavailable DB gather failed (non-fatal)",
                exc_info=True,
            )

    chain_str = (
        " → ".join(backend_chain[:3]) if backend_chain else "all backends"
    )
    return {
        "type": "refusal",
        "reason_code": "llm_unavailable",
        "searched": {
            "stores_queried": stores_queried,
            "candidates_considered": 0,
            "query_class": str(query_class),
        },
        "missing": {
            "what_was_needed": _what_was_needed("llm_unavailable"),
            "nearest_candidates": [],
        },
        "message": (
            f"The language model is currently unavailable ({chain_str} failed). "
            "Please try again in a few minutes."
        ),
        "failed_guards": [],
    }


async def build_budget_exhausted_payload(
    *,
    answer_run_id: UUID | None = None,
    pg_pool: object = None,
) -> dict[str, Any]:
    """Build the B4 refusal payload for the TIMEOUT_GATHER_S exceeded case."""
    query_class = "unknown"
    stores_queried: list[str] = list(_STORES_QUERIED_FALLBACK)

    if pg_pool is not None and answer_run_id is not None:
        import asyncio  # noqa: PLC0415
        try:
            query_class, stores_queried = await asyncio.gather(
                _fetch_query_class(pg_pool, answer_run_id),
                _fetch_stores_queried(pg_pool, answer_run_id),
                return_exceptions=True,
            )
            if isinstance(query_class, BaseException):
                query_class = "unknown"
            if isinstance(stores_queried, BaseException):
                stores_queried = list(_STORES_QUERIED_FALLBACK)
        except Exception:
            logger.warning(
                "refusal_builder: budget_exhausted DB gather failed (non-fatal)",
                exc_info=True,
            )

    return {
        "type": "refusal",
        "reason_code": "budget_exhausted",
        "searched": {
            "stores_queried": stores_queried,
            "candidates_considered": 0,
            "query_class": str(query_class),
        },
        "missing": {
            "what_was_needed": _what_was_needed("budget_exhausted"),
            "nearest_candidates": [],
        },
        "message": (
            "The query timed out before an answer could be assembled. "
            "Try a more specific question or reduce the scope of the request."
        ),
        "failed_guards": [],
    }


def build_insufficient_evidence_payload(
    *,
    query_context: str | None = None,
    stores_queried: list[str] | None = None,
    candidates_considered: int = 0,
) -> dict[str, Any]:
    """Build the B4 refusal payload for the insufficient_evidence case.

    This is the synchronous variant: called when 0 markers were resolved and
    no guard fired (rare; no DB context available at the call site).
    """
    return {
        "type": "refusal",
        "reason_code": "insufficient_evidence",
        "searched": {
            "stores_queried": stores_queried or list(_STORES_QUERIED_FALLBACK),
            "candidates_considered": candidates_considered,
            "query_class": "unknown",
        },
        "missing": {
            "what_was_needed": _what_was_needed("insufficient_evidence", query_context=query_context),
            "nearest_candidates": [],
        },
        "message": "We can't answer this from your corpus. No matching passages were found.",
        "failed_guards": [],
    }
