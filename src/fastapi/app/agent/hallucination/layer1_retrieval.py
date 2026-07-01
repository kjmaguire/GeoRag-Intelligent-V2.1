"""Layer 1 — Retrieval Quality Gate.

Architecture reference: Section 04i, Layer 1.

Purpose
-------
Drop low-relevance document chunks before they can ground LLM claims.
A chunk with a relevance_score below the threshold carries too little signal
to be worth citing — including it risks the agent over-interpreting a weak
match as a strong factual source.

This layer is applied INSIDE tool functions (search_documents in tools.py),
not as a Pydantic AI output_validator.  The rationale: it is better to filter
bad data before it reaches the LLM's context window at all, rather than
catching the downstream symptom after the LLM has already synthesised an
answer from bad data.

Usage
-----
Call ``filter_by_quality`` on any list of objects that have a ``relevance_score``
attribute (DocumentChunk instances) before returning them from a tool:

    from app.agent.hallucination.layer1_retrieval import filter_by_quality

    chunks = await _run_search()
    chunks = filter_by_quality(chunks, settings.RETRIEVAL_QUALITY_THRESHOLD)
    return DocumentSearchResult(chunks=chunks, count=len(chunks), ...)

If ALL chunks are dropped the tool returns an empty result list and the agent
must report "insufficient information" rather than fabricating an answer.
"""

from __future__ import annotations

import logging
from typing import Protocol, TypeVar, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol — any object with a relevance_score attribute qualifies
# ---------------------------------------------------------------------------


@runtime_checkable
class HasRelevanceScore(Protocol):
    """Protocol for objects that carry a relevance score.

    Both DocumentChunk (Qdrant results) and any future reranked result type
    can be passed to filter_by_quality as long as they implement this
    interface.
    """

    relevance_score: float


_T = TypeVar("_T", bound=HasRelevanceScore)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def filter_by_quality[T: HasRelevanceScore](results: list[_T], threshold: float) -> list[_T]:
    """Filter a list of scored results, dropping those below the quality threshold.

    Args:
        results:   List of objects with a ``relevance_score`` attribute (0–1).
        threshold: Minimum relevance score to retain (inclusive — results
                   with score exactly equal to threshold are kept).
                   Use ``settings.RETRIEVAL_QUALITY_THRESHOLD`` (default 0.5
                   as of R8; see scripts/sweep_retrieval_threshold.py for
                   the tuning history) unless you have a specific reason to
                   override it.

    Returns:
        Filtered list containing only results whose relevance_score >= threshold.
        Returns an empty list if all results fall below the threshold; callers
        must handle this case and return an empty tool result so the agent can
        report "insufficient information" rather than fabricating an answer.

    Side effects:
        Logs the number of results accepted and dropped at DEBUG level, and
        logs a WARNING if all results were dropped (empty return).
    """
    if not results:
        return results

    accepted: list[_T] = []
    dropped: list[_T] = []

    for item in results:
        if item.relevance_score >= threshold:
            accepted.append(item)
        else:
            dropped.append(item)

    if dropped:
        logger.debug(
            "layer1_retrieval: dropped %d/%d results below threshold %.2f "
            "(scores: %s)",
            len(dropped),
            len(results),
            threshold,
            ", ".join(f"{r.relevance_score:.3f}" for r in dropped),
        )

    if not accepted:
        logger.warning(
            "layer1_retrieval: ALL %d results dropped — scores below threshold %.2f. "
            "Returning empty list; agent will report 'insufficient information'.",
            len(results),
            threshold,
        )
    else:
        logger.debug(
            "layer1_retrieval: %d/%d results passed quality gate (threshold %.2f)",
            len(accepted),
            len(results),
            threshold,
        )

    return accepted
