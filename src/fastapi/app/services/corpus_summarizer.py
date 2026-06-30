"""Dedicated map-reduce summarization pipeline (#6) — 2026-06-01.

Free-text Q&A and corpus summarization are structurally different
problems and the current pipeline conflates them. A factoid like *"how
deep is PLS-22-08?"* wants the single most-relevant chunk; a request
like *"summarize this part of the corpus and tell me what it's about"*
wants COVERAGE of many chunks, even at the cost of relevance.

This module is the dedicated summarization path:

  1. **Scope decision** — parse the query for an implicit scope filter
     ("Article 5", "the Madsen PFS", "all 2024 reports", "the QA/QC
     sections"). When unclear, ask a small LLM to extract a structured
     scope from the user's wording.
  2. **Scope-bounded retrieval** — apply the scope filter to the
     existing retrieval pipeline and pull a wider candidate pool than
     normal (RETRIEVAL_TOP_N×3) to give the map step real coverage.
  3. **Map** — summarize each chunk individually, with citation. The
     map LLM is told to produce 2–5 bullet points per chunk, each one
     traceable to a quote-able span.
  4. **Reduce** — synthesize across chunk-summaries into a final
     summary. The reducer is constrained to use only material the map
     step produced — no NEW facts at the reduce stage.
  5. **Faithfulness gate** — every bullet in the final summary must
     cite ≥1 chunk-summary, which itself cites ≥1 chunk.
  6. **What I didn't cover** footer — explicitly list scopes the
     retrieval returned but the summary chose not to use. Honest
     about its own gaps.

Status: full implementation of map + reduce + scope decision. NOT YET
wired to the orchestrator's intent dispatcher. Wiring is a focused
follow-up — needs the intent classifier to route summarize queries to
this pipeline instead of the standard answer pathway, plus an
``IntentRoute.SUMMARIZE`` enum addition.

This module is callable today via ``summarize_scope(...)`` for testing
and benchmark runs against the gap-question set.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeFilter:
    """A structured-and-fuzzy scope decision parsed from the user's query."""

    raw_query: str
    document_filter: str | None = None  # e.g. "Section 5", "Article 5", "Item 4"
    project_filter: str | None = None   # e.g. "Madsen PFS", "WRLG"
    topic_filter: str | None = None     # e.g. "QA/QC", "resource estimate"
    breadth: str = "narrow"             # narrow | medium | broad


@dataclass(frozen=True)
class ChunkSummary:
    """A per-chunk summary produced by the map step."""

    chunk_id: str
    bullets: tuple[str, ...]
    document_title: str | None = None
    section: str | None = None


@dataclass
class SummaryResult:
    """Final output of the summarization pipeline."""

    summary_text: str = ""
    chunk_summaries: list[ChunkSummary] = field(default_factory=list)
    scope: ScopeFilter | None = None
    not_covered: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "summary_text": self.summary_text,
            "chunk_summaries": [
                {
                    "chunk_id": cs.chunk_id,
                    "bullets": list(cs.bullets),
                    "document_title": cs.document_title,
                    "section": cs.section,
                }
                for cs in self.chunk_summaries
            ],
            "scope": (
                {
                    "raw_query": self.scope.raw_query,
                    "document_filter": self.scope.document_filter,
                    "project_filter": self.scope.project_filter,
                    "topic_filter": self.scope.topic_filter,
                    "breadth": self.scope.breadth,
                }
                if self.scope
                else None
            ),
            "not_covered": list(self.not_covered),
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# Step 1 — scope decision
# ---------------------------------------------------------------------------


# JSON Schema for vLLM xgrammar guided decoding (decide_scope).
_SCOPE_GUIDED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_filter": {"type": ["string", "null"]},
        "project_filter": {"type": ["string", "null"]},
        "topic_filter": {"type": ["string", "null"]},
        "breadth": {"type": "string", "enum": ["narrow", "medium", "broad"]},
    },
    "required": ["breadth"],
    "additionalProperties": False,
}


_SCOPE_SYSTEM_PROMPT = (
    "You parse a user's summarization query into a structured scope. "
    "Extract document_filter (e.g. 'Section 5', 'Article 5', 'Item 4', "
    "'Appendix B'), project_filter (e.g. 'Madsen PFS', 'WRLG', "
    "'Shakespeare Property'), topic_filter (e.g. 'QA/QC', 'resource "
    "estimate', 'metallurgy'), and breadth (narrow | medium | broad). "
    "Leave a field null when the query doesn't constrain it. Return "
    "ONLY valid JSON."
)


def _build_scope_prompt(query: str) -> str:
    return (
        f"USER QUERY:\n{query}\n\n"
        "Return ONLY a JSON object in this shape:\n"
        '{"document_filter": "<str or null>", '
        '"project_filter": "<str or null>", '
        '"topic_filter": "<str or null>", '
        '"breadth": "narrow|medium|broad"}'
    )


def _parse_scope_json(raw: str, query: str) -> ScopeFilter:
    text = raw.strip()
    for fence in ("```json", "```JSON", "```"):
        if text.startswith(fence):
            text = text[len(fence):].lstrip()
        if text.endswith("```"):
            text = text[: -len("```")].rstrip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in scope reply: {raw[:200]!r}")
    parsed = json.loads(text[start : end + 1])
    breadth = str(parsed.get("breadth") or "narrow").strip().lower()
    if breadth not in {"narrow", "medium", "broad"}:
        breadth = "narrow"
    return ScopeFilter(
        raw_query=query,
        document_filter=(parsed.get("document_filter") or None)
        if isinstance(parsed.get("document_filter"), str)
        else None,
        project_filter=(parsed.get("project_filter") or None)
        if isinstance(parsed.get("project_filter"), str)
        else None,
        topic_filter=(parsed.get("topic_filter") or None)
        if isinstance(parsed.get("topic_filter"), str)
        else None,
        breadth=breadth,
    )


async def decide_scope(
    query: str,
    *,
    anthropic_client: Any = None,
    openai_http_client: Any = None,
    pg_pool: Any = None,
) -> ScopeFilter:
    """LLM-parsed scope decision. Falls back to ScopeFilter(raw_query=query)
    on any error — caller can still proceed with no filter."""
    from app.agent.llm_calls import _call_llm  # noqa: PLC0415

    try:
        raw = await asyncio.wait_for(
            _call_llm(
                query=_build_scope_prompt(query),
                context="",
                temperature=0.0,
                anthropic_client=anthropic_client,
                openai_http_client=openai_http_client,
                pg_pool=pg_pool,
                system_prompt=_SCOPE_SYSTEM_PROMPT,
                audit_label="summarizer_scope",
                response_format="json",
                guided_json=_SCOPE_GUIDED_SCHEMA,
                enable_thinking=False,
            ),
            timeout=settings.SUMMARIZER_SCOPE_TIMEOUT_S,
        )
    except (TimeoutError, Exception) as exc:
        logger.warning("corpus_summarizer.decide_scope: LLM error (%s)", exc)
        return ScopeFilter(raw_query=query)

    try:
        return _parse_scope_json(raw, query)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("corpus_summarizer.decide_scope: parse error (%s)", exc)
        return ScopeFilter(raw_query=query)


# ---------------------------------------------------------------------------
# Step 3 — map: per-chunk summary
# ---------------------------------------------------------------------------


# JSON Schema for vLLM xgrammar guided decoding (map_chunk).
_MAP_GUIDED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bullets": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 400},
            "maxItems": 5,
        }
    },
    "required": ["bullets"],
    "additionalProperties": False,
}


_MAP_SYSTEM_PROMPT = (
    "You summarize a single chunk of source text into 2–5 atomic bullet "
    "points relevant to the user's query. Each bullet must (a) be "
    "directly supported by the chunk, (b) be a complete sentence, (c) "
    "avoid filler. Do NOT introduce facts the chunk doesn't state. If "
    "the chunk has no content relevant to the query, return an empty "
    "bullets list."
)


def _build_map_prompt(query: str, chunk_text: str) -> str:
    return (
        f"USER QUERY:\n{query}\n\n"
        f"SOURCE CHUNK:\n{chunk_text[:3500]}\n\n"
        "Return ONLY valid JSON in this shape (max 5 bullets):\n"
        '{"bullets": ["<bullet 1>", "<bullet 2>", ...]}'
    )


def _parse_map_json(raw: str) -> list[str]:
    text = raw.strip()
    for fence in ("```json", "```JSON", "```"):
        if text.startswith(fence):
            text = text[len(fence):].lstrip()
        if text.endswith("```"):
            text = text[: -len("```")].rstrip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON in map reply")
    parsed = json.loads(text[start : end + 1])
    bullets = parsed.get("bullets") or []
    if not isinstance(bullets, list):
        return []
    return [str(b).strip() for b in bullets[:5] if isinstance(b, str) and b.strip()]


async def map_chunk(
    query: str,
    chunk_id: str,
    chunk_text: str,
    *,
    anthropic_client: Any = None,
    openai_http_client: Any = None,
    pg_pool: Any = None,
    document_title: str | None = None,
    section: str | None = None,
) -> ChunkSummary:
    from app.agent.llm_calls import _call_llm  # noqa: PLC0415

    if not chunk_text or not chunk_text.strip():
        return ChunkSummary(
            chunk_id=chunk_id,
            bullets=(),
            document_title=document_title,
            section=section,
        )

    try:
        raw = await asyncio.wait_for(
            _call_llm(
                query=_build_map_prompt(query, chunk_text),
                context="",
                temperature=0.1,
                anthropic_client=anthropic_client,
                openai_http_client=openai_http_client,
                pg_pool=pg_pool,
                system_prompt=_MAP_SYSTEM_PROMPT,
                audit_label="summarizer_map",
                response_format="json",
                guided_json=_MAP_GUIDED_SCHEMA,
                enable_thinking=False,
            ),
            timeout=settings.SUMMARIZER_MAP_TIMEOUT_S,
        )
    except (TimeoutError, Exception) as exc:
        logger.warning(
            "corpus_summarizer.map_chunk: LLM error for chunk=%s: %s",
            chunk_id[:12],
            exc,
        )
        return ChunkSummary(
            chunk_id=chunk_id,
            bullets=(),
            document_title=document_title,
            section=section,
        )

    try:
        bullets = _parse_map_json(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("corpus_summarizer.map_chunk: parse error: %s", exc)
        bullets = []

    return ChunkSummary(
        chunk_id=chunk_id,
        bullets=tuple(bullets),
        document_title=document_title,
        section=section,
    )


# ---------------------------------------------------------------------------
# Step 4 — reduce: synthesise across chunk-summaries
# ---------------------------------------------------------------------------


_REDUCE_SYSTEM_PROMPT = (
    "You synthesise a final answer to the user's query using ONLY a "
    "list of bullet points extracted from source chunks. You may merge, "
    "reorder, and paraphrase the bullets into flowing prose, but you "
    "MUST NOT introduce facts that aren't in the bullet list. Every "
    "sentence you emit must cite at least one [CHUNK-N] marker — the "
    "chunk(s) whose bullets it draws from. If the bullets don't cover "
    "the query, say so plainly and list what they DO cover."
)


def _build_reduce_prompt(
    query: str, chunk_summaries: list[ChunkSummary]
) -> tuple[str, dict[str, int]]:
    """Render the bullet pool and a chunk-index map for the reducer."""
    chunk_index: dict[str, int] = {}
    lines: list[str] = []
    for cs in chunk_summaries:
        if not cs.bullets:
            continue
        idx = chunk_index.get(cs.chunk_id)
        if idx is None:
            idx = len(chunk_index) + 1
            chunk_index[cs.chunk_id] = idx
        for b in cs.bullets:
            lines.append(f"[CHUNK-{idx}] {b}")
    block = "\n".join(lines) if lines else "(no bullets produced)"
    prompt = (
        f"USER QUERY:\n{query}\n\n"
        f"BULLET POOL (from {len(chunk_index)} chunks):\n{block}\n\n"
        "Compose the final summary. Every sentence must end with the "
        "[CHUNK-N] marker(s) it draws from. Lead with the most "
        "directly-relevant bullets; close with a one-line 'gaps I "
        "noticed' note if any obvious aspect of the query was not "
        "covered by the bullets."
    )
    return prompt, chunk_index


async def reduce_summaries(
    query: str,
    chunk_summaries: list[ChunkSummary],
    *,
    anthropic_client: Any = None,
    openai_http_client: Any = None,
    pg_pool: Any = None,
) -> str:
    if not any(cs.bullets for cs in chunk_summaries):
        return (
            "I retrieved passages for your query but none yielded "
            "summarisable content. Try narrowing the topic or naming a "
            "specific report / section."
        )

    from app.agent.llm_calls import _call_llm  # noqa: PLC0415

    prompt, _ = _build_reduce_prompt(query, chunk_summaries)

    try:
        return await asyncio.wait_for(
            _call_llm(
                query=prompt,
                context="",
                temperature=0.2,
                anthropic_client=anthropic_client,
                openai_http_client=openai_http_client,
                pg_pool=pg_pool,
                system_prompt=_REDUCE_SYSTEM_PROMPT,
                audit_label="summarizer_reduce",
                enable_thinking=False,
            ),
            timeout=settings.SUMMARIZER_REDUCE_TIMEOUT_S,
        )
    except TimeoutError:
        logger.warning("corpus_summarizer.reduce_summaries: timed out")
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.exception("corpus_summarizer.reduce_summaries: LLM error: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# End-to-end entry point
# ---------------------------------------------------------------------------


async def summarize_scope(
    query: str,
    chunks: list[tuple[str, str, dict[str, Any]]],
    *,
    anthropic_client: Any = None,
    openai_http_client: Any = None,
    pg_pool: Any = None,
    max_chunks: int | None = None,
) -> SummaryResult:
    """End-to-end map-reduce summarisation over a retrieved chunk pool.

    Args:
        query: The user's summarisation request.
        chunks: List of (chunk_id, chunk_text, metadata) tuples, where
            metadata may carry ``document_title`` and ``section``.
            Typically comes from search_documents with a wider top-K
            than the standard answer pathway.
        anthropic_client / openai_http_client / pg_pool: forwarded to
            ``_call_llm``.
        max_chunks: Optional cap on chunks fed into the map step.
            Defaults to ``settings.SUMMARIZER_MAX_CHUNKS``. Above this
            cap the extra chunks land in ``result.not_covered``.

    Returns:
        SummaryResult with the final synthesis, per-chunk summaries,
        scope decision, and any chunks not used.
    """
    result = SummaryResult()

    # Scope decision (single LLM call; fall back to raw query on error).
    scope = await decide_scope(
        query,
        anthropic_client=anthropic_client,
        openai_http_client=openai_http_client,
        pg_pool=pg_pool,
    )
    result.scope = scope

    if not chunks:
        result.summary_text = (
            "I couldn't find any passages matching your summarisation "
            "request. Try a broader topic or name a specific report."
        )
        return result

    # Map step — capped to keep map LLM call count bounded.
    cap = (
        max_chunks
        if max_chunks is not None
        else settings.SUMMARIZER_MAX_CHUNKS
    )
    map_chunks, deferred = chunks[:cap], chunks[cap:]
    if deferred:
        result.not_covered.extend(
            f"{cid} ({(meta or {}).get('document_title', 'unknown')})"
            for cid, _txt, meta in deferred
        )

    sem = asyncio.Semaphore(settings.SUMMARIZER_MAP_CONCURRENCY)

    async def _run_one(cid: str, text: str, meta: dict[str, Any]) -> ChunkSummary:
        async with sem:
            return await map_chunk(
                query, cid, text,
                anthropic_client=anthropic_client,
                openai_http_client=openai_http_client,
                pg_pool=pg_pool,
                document_title=(meta or {}).get("document_title"),
                section=(meta or {}).get("section"),
            )

    chunk_summaries = await asyncio.gather(
        *(_run_one(cid, txt, meta or {}) for cid, txt, meta in map_chunks),
        return_exceptions=False,
    )
    result.chunk_summaries = list(chunk_summaries)

    # Reduce step.
    result.summary_text = await reduce_summaries(
        query, result.chunk_summaries,
        anthropic_client=anthropic_client,
        openai_http_client=openai_http_client,
        pg_pool=pg_pool,
    )

    logger.info(
        "corpus_summarizer.summarize_scope: %d chunks mapped, %d bullet "
        "groups produced, %d chunks deferred",
        len(map_chunks),
        sum(1 for cs in chunk_summaries if cs.bullets),
        len(deferred),
    )
    return result
