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
Uses ``strict_word_similarity($1, text) > 0.3`` ranked DESC and capped to
the caller's ``limit``. Filters by workspace_id (the RLS policy on
silver.document_passages applies, but we also do an explicit filter so
the EXPLAIN shows index usage). Returns ``ScoredPoint``-shaped dicts so
the orchestrator's downstream code doesn't need to branch on shape.

Status: NOT WIRED, and there is a prerequisite before it can be
------------------------------------------------------------------
``safe_hybrid_query`` has no call sites. ``search_documents`` calls
``hybrid_query`` directly and, on a Qdrant failure, returns an empty
result whose ``data_source`` carries "(error)" or "(timeout)" -- which
``response_assembler._collect_degraded_sources`` turns into a
``degraded_sources`` label, so an outage is at least VISIBLE today. What
the user does not get is results.

Before this module is wired in, silver.document_passages needs a GIN
trigram index on ``text``. No migration creates one (four other tables
have trigram indexes; this one does not), so the query below is a
sequential scan computing a trigram score per row over the largest table
in the system -- run at the moment Qdrant is already unavailable. Wiring
it without the index trades a degraded search for a degraded database.

If nobody intends to add the index, delete this module rather than keep a
safety net that cannot deploy. That call needs an owner: deleting it also
removes tests, which CLAUDE.md puts behind approval.

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
from app.db import bind_workspace_scope  # noqa: E402


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
    # Two corrections, a day apart, both of which independently guaranteed
    # this fallback returned nothing.
    #
    # 2026-08-21 — column names. The query named `passage_text` and
    #   `document_revision_id`; silver.document_passages has neither
    #   (verified against a freshly-migrated schema AND live production,
    #   28 columns). They are `text` and `document_id`. Every call raised
    #   UndefinedColumnError into the blanket handler below and returned
    #   []. The payload KEYS keep their original names, so callers reading
    #   `passage_text` off the payload are unaffected.
    #
    # 2026-08-22 — the scoring function. `similarity()` is a symmetric
    #   Jaccard over both trigram sets, so a 40-character query against a
    #   5,000-character passage scores about 0.008 on a perfect substring
    #   match — an order of magnitude under the 0.1 gate that was there.
    #   Nothing could ever have cleared it.
    #
    #   `strict_word_similarity(needle, haystack)` scores the needle
    #   against the best word-aligned extent of the haystack instead of
    #   against its whole length, which is the comparison this always
    #   meant. The ARGUMENT ORDER is the fix, not just the name: passing
    #   the passage first would search the query for an extent matching
    #   the passage and reproduce the same failure.
    #
    #   0.3 rather than pg_trgm's 0.5 default: this runs only when
    #   semantic search is already gone, so a loose lexical hit ranked
    #   below a good one beats an empty page.
    sql = """
        SELECT
            passage_id::text AS id,
            strict_word_similarity($1, text) AS score,
            jsonb_build_object(
              'workspace_id', workspace_id::text,
              'document_id', document_id::text,
              'passage_text', text,
              'page_number', page_number,
              'ordinal', ordinal
            ) AS payload
          FROM silver.document_passages
         WHERE workspace_id = $2::uuid
           AND strict_word_similarity($1, text) > 0.3
         ORDER BY score DESC
         LIMIT $3
    """
    try:
        async with pg_pool.acquire() as conn:
            # The transaction is NOT decorative. bind_workspace_scope
            # defaults to is_local=True (SET LOCAL), which PostgreSQL
            # DISCARDS outside a transaction block — so on a bare
            # pool.acquire() this "mandatory GUC" was never set at all.
            # document_passages' RLS policy is fail-closed
            # (NULLIF(current_setting(...), '')::uuid → NULL → no match),
            # so an unbound GUC means zero rows, not a leak — but zero rows
            # is exactly what a working fallback must not return.
            async with conn.transaction():
                await bind_workspace_scope(
                    conn, workspace_id=ws, site="qdrant_fallback",
                )
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


#: Lazily-constructed counter for _fire_fallback_metric. Declared BEFORE
#: the function that assigns it, because the previous ordering is what
#: made the bug below invisible: the sentinel sat at the bottom of the
#: file, thirty lines after the code whose correctness depended on it.
_QDRANT_FALLBACK_TOTAL: Any = None


def _fire_fallback_metric(collection: str) -> None:
    """Best-effort prometheus_client counter increment.

    FIXED 2026-08-21 (found by mypy: `"None" has no attribute "labels"`).
    This guarded the lazy singleton with a NameError check::

        try:
            _QDRANT_FALLBACK_TOTAL          # noqa: B018
        except NameError:
            _QDRANT_FALLBACK_TOTAL = Counter(...)

    while the module also did ``_QDRANT_FALLBACK_TOTAL = None`` at import
    time. The name therefore ALWAYS existed, NameError was never raised,
    the Counter was never built, and ``.labels`` was called on None --
    an AttributeError, which the enclosing ``except ImportError`` does not
    catch, so it reached the caller.

    Latent rather than live: this function has no callers today, the same
    as ``safe_hybrid_query`` below it. It would have become live on the
    first Qdrant outage after someone wired the fallback up, turning a
    degraded-but-working query path into a crash.

    The guard now checks the VALUE, which is what the sentinel encodes.
    """
    global _QDRANT_FALLBACK_TOTAL
    try:
        from prometheus_client import Counter  # noqa: PLC0415

        if _QDRANT_FALLBACK_TOTAL is None:
            # Registration is idempotent by name in prometheus_client, but
            # constructing it twice would still raise Duplicated
            # timeseries, hence the singleton.
            _QDRANT_FALLBACK_TOTAL = Counter(
                "georag_qdrant_fallback_total",
                "Number of times Qdrant was unavailable and the pg_trgm "
                "fallback served a query.",
                labelnames=("collection",),
            )
        _QDRANT_FALLBACK_TOTAL.labels(collection=collection).inc()
    except ImportError:
        # prometheus_client is optional. Anything else is a real fault and
        # must not be swallowed by a metrics helper.
        pass
