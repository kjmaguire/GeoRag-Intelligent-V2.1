"""Sentence-level grounding verifier — 2026-06-01.

The Pydantic typed-output validator (Layer 2) checks that every Citation
object on the response has a non-empty source_chunk_id. That guarantees
*structural* citation discipline — but it doesn't catch paraphrase drift:
the LLM can emit a sentence with a citation marker whose semantic content
isn't actually supported by the cited chunk.

This module adds a SEMANTIC grounding check on top of Layer 2:

  1. Split the LLM's answer text into sentences.
  2. For each sentence, extract its citation marker (e.g. ``[DATA-3]``)
     and resolve to the underlying chunk text.
  3. Ask a small LLM "does this chunk support this sentence? yes/no
     with a 1-sentence rationale."
  4. Return a GroundingReport — supported sentences, unsupported sentences,
     and uncited sentences (facts with no citation at all).

The report is *advisory* by default — sentences are tagged in the response
metadata so the UI can render an "⚠ may not be fully supported by sources"
indicator, but the answer text is NOT modified. Switching to drop-mode
(removing unsupported sentences before emit) is a settings flip
(``SENTENCE_GROUNDING_DROP_MODE``) once the operator has trust in the
verifier's precision.

Performance: one LLM call per sentence — capped by
``SENTENCE_GROUNDING_MAX_SENTENCES`` (default 12). On Qwen3-14B-AWQ
each verification call is ~150-300ms; the full check on a 5-sentence
answer adds ~1-1.5s. Cached by sha256(sentence + chunk_id) in Redis for
24h since (sentence, chunk) pairs are stable across runs.

Graceful degradation: if the LLM is unavailable or the verifier errors,
all sentences are returned as ``"unverified"`` and the answer ships
unchanged. Never blocks the answer path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


# A "citation marker" is a token like [DATA-3], [NI43-2], [PGEO-1],
# or the colon variant [DATA:3]. Captures both hyphen and colon forms.
_CITATION_MARKER_RE = re.compile(
    r"\[(DATA|NI43|PUB|PGEO|GRAPH)[-:](\d+)\]",
    re.IGNORECASE,
)

# Simple sentence splitter — splits on sentence-ending punctuation
# followed by whitespace + capital letter. Avoids splitting common
# abbreviations like "Sr.", "Co.", "U.S.", "et al." and decimal numbers
# ("0.5 percent"). Geological text has lots of these so the splitter
# has to be a little more careful than a plain ``re.split(r'[.!?]\s+')``.
_SENTENCE_BOUNDARY_RE = re.compile(
    r"""(?<=[.!?])         # preceded by .!? but not consumed
        (?<![A-Z]\.)        # not after a single capital + dot (initials)
        (?<!\b[A-Z][a-z]\.) # not after Sr.|Co.|Mr.|Mt.|Dr. etc.
        \s+                 # whitespace
        (?=[A-Z(])          # followed by capital letter or opening paren
    """,
    re.VERBOSE,
)


_VERDICT_SUPPORTED = "supported"
_VERDICT_UNSUPPORTED = "unsupported"
_VERDICT_UNVERIFIED = "unverified"  # LLM call failed / timed out
_VERDICT_UNCITED = "uncited"        # sentence has no citation marker


@dataclass(frozen=True)
class SentenceVerdict:
    """One sentence's grounding verdict."""

    sentence: str
    verdict: str  # one of the _VERDICT_* constants
    cited_chunk_ids: tuple[str, ...] = ()
    rationale: str = ""


@dataclass
class GroundingReport:
    """Per-answer grounding report. Attached to the answer's metadata."""

    sentences: list[SentenceVerdict] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    verifier_ran: bool = False
    verifier_error: str | None = None

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "sentences": [
                {
                    "text": s.sentence,
                    "verdict": s.verdict,
                    "cited_chunk_ids": list(s.cited_chunk_ids),
                    "rationale": s.rationale,
                }
                for s in self.sentences
            ],
            "summary": dict(self.summary),
            "verifier_ran": self.verifier_ran,
            "verifier_error": self.verifier_error,
        }


def split_sentences(text: str) -> list[str]:
    """Split answer text into sentence-ish chunks.

    Conservative — would rather under-split (longer, atomic-ish chunks)
    than over-split (broken fragments verified out of context).
    """
    if not text or not text.strip():
        return []

    # Normalise whitespace.
    normalised = re.sub(r"\s+", " ", text.strip())
    # Initial split.
    raw_parts = _SENTENCE_BOUNDARY_RE.split(normalised)

    sentences: list[str] = []
    for part in raw_parts:
        stripped = part.strip()
        if not stripped:
            continue
        # Drop pure citation-only fragments like "[DATA-3]." that the
        # splitter occasionally produces on lists.
        if _CITATION_MARKER_RE.fullmatch(stripped.rstrip(".")):
            continue
        # Cap at 800 chars to keep verifier prompts cheap.
        if len(stripped) > 800:
            stripped = stripped[:800].rsplit(" ", 1)[0] + "…"
        sentences.append(stripped)

    return sentences


def extract_cited_chunk_ids(
    sentence: str, citation_map: dict[str, str]
) -> tuple[str, ...]:
    """Resolve a sentence's [DATA-3]-style markers to chunk_ids.

    ``citation_map`` is the response_assembler's marker→chunk_id table,
    e.g. ``{"DATA-3": "abc123…", "NI43-2": "def456…"}``.
    Markers that don't resolve are dropped (logged at debug level).
    """
    matches = _CITATION_MARKER_RE.findall(sentence)
    if not matches:
        return ()

    resolved: list[str] = []
    seen: set[str] = set()
    for kind, idx in matches:
        marker = f"{kind.upper()}-{idx}"
        chunk_id = citation_map.get(marker) or citation_map.get(
            f"{kind.upper()}:{idx}"
        )
        if chunk_id and chunk_id not in seen:
            seen.add(chunk_id)
            resolved.append(chunk_id)
        elif not chunk_id:
            logger.debug(
                "sentence_grounding: unresolved citation marker %r in sentence",
                marker,
            )

    return tuple(resolved)


def _cache_key(sentence: str, chunk_id: str) -> str:
    payload = f"{sentence.strip().lower()}||{chunk_id}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"georag:sg:v1:{digest}"


async def _read_cache(redis_client: Any, key: str) -> dict[str, Any] | None:
    if redis_client is None:
        return None
    try:
        raw = await redis_client.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sentence_grounding: redis GET failed: %s", exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


async def _write_cache(
    redis_client: Any, key: str, value: dict[str, Any], ttl_s: int
) -> None:
    if redis_client is None:
        return
    try:
        await redis_client.set(key, json.dumps(value), ex=ttl_s)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sentence_grounding: redis SET failed: %s", exc)


# JSON Schema for vLLM xgrammar guided decoding. The verifier must return
# {supported: bool, rationale: str} — the schema eliminates the
# `_parse_verifier_json` error branch on the vLLM path.
_VERIFIER_GUIDED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "rationale": {"type": "string", "maxLength": 400},
    },
    "required": ["supported", "rationale"],
    "additionalProperties": False,
}


_VERIFIER_SYSTEM_PROMPT = (
    "You are a fact-checking assistant for a geological RAG system. "
    "Given a sentence from an AI-generated answer and the chunks of source "
    "text it cites, decide whether the chunks SUPPORT the sentence. A "
    "sentence is supported if every factual claim it makes (numbers, names, "
    "entities, properties, dates) appears in or follows directly from the "
    "chunks. Paraphrasing is fine. Inferring new facts the chunks don't "
    "contain is NOT fine. Be strict on numbers, names, and entity IDs."
)


def _build_verifier_prompt(sentence: str, chunk_texts: list[str]) -> str:
    chunks_block = "\n\n".join(
        f"[CHUNK {i + 1}]\n{text[:2000]}" for i, text in enumerate(chunk_texts)
    )
    return (
        f"SENTENCE:\n{sentence}\n\n"
        f"CITED CHUNKS:\n{chunks_block}\n\n"
        "Reply with valid JSON only, in this exact shape:\n"
        '{"supported": true|false, "rationale": "<one sentence>"}\n\n'
        "Set supported=false if the sentence introduces facts not in the "
        "chunks, or contradicts the chunks, or relies on inference the "
        "chunks don't actually permit. Set supported=true if every claim "
        "in the sentence traces to the chunks."
    )


def _parse_verifier_json(raw: str) -> tuple[bool, str]:
    """Extract (supported, rationale) from the verifier's JSON output."""
    text = raw.strip()
    # Strip markdown fences if present.
    for fence in ("```json", "```JSON", "```"):
        if text.startswith(fence):
            text = text[len(fence):].lstrip()
        if text.endswith("```"):
            text = text[: -len("```")].rstrip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no JSON object in verifier reply: {raw[:200]!r}")

    payload = text[start : end + 1]
    parsed = json.loads(payload)
    supported = bool(parsed.get("supported", False))
    rationale = str(parsed.get("rationale", "")).strip()[:300]
    return supported, rationale


async def _verify_one_sentence(
    sentence: str,
    cited_chunk_ids: tuple[str, ...],
    chunk_text_lookup: dict[str, str],
    *,
    anthropic_client: Any,
    openai_http_client: Any,
    redis_client: Any,
    pg_pool: Any,
    timeout_s: float,
) -> SentenceVerdict:
    """Run the LLM-backed grounding check for one (sentence, citations) pair."""

    # Resolve chunk texts. Skip silently if the citation marker doesn't
    # map to a known chunk — Layer 2 already enforces that markers
    # resolve, so this branch is defensive only.
    chunk_texts: list[str] = []
    for cid in cited_chunk_ids:
        text = chunk_text_lookup.get(cid)
        if text:
            chunk_texts.append(text)
    if not chunk_texts:
        return SentenceVerdict(
            sentence=sentence,
            verdict=_VERDICT_UNVERIFIED,
            cited_chunk_ids=cited_chunk_ids,
            rationale="no chunk text available for cited markers",
        )

    # Cache lookup — keyed on the full (sentence, chunk_id_tuple) combo
    # because verdict can change if the LLM sees a different chunk subset.
    cache_key = _cache_key(sentence, "|".join(sorted(cited_chunk_ids)))
    cached = await _read_cache(redis_client, cache_key)
    if cached is not None:
        return SentenceVerdict(
            sentence=sentence,
            verdict=cached.get("verdict", _VERDICT_UNVERIFIED),
            cited_chunk_ids=cited_chunk_ids,
            rationale=cached.get("rationale", ""),
        )

    from app.agent.llm_calls import _call_llm  # noqa: PLC0415

    prompt = _build_verifier_prompt(sentence, chunk_texts)

    try:
        raw = await asyncio.wait_for(
            _call_llm(
                query=prompt,
                context="",
                temperature=0.0,
                anthropic_client=anthropic_client,
                openai_http_client=openai_http_client,
                pg_pool=pg_pool,
                system_prompt=_VERIFIER_SYSTEM_PROMPT,
                audit_label="sentence_grounding",
                response_format="json",
                guided_json=_VERIFIER_GUIDED_SCHEMA,
                enable_thinking=False,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "sentence_grounding: verifier timed out after %.1fs — marking unverified",
            timeout_s,
        )
        return SentenceVerdict(
            sentence=sentence,
            verdict=_VERDICT_UNVERIFIED,
            cited_chunk_ids=cited_chunk_ids,
            rationale="verifier LLM call timed out",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("sentence_grounding: verifier errored (%s)", exc)
        return SentenceVerdict(
            sentence=sentence,
            verdict=_VERDICT_UNVERIFIED,
            cited_chunk_ids=cited_chunk_ids,
            rationale=f"verifier error: {exc}",
        )

    try:
        supported, rationale = _parse_verifier_json(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("sentence_grounding: malformed verifier JSON (%s)", exc)
        return SentenceVerdict(
            sentence=sentence,
            verdict=_VERDICT_UNVERIFIED,
            cited_chunk_ids=cited_chunk_ids,
            rationale=f"verifier parse error: {exc}",
        )

    verdict = _VERDICT_SUPPORTED if supported else _VERDICT_UNSUPPORTED
    await _write_cache(
        redis_client,
        cache_key,
        {"verdict": verdict, "rationale": rationale},
        settings.SENTENCE_GROUNDING_CACHE_TTL_S,
    )

    return SentenceVerdict(
        sentence=sentence,
        verdict=verdict,
        cited_chunk_ids=cited_chunk_ids,
        rationale=rationale,
    )


def build_chunk_text_lookup_from_tool_results(
    tool_results: list[tuple[str, Any]],
) -> dict[str, str]:
    """Walk tool_results and harvest a {chunk_id: text} map.

    Used by the orchestrator to feed verify_answer_grounding without
    requiring the assembler to expose its internal source_chunk_id table.

    Currently covers DocumentSearchResult.chunks (the main retrieval
    path); SpatialQueryResult / GraphTraversalResult are skipped because
    their "text" is row-shaped, not free prose — verifying a sentence
    against tabular data needs different scaffolding (e.g. ask the
    verifier to compare numbers field-by-field rather than NLI-style).
    Adding row-shaped tools is a follow-up.
    """
    lookup: dict[str, str] = {}
    for _tool_name, result in tool_results:
        chunks = getattr(result, "chunks", None)
        if not chunks:
            continue
        for chunk in chunks:
            chunk_id = getattr(chunk, "chunk_id", None)
            text = getattr(chunk, "text", None)
            if chunk_id and text:
                lookup[str(chunk_id)] = str(text)
    return lookup


def build_marker_to_chunk_id(citations: list[Any]) -> dict[str, str]:
    """Build the {[DATA-3]: chunk_uuid} table from a list of Citation objects.

    Strips the surrounding ``[]`` so the lookup matches what the
    extractor produces from regex matches. Handles both ``[DATA-3]`` and
    ``[DATA:3]`` citation-id shapes (the system supports both).
    """
    lookup: dict[str, str] = {}
    for citation in citations:
        cid = getattr(citation, "citation_id", "")
        chunk_id = getattr(citation, "source_chunk_id", "")
        if not cid or not chunk_id:
            continue
        # Strip [] wrapping if present.
        key = cid.strip()
        if key.startswith("[") and key.endswith("]"):
            key = key[1:-1]
        lookup[key] = chunk_id
        # Also register the opposite separator form for convenience.
        if "-" in key:
            lookup[key.replace("-", ":", 1)] = chunk_id
        elif ":" in key:
            lookup[key.replace(":", "-", 1)] = chunk_id
    return lookup


async def verify_answer_grounding(
    answer_text: str,
    citation_marker_to_chunk_id: dict[str, str],
    chunk_text_lookup: dict[str, str],
    *,
    anthropic_client: Any = None,
    openai_http_client: Any = None,
    redis_client: Any = None,
    pg_pool: Any = None,
) -> GroundingReport:
    """Verify every cited sentence in ``answer_text`` against its chunks.

    Args:
        answer_text: The LLM's final answer string.
        citation_marker_to_chunk_id: ``{"DATA-3": "chunk_uuid", ...}`` —
            the response_assembler's marker→chunk_id resolution table.
        chunk_text_lookup: ``{"chunk_uuid": "<chunk text>", ...}`` for
            every chunk that could be cited.
        anthropic_client / openai_http_client / redis_client / pg_pool:
            Forwarded to the inner ``_call_llm`` for the verifier model.

    Returns:
        GroundingReport with one SentenceVerdict per sentence + a summary
        count by verdict. ``verifier_ran=True`` when at least one cited
        sentence was actually verified; ``False`` when the verifier was
        disabled, ran into no eligible sentences, or hit a global error.
    """
    report = GroundingReport()

    if not settings.SENTENCE_GROUNDING_ENABLED:
        return report
    if not answer_text or not answer_text.strip():
        return report

    sentences = split_sentences(answer_text)
    if not sentences:
        return report

    # Cap how many sentences we verify per answer — protects against
    # pathological long answers blowing the LLM-call budget. Truncated
    # remaining sentences are reported as 'unverified' so the caller
    # can render a "this many sentences not checked" badge.
    cap = settings.SENTENCE_GROUNDING_MAX_SENTENCES
    verifiable, truncated = sentences[:cap], sentences[cap:]

    # Build the per-sentence work list. Sentences with no citation
    # marker are recorded as 'uncited' without an LLM call.
    work: list[tuple[str, tuple[str, ...]]] = []
    for sentence in verifiable:
        chunk_ids = extract_cited_chunk_ids(sentence, citation_marker_to_chunk_id)
        if chunk_ids:
            work.append((sentence, chunk_ids))
        else:
            report.sentences.append(
                SentenceVerdict(
                    sentence=sentence,
                    verdict=_VERDICT_UNCITED,
                    cited_chunk_ids=(),
                    rationale="no citation marker in sentence",
                )
            )

    if work:
        report.verifier_ran = True
        timeout_s = settings.SENTENCE_GROUNDING_PER_SENTENCE_TIMEOUT_S
        # Fan out the verifier calls in parallel — cap concurrency to
        # avoid stampeding the LLM backend with 12 simultaneous calls.
        sem = asyncio.Semaphore(settings.SENTENCE_GROUNDING_CONCURRENCY)

        async def _run_one(sent: str, cids: tuple[str, ...]) -> SentenceVerdict:
            async with sem:
                return await _verify_one_sentence(
                    sent,
                    cids,
                    chunk_text_lookup,
                    anthropic_client=anthropic_client,
                    openai_http_client=openai_http_client,
                    redis_client=redis_client,
                    pg_pool=pg_pool,
                    timeout_s=timeout_s,
                )

        try:
            verdicts = await asyncio.gather(
                *(_run_one(s, c) for s, c in work),
                return_exceptions=False,
            )
            report.sentences.extend(verdicts)
        except Exception as exc:  # noqa: BLE001
            logger.exception("sentence_grounding: fan-out failed")
            report.verifier_error = str(exc)
            # Mark all the un-verified work as 'unverified' rather than
            # losing the sentences entirely.
            for sent, cids in work:
                report.sentences.append(
                    SentenceVerdict(
                        sentence=sent,
                        verdict=_VERDICT_UNVERIFIED,
                        cited_chunk_ids=cids,
                        rationale=f"fan-out error: {exc}",
                    )
                )

    # Truncated tail → record as unverified-by-cap so UI can show a badge.
    for sentence in truncated:
        chunk_ids = extract_cited_chunk_ids(sentence, citation_marker_to_chunk_id)
        report.sentences.append(
            SentenceVerdict(
                sentence=sentence,
                verdict=_VERDICT_UNVERIFIED,
                cited_chunk_ids=chunk_ids,
                rationale=f"skipped — answer exceeded per-answer cap ({cap})",
            )
        )

    # Summary counts by verdict.
    summary: dict[str, int] = {}
    for sv in report.sentences:
        summary[sv.verdict] = summary.get(sv.verdict, 0) + 1
    report.summary = summary

    logger.info(
        "sentence_grounding: verified %d sentences (%s)",
        len(report.sentences),
        ", ".join(f"{k}={v}" for k, v in sorted(summary.items())),
    )

    return report
