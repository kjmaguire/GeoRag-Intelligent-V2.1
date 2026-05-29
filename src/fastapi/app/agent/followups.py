"""Post-answer follow-up suggestions (D3 — "Explore deeper").

Rule-based synthesis from the query + the assistant's assembled response
+ the tool_results the orchestrator gathered. Zero additional LLM calls:
we want follow-ups to appear at the same moment the answer does, not a
second later, and we want them to be deterministic + cheap.

Rendered by the frontend as clickable chips below the completed message.
Clicking sends the chip's text as a new user query.

Philosophy
----------
- Three follow-ups max — more becomes visual noise.
- Specific beats clever. "Tell me about XLS-24-10" reads better than
  "Deep-dive on the top hole."
- Don't repeat the original query shape. If the user asked "how many
  holes", don't offer "how many X" variants; offer a narrative, a
  single-hole deep-dive, and an aggregate/comparison.
- No follow-up when the response is a refusal — asking the user to
  re-engage with empty data is frustrating.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.agent.tools import (
    AssayDataResult,
    DocumentSearchResult,
    GraphTraversalResult,
    SpatialQueryResult,
)
from app.models.rag import GeoRAGResponse

logger = logging.getLogger(__name__)


_MAX_FOLLOWUPS = 3

_REFUSAL_MARKERS = (
    "i don't have",
    "i do not have",
    "not found",
    "no data",
    "insufficient",
    "does not exist",
    "doesn't exist",
    "language model is currently unavailable",
)


def _is_refusal(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _REFUSAL_MARKERS)


def _extract_cited_hole(tool_results: list[tuple[str, Any]]) -> str | None:
    """First hole_id to appear in a SpatialQueryResult, if any."""
    for _, result in tool_results:
        if isinstance(result, SpatialQueryResult) and result.collars:
            deepest = max(result.collars, key=lambda c: c.total_depth or 0)
            return getattr(deepest, "hole_id", None)
    return None


def _extract_cited_element(tool_results: list[tuple[str, Any]]) -> str | None:
    """Element from the first AssayDataResult, if any."""
    for _, result in tool_results:
        if isinstance(result, AssayDataResult) and getattr(result, "element", None):
            return result.element
    return None


def _extract_graph_entity(tool_results: list[tuple[str, Any]]) -> str | None:
    """Most-frequent entity name from a GraphTraversalResult, if any."""
    for _, result in tool_results:
        if isinstance(result, GraphTraversalResult) and result.entities:
            first = result.entities[0]
            return getattr(first, "name", None)
    return None


def _has_document_chunks(tool_results: list[tuple[str, Any]]) -> bool:
    return any(
        isinstance(r, DocumentSearchResult) and r.count > 0
        for _, r in tool_results
    )


def generate_followups(
    query: str,
    response: GeoRAGResponse,
    tool_results: list[tuple[str, Any]],
) -> list[str]:
    """Propose up to 3 follow-up queries based on what the answer cited.

    Returns an empty list on refusal responses or when no useful signal
    is available. Never raises — the orchestrator call site treats this
    as strictly additive.
    """
    try:
        if response is None or _is_refusal(response.text or ""):
            return []

        q_lower = (query or "").lower()
        suggestions: list[str] = []

        hole = _extract_cited_hole(tool_results)
        element = _extract_cited_element(tool_results)
        entity = _extract_graph_entity(tool_results)
        has_docs = _has_document_chunks(tool_results)

        # Rule 1 — if a specific hole was surfaced and the user's query
        # wasn't already hole-specific, offer a single-hole deep-dive.
        if hole and not re.search(r"\b[A-Z]{2,8}-\d{1,6}-\d{1,6}\b", query):
            suggestions.append(f"Show lithology and structural intercepts for {hole}")

        # Rule 2 — if an element was queried, compare across holes.
        if element and "compare" not in q_lower:
            suggestions.append(f"Compare mean {element} grade across all holes")

        # Rule 3 — if a graph entity was cited AND documents exist, offer
        # a narrative deep-dive via the document corpus.
        if entity and has_docs and "what is" not in q_lower and "describe" not in q_lower:
            suggestions.append(f"What does the NI 43-101 report say about {entity}?")

        # Rule 4 — fallback for pure narrative queries: offer a target
        # recommendation if there was spatial data in scope.
        if not suggestions and any(
            isinstance(r, SpatialQueryResult) for _, r in tool_results
        ):
            suggestions.append("Where should the next drill hole be located?")

        # Rule 5 — always end with a "confidence probe" if there's any
        # uncertainty (confidence < 0.7). Gives the user an explicit
        # path to revisit instead of silently distrusting the answer.
        if getattr(response, "confidence", 1.0) < 0.7 and len(suggestions) < _MAX_FOLLOWUPS:
            suggestions.append("What sources was this answer based on?")

        # Dedupe + cap.
        seen: set[str] = set()
        deduped: list[str] = []
        for s in suggestions:
            if s not in seen:
                deduped.append(s)
                seen.add(s)
            if len(deduped) >= _MAX_FOLLOWUPS:
                break
        return deduped
    except Exception:
        logger.exception("generate_followups: non-fatal error; returning empty")
        return []
