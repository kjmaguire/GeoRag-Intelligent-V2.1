"""Multi-query expansion for retrieval — 2026-06-01.

Generates alternative phrasings of the user's question to widen retrieval
coverage. Today's deterministic retrieval embeds the user's literal query
once; if the corpus uses different vocabulary (e.g. user says "Red Lake
Gold Project" but the NI 43-101 calls it "Dixie Project" or "WRLG"), the
single embedding misses semantically-equivalent passages.

Multi-query expansion calls a small LLM to draft N alternative queries
covering three orthogonal angles:

  1. **Synonym swap** — abbreviations ↔ full terms ("DDH" ↔ "diamond
     drillhole"), company names ↔ project nicknames.
  2. **HyDE-style hypothetical** — a one-sentence draft answer. The
     hypothetical often phrases the topic the way the corpus does, so
     embedding the hypothetical matches more strongly than embedding the
     question.
  3. **Entity-focused** — rephrase leading with the named entity rather
     than the question verb.

Each expansion is embedded and queried independently; results are unioned
by chunk_id and reranked against the **original** query (the
reranker should always optimise for the user's semantic intent, not the
expanded paraphrase).

Caching: expansions are pure functions of the query text and are stable
across runs, so we cache them in Redis by sha256(query) for 24h. Cache
misses pay one LLM call; cache hits are ~1ms.

Graceful degradation: if the LLM call errors out or returns malformed
JSON, the function returns `[original_query]` and logs a warning. Callers
keep retrieving against the literal query — never block on the expansion.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


_CACHE_KEY_PREFIX = "georag:mqe:v1:"

# JSON Schema for vLLM xgrammar guided decoding. Forwarded as `guided_json`
# so the engine refuses to emit tokens that would violate the shape — the
# downstream `_parse_llm_json` ValueError branch becomes structurally
# unreachable on the vLLM path. `additionalProperties: false` keeps the
# model from inventing sibling fields (no `notes`, `reasoning`, etc.).
_EXPANSION_GUIDED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "expansions": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 400},
            "minItems": 1,
            "maxItems": 8,
        }
    },
    "required": ["expansions"],
    "additionalProperties": False,
}

_EXPANSION_SYSTEM_PROMPT = (
    "You are a query-rewriting assistant for a geological intelligence "
    "RAG system. Given a user's question, generate alternative phrasings "
    "that would retrieve relevant passages from a corpus of NI 43-101 "
    "technical reports, drill-hole logs, and public geoscience datasets. "
    "Do NOT answer the question. Do NOT add information. Only rephrase."
)


def _build_expansion_prompt(query: str, n: int) -> str:
    return (
        f"User question: {query}\n\n"
        f"Generate exactly {n} alternative phrasings of this question that "
        "might match different vocabulary in a geological corpus. Use these "
        "three strategies, one per phrasing:\n"
        "  1. SYNONYM SWAP — substitute abbreviations / full terms / project "
        "nicknames. Examples: 'DDH' ↔ 'diamond drillhole'; 'Red Lake Gold "
        "Project' ↔ 'Dixie Project' or 'WRLG'; 'Article 5' ↔ 'Section 5'.\n"
        "  2. HYPOTHETICAL ANSWER — write a single declarative sentence as if "
        "you were drafting the answer (HyDE). This is the embedding that most "
        "often matches corpus phrasing.\n"
        "  3. ENTITY-FOCUSED — lead with the most specific named entity in "
        "the question, then state what's being asked about it.\n\n"
        "Return ONLY valid JSON in this exact shape:\n"
        '{"expansions": ["phrasing1", "phrasing2", "phrasing3"]}\n\n'
        "Each phrasing must be a complete English sentence ≤ 200 characters. "
        "Do not include explanations, numbering, or any text outside the JSON."
    )


def _query_cache_key(query: str, n: int) -> str:
    digest = hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()[:24]
    return f"{_CACHE_KEY_PREFIX}n{n}:{digest}"


async def _read_cache(redis_client: Any, key: str) -> list[str] | None:
    if redis_client is None:
        return None
    try:
        raw = await redis_client.get(key)
    except Exception as exc:  # noqa: BLE001 — defensive: cache is best-effort
        logger.debug("multi_query_expansion: redis GET failed: %s", exc)
        return None
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, list) and all(isinstance(x, str) for x in decoded):
            return decoded
    except (json.JSONDecodeError, TypeError):
        return None
    return None


async def _write_cache(redis_client: Any, key: str, value: list[str], ttl_s: int) -> None:
    if redis_client is None:
        return
    try:
        await redis_client.set(key, json.dumps(value), ex=ttl_s)
    except Exception as exc:  # noqa: BLE001
        logger.debug("multi_query_expansion: redis SET failed: %s", exc)


def _parse_llm_json(raw: str, expected_n: int) -> list[str]:
    """Extract the 'expansions' list from the LLM's JSON response.

    Defensive against:
      - Surrounding prose / markdown fences
      - Trailing commas (jsonish, not strict JSON)
      - Wrong type for the 'expansions' value
    """
    # Strip common markdown fences.
    text = raw.strip()
    for fence in ("```json", "```JSON", "```"):
        if text.startswith(fence):
            text = text[len(fence):].lstrip()
        if text.endswith("```"):
            text = text[: -len("```")].rstrip()

    # Find the JSON object boundaries.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no JSON object found in LLM response: {raw[:200]!r}")

    payload = text[start : end + 1]
    parsed = json.loads(payload)

    expansions = parsed.get("expansions")
    if not isinstance(expansions, list):
        raise ValueError(f"'expansions' is not a list: {parsed!r}")

    cleaned: list[str] = []
    for item in expansions:
        if not isinstance(item, str):
            continue
        stripped = item.strip()
        if 0 < len(stripped) <= 400:
            cleaned.append(stripped)

    if not cleaned:
        raise ValueError(f"no usable expansions after cleaning: {expansions!r}")

    # Truncate to expected count — the LLM occasionally over-produces.
    return cleaned[:expected_n]


async def expand_query_multi(
    query: str,
    *,
    anthropic_client: Any = None,
    openai_http_client: Any = None,
    redis_client: Any = None,
    pg_pool: Any = None,
    n: int | None = None,
    timeout_s: float | None = None,
) -> list[str]:
    """Return [original_query] + up to N LLM-generated alternative phrasings.

    The original query is ALWAYS the first element. If the LLM call fails,
    times out, or returns malformed output, the returned list is just
    [original_query] — never empty, never errored, retrieval always
    proceeds.

    Args:
        query: The user's natural-language question. Stripped before use.
        anthropic_client / openai_http_client: Forwarded to `_call_llm`.
        redis_client: Optional cache backend. When omitted, every call
            costs one LLM round-trip.
        pg_pool: Forwarded to `_call_llm` for audit-log writes.
        n: Number of alternatives to request (defaults to
            settings.MULTI_QUERY_EXPANSION_N).
        timeout_s: Hard ceiling on the LLM call (defaults to
            settings.MULTI_QUERY_EXPANSION_TIMEOUT_S). On timeout we fall
            back to [original_query].

    Returns:
        list[str]: 1 + N elements when expansion succeeds; 1 element on
        any failure path. Order is [original, alt1, alt2, alt3].
    """
    cleaned_query = (query or "").strip()
    if not cleaned_query:
        return []

    n_alts = n if n is not None else settings.MULTI_QUERY_EXPANSION_N
    if n_alts <= 0:
        return [cleaned_query]

    cache_key = _query_cache_key(cleaned_query, n_alts)
    cached = await _read_cache(redis_client, cache_key)
    if cached is not None:
        logger.debug("multi_query_expansion: cache hit key=%s", cache_key)
        return [cleaned_query, *cached]

    # Lazy import — keeps this module importable in tests that don't
    # spin up the full orchestrator/LLM stack.
    from app.agent.llm_calls import _call_llm  # noqa: PLC0415

    prompt = _build_expansion_prompt(cleaned_query, n_alts)
    deadline = (
        timeout_s
        if timeout_s is not None
        else settings.MULTI_QUERY_EXPANSION_TIMEOUT_S
    )

    try:
        raw = await asyncio.wait_for(
            _call_llm(
                query=prompt,
                context="",
                # Qwen team's published non-thinking default. Was 0.2 — too
                # low for diversity, was producing near-duplicate paraphrases
                # that defeated the point of multi-query retrieval. The whole
                # value of this stage IS phrasing diversity; the original
                # query is always in slot 0, so temperature-induced noise can
                # only widen recall, never hurt it.
                temperature=settings.QWEN3_TEMPERATURE_DIVERSE,
                anthropic_client=anthropic_client,
                openai_http_client=openai_http_client,
                pg_pool=pg_pool,
                system_prompt=_EXPANSION_SYSTEM_PROMPT,
                audit_label="multi_query_expansion",
                response_format="json",
                guided_json=_EXPANSION_GUIDED_SCHEMA,
                enable_thinking=False,
            ),
            timeout=deadline,
        )
    except TimeoutError:
        logger.warning(
            "multi_query_expansion: LLM call timed out after %.1fs — "
            "falling back to single-query retrieval",
            deadline,
        )
        return [cleaned_query]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "multi_query_expansion: LLM call failed (%s) — falling back "
            "to single-query retrieval",
            exc,
        )
        return [cleaned_query]

    try:
        expansions = _parse_llm_json(raw, expected_n=n_alts)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "multi_query_expansion: malformed LLM JSON (%s) — falling back "
            "to single-query retrieval. raw=%r",
            exc,
            raw[:300] if isinstance(raw, str) else raw,
        )
        return [cleaned_query]

    # De-dupe: if the LLM returned the original query (or a trivial casing
    # variant) drop it — we already have it in slot 0.
    seen_norm = {cleaned_query.lower()}
    deduped: list[str] = []
    for exp in expansions:
        norm = exp.lower().strip()
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        deduped.append(exp)

    await _write_cache(
        redis_client, cache_key, deduped, settings.MULTI_QUERY_EXPANSION_CACHE_TTL_S
    )

    logger.info(
        "multi_query_expansion: generated %d expansions for query_hash=%s",
        len(deduped),
        cache_key[-24:],
    )

    return [cleaned_query, *deduped]
