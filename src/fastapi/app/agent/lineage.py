"""Lineage assembly — Phase 1 / Step 1.5.

Builds a :class:`app.models.lineage.LineagePayload` from the orchestrator's
in-scope state. The orchestrator persists this payload atomically with the
answer-run row; when ``settings.GEO_ANSWER_OIUR_ENABLED=True`` the write
is fail-closed (lineage failure raises and the answer is not returned).

This module is intentionally pure — no I/O, no settings reads. Callers
hand in the data they already have and get back a validated payload.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable
from uuid import UUID

from app.agent.schemas import GEO_ANSWER_SCHEMA_VERSION, GeoAnswer
from app.models.lineage import (
    FiltersApplied,
    LineagePayload,
    QaQcFiltersApplied,
    RetrievedSource,
    SourceTypeLiteral,
)
from app.models.rag import Citation, GeoRAGResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source-type normalisation
# ---------------------------------------------------------------------------


def _normalise_source_type(raw: str | None) -> SourceTypeLiteral:
    """Map an orchestrator-side store label to the lineage source_type enum."""
    if not raw:
        return "other"
    low = raw.lower()
    if low in {"qdrant", "neo4j", "postgis", "public_geoscience", "hybrid"}:
        return low  # type: ignore[return-value]
    if "public" in low or "pgeo" in low:
        return "public_geoscience"
    if low in {"silver", "gold"}:
        return "postgis"
    return "other"


# ---------------------------------------------------------------------------
# Cited-marker resolution
# ---------------------------------------------------------------------------


def _cited_chunk_ids(citations: Iterable[Citation]) -> set[str]:
    """Return the set of source_chunk_ids that actually appear in the answer."""
    out: set[str] = set()
    for c in citations:
        if c.source_chunk_id:
            out.add(c.source_chunk_id)
    return out


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_lineage_payload(
    *,
    response: GeoRAGResponse,
    fused_candidates: Iterable[Any] = (),
    filters: FiltersApplied | None = None,
    qaqc_filters: QaQcFiltersApplied | None = None,
    session_id: UUID | None = None,
) -> LineagePayload:
    """Assemble a :class:`LineagePayload` for one answer run.

    Args:
        response: The finalised :class:`GeoRAGResponse` carrying the
            answer text + citations + (optionally) the OIUR ``geo_answer``.
        fused_candidates: Iterable of scored candidate wrappers from the
            orchestrator's retrieval-fusion stage. Each item is expected to
            expose ``candidate.store``, ``candidate.canonical_id`` /
            ``candidate.payload.chunk_id``, and a numeric ``score``. Items
            that don't match this shape are skipped (best-effort builder).
        filters: Scope filters active at query time. ``None`` becomes the
            default empty payload.
        qaqc_filters: QA/QC exclusions active at query time. ``None``
            becomes the default empty payload.
        session_id: Optional session UUID for chat-mode replay grouping.

    Returns:
        A validated :class:`LineagePayload`. ``answer_schema_version`` is
        populated when ``response.geo_answer`` is non-None (signal that
        the OIUR contract was active).
    """
    cited = _cited_chunk_ids(response.citations)
    retrieved: list[RetrievedSource] = []

    for sc in fused_candidates:
        candidate = getattr(sc, "candidate", sc)
        store = getattr(candidate, "store", None)
        # Try chunk_id from payload first (Qdrant), then canonical_id (graph/structured).
        chunk_id: str | None = None
        payload = getattr(candidate, "payload", None)
        if payload is not None:
            chunk_id = getattr(payload, "chunk_id", None)
        if not chunk_id:
            cand_canonical = getattr(candidate, "canonical_id", None)
            if cand_canonical:
                chunk_id = str(cand_canonical)
        # pdf_id is optional — only Qdrant chunks reliably surface it.
        pdf_id_raw = getattr(payload, "pdf_id", None) if payload is not None else None
        pdf_id: UUID | None = None
        if pdf_id_raw:
            try:
                pdf_id = UUID(str(pdf_id_raw))
            except (ValueError, TypeError):
                pdf_id = None
        # Score: prefer the wrapper's rerank/RRF score, fall back to candidate.score.
        score: float | None = None
        for attr in ("rrf_score", "score"):
            v = getattr(sc, attr, None)
            if v is None and sc is not candidate:
                v = getattr(candidate, attr, None)
            if isinstance(v, (int, float)):
                score = float(v)
                break

        try:
            retrieved.append(
                RetrievedSource(
                    source_type=_normalise_source_type(store),
                    chunk_id=chunk_id,
                    pdf_id=pdf_id,
                    score=score,
                    cited=bool(chunk_id and chunk_id in cited),
                )
            )
        except Exception:
            logger.debug(
                "build_lineage_payload: skipped malformed candidate", exc_info=True
            )
            continue

    return LineagePayload(
        session_id=session_id,
        retrieved_sources=retrieved,
        filters_applied=filters or FiltersApplied(),
        qaqc_filters_applied=qaqc_filters or QaQcFiltersApplied(),
        answer_schema_version=(
            GEO_ANSWER_SCHEMA_VERSION if isinstance(response.geo_answer, GeoAnswer) else None
        ),
    )


__all__ = ["build_lineage_payload"]
