"""Source-trust boost layer for retrieval ranking (§12.8).

Reads ``silver.source_trust_scores`` per chunk's source_document_id
and adjusts the retrieval score downstream of fusion (semantic +
lexical). Configurable boost weight (default 0.2) so the fusion
remains primary; trust modulates rather than dominates.

Phase H4 graduation — the boost layer now performs the real DB
lookup against ``silver.source_trust_scores`` and applies the
modulation formula. When the trust table is empty (which is the
state until the §12.7 ``train_source_trust`` workflow lands), every
source gets the ``fallback_trust`` value and the boost is a no-op —
which is the intended degraded behaviour.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


async def _trust_lookup(
    conn: asyncpg.Connection,
    source_document_ids: set[str],
    fallback_trust: float,
) -> dict[str, float]:
    """Bulk-fetch per-source trust scores; fall back where missing."""
    if not source_document_ids:
        return {}
    out: dict[str, float] = {sid: fallback_trust for sid in source_document_ids}
    try:
        rows = await conn.fetch(
            """
            SELECT source_document_id::text AS source_document_id, trust_score
              FROM silver.source_trust_scores
             WHERE source_document_id = ANY($1::uuid[])
            """,
            [sid for sid in source_document_ids],  # noqa: C416
        )
        for r in rows:
            out[r["source_document_id"]] = float(r["trust_score"] or fallback_trust)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "boost_by_trust: trust lookup failed (table may not be "
            "populated yet); using fallback. err=%s", exc,
        )
    return out


async def boost_by_trust(
    conn: asyncpg.Connection,
    *,
    workspace_id: UUID | str,
    retrieved_chunks: Sequence[dict[str, Any]],
    boost_weight: float = 0.2,
    fallback_trust: float = 0.5,
) -> list[dict[str, Any]]:
    """Adjust retrieval scores using per-source trust scores.

    Args:
        conn: asyncpg Connection scoped to workspace RLS.
        retrieved_chunks: list of chunk dicts post-fusion. Each must
            have ``source_document_id`` and ``score`` keys.
        boost_weight: multiplier on the trust delta. final_score =
            ``score * (1 + boost_weight * (trust - 0.5))``. Default 0.2.
        fallback_trust: trust score for sources lacking
            ``silver.source_trust_scores`` entry. Default 0.5 (neutral —
            no boost applied).

    Returns:
        Chunks list with ``score`` adjusted and ``source_trust``
        annotated. Input order preserved.
    """
    if not retrieved_chunks:
        return []

    source_ids: set[str] = set()
    for chunk in retrieved_chunks:
        sid = chunk.get("source_document_id")
        if sid:
            source_ids.add(str(sid))

    trust_map = await _trust_lookup(conn, source_ids, fallback_trust)

    out: list[dict[str, Any]] = []
    for chunk in retrieved_chunks:
        sid = chunk.get("source_document_id")
        trust = float(trust_map.get(str(sid), fallback_trust)) if sid else fallback_trust
        original_score = float(chunk.get("score", 0.0))
        # Final score formula per §12.8: score * (1 + boost * (trust - 0.5))
        boosted = original_score * (1.0 + boost_weight * (trust - 0.5))
        new_chunk = dict(chunk)
        new_chunk["score"] = boosted
        new_chunk["source_trust"] = trust
        new_chunk["pre_boost_score"] = original_score
        out.append(new_chunk)

    # Re-sort by boosted score so downstream consumers (the
    # in_context selector) see the new ordering.
    out.sort(key=lambda c: c["score"], reverse=True)
    logger.info(
        "boost_by_trust: workspace=%s chunks=%d unique_sources=%d boost_weight=%.2f",
        workspace_id, len(out), len(source_ids), boost_weight,
    )
    return out


__all__ = ["boost_by_trust"]
