"""Qdrant → pg_trgm graceful-degradation wrapper (Eval 16 P1 follow-up).

When the Qdrant cluster is unreachable, slow, or returning errors, we
fall back to a lexical search over ``silver.document_passages`` using
PostgreSQL's pg_trgm extension. The fallback is intentionally a degraded
experience — semantic similarity is gone — but the user still gets
relevant passages instead of a hard 500.

Contract
--------
``safe_hybrid_query`` accepts the same arguments as ``hybrid_query``
plus a ``query_text`` (needed for the lexical fallback path), and returns
a tuple ``(results, degraded)``:

  - ``degraded=False`` → results came from Qdrant; semantic + sparse fusion.
  - ``degraded=True``  → results came from pg_trgm; caller is expected to
    surface a UX banner ("Results may be less relevant — semantic
    search is temporarily unavailable").

Failure detection
-----------------
We treat the following as Qdrant unavailability:
  - any subclass of ``qdrant_client.http.exceptions.UnexpectedResponse``
  - ``httpx.HTTPError`` (transport-level)
  - ``asyncio.TimeoutError`` (the caller's wait_for cap)
  - any other exception whose class name contains "Qdrant" or "Connect"

Anything else propagates — those are bugs, not service availability.

pg_trgm query
-------------
Uses ``similarity(passage_text, $1) > 0.1`` ranked DESC and capped to the
caller's ``limit``. Filters by workspace_id (the RLS policy on
silver.document_passages applies, but we also do an explicit filter so
the EXPLAIN shows index usage). Returns ``ScoredPoint``-shaped dicts so
the orchestrator's downstream code doesn't need to branch on shape.

Metrics
-------
Each fallback fires the Prometheus counter ``QDRANT_FALLBACK_TOTAL``
labelled by collection. The OPS alert ``QdrantFallbackRateHigh`` is
expected to be added to v3.1-supplemental-alerts.yml in a follow-up.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)


# Sentinel base class catch — qdrant_client's exception hierarchy. We
# import lazily to avoid the dep at module-import time (the fallback
# module is imported in the hot path; qdrant_client adds ~80ms cold).
from app.db import bind_workspace_scope


def _is_qdrant_unavailability(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, httpx.HTTPError, ConnectionError)):
        return True
    name = type(exc).__name__
    return "Qdrant" in name or "Connect" in name or "Transport" in name


async def safe_hybrid_query(
    *,
    qdrant_client: Any,
    pg_pool: Any,
    collection: str,
    query_text: str,
    query_dense: list[float],
    query_sparse: dict[int, float],
    workspace_id: str | UUID,
    limit: int = 50,
    additional_filter: Any | None = None,
    sparse_boost_factor: float = 1.0,
) -> tuple[list[dict[str, Any]], bool]:
    """Hybrid query with Qdrant-failure fallback to pg_trgm.

    Returns ``(results, degraded)``. ``degraded=True`` signals the
    caller to surface a "search quality reduced" banner.
    """
    # Try Qdrant first.
    try:
        from app.services.qdrant_service import hybrid_query  # noqa: PLC0415

        scored = await hybrid_query(
            client=qdrant_client,
            collection=collection,
            query_dense=query_dense,
            query_sparse=query_sparse,
            workspace_id=workspace_id,
            limit=limit,
            additional_filter=additional_filter,
            sparse_boost_factor=sparse_boost_factor,
        )
        # Normalise to the dict shape the fallback emits so callers can
        # be shape-agnostic. Qdrant ScoredPoint is duck-friendly.
        out = [
            {
                "id": str(getattr(sp, "id", "")),
                "score": float(getattr(sp, "score", 0.0)),
                "payload": getattr(sp, "payload", None) or {},
            }
            for sp in scored
        ]
        return out, False
    except Exception as exc:  # noqa: BLE001
        if not _is_qdrant_unavailability(exc):
            # Programming error — let it propagate so the test suite
            # and Sentry catch it. We only fall back on infrastructure
            # failure, not on bugs.
            raise

        logger.warning(
            "Qdrant unavailable (%s: %s) — falling back to pg_trgm "
            "for collection=%s. Surface degraded=True in the response.",
            type(exc).__name__, str(exc)[:200], collection,
        )
        try:
            _fire_fallback_metric(collection)
        except Exception:
            logger.debug("qdrant fallback metric emit failed", exc_info=True)

    # ── pg_trgm fallback ────────────────────────────────────────────
    return await _pg_trgm_search(
        pg_pool=pg_pool,
        query_text=query_text,
        workspace_id=workspace_id,
        limit=limit,
    ), True


async def _pg_trgm_search(
    *,
    pg_pool: Any,
    query_text: str,
    workspace_id: str | UUID,
    limit: int,
) -> list[dict[str, Any]]:
    """Lexical fallback against silver.document_passages.

    Requires the pg_trgm extension (created at init time per
    docker/postgresql/init scripts).
    """
    ws = str(workspace_id)
    sql = """
        SELECT
            passage_id::text AS id,
            similarity(passage_text, $1) AS score,
            jsonb_build_object(
              'workspace_id', workspace_id::text,
              'document_revision_id', document_revision_id::text,
              'passage_text', passage_text,
              'page_number', page_number,
              'ordinal', ordinal
            ) AS payload
          FROM silver.document_passages
         WHERE workspace_id = $2::uuid
           AND similarity(passage_text, $1) > 0.1
         ORDER BY score DESC
         LIMIT $3
    """
    try:
        async with pg_pool.acquire() as conn:
            # Mandatory GUC for the RLS policy on document_passages.
            await bind_workspace_scope(conn, workspace_id=ws, site="qdrant_fallback")
            rows = await conn.fetch(sql, query_text, ws, limit)
        return [
            {
                "id": r["id"],
                "score": float(r["score"]),
                "payload": dict(r["payload"]) if r["payload"] else {},
            }
            for r in rows
        ]
    except Exception:
        logger.exception(
            "pg_trgm fallback also failed — returning empty result set"
        )
        return []


def _fire_fallback_metric(collection: str) -> None:
    """Best-effort prometheus_client counter increment."""
    try:
        from prometheus_client import Counter  # noqa: PLC0415

        # Module-level lazy singleton; the registration is idempotent
        # because prometheus_client deduplicates by name.
        global _QDRANT_FALLBACK_TOTAL
        try:
            _QDRANT_FALLBACK_TOTAL  # type: ignore[name-defined]
        except NameError:
            _QDRANT_FALLBACK_TOTAL = Counter(  # type: ignore[assignment]
                "georag_qdrant_fallback_total",
                "Number of times Qdrant was unavailable and the pg_trgm "
                "fallback served a query.",
                labelnames=("collection",),
            )
        _QDRANT_FALLBACK_TOTAL.labels(collection=collection).inc()
    except ImportError:
        pass


_QDRANT_FALLBACK_TOTAL = None  # type: ignore[assignment]
