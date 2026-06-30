"""Atomic-claim extractor for citation-first generation (#5) — 2026-06-01.

The current answer pipeline is **citation-attached**: the LLM writes free
prose, then the response_assembler attaches citation markers to whole
sentences. This means a single sentence can contain three claims with
only one citation, and the LLM can slip in an un-grounded clause between
two cited ones — the Pydantic validator only checks that *some* citation
is present, not that *every* claim traces to source.

Citation-first generation inverts the flow:

  1. For each retrieved chunk, ask the LLM to extract a list of
     ATOMIC CLAIMS — single-fact statements with a stable subject /
     predicate / value structure.
  2. Each atomic claim is born with its chunk-id parent attached.
  3. The answer composer reads the claim pool and assembles the answer
     by selecting + combining claims; every output sentence has a
     known set of claim parents (and therefore chunk parents).
  4. The LLM is allowed to paraphrase across multiple claims into a
     single sentence, but it cannot introduce a sentence with no
     claim parent — there's nothing to introduce *from*.

This module ships the EXTRACTOR — Phase 1 of the refactor. The composer
(``compose_from_claims``) is also drafted but the *full integration*
into the orchestrator's answer pathway is deliberately deferred. Wiring
it in needs eval-driven A/B against the existing free-text pathway on
the 1500 gap-question set — that's a separate, focused session, not a
rushed swap-out.

Status: production-ready extractor, prototype composer, NOT yet wired
into ``orchestrator/__init__.py``. Flip ``CITATION_FIRST_ENABLED`` to
make it discoverable; the integration in run_deterministic_rag is a
follow-up.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AtomicClaim:
    """A single atomic factual claim with its source chunk parent."""

    text: str
    source_chunk_id: str
    subject: str | None = None      # e.g. "PLS-22-08"
    predicate: str | None = None    # e.g. "has total depth"
    value: str | None = None        # e.g. "510 m"
    confidence: float = 1.0         # extractor's confidence in the claim

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "source_chunk_id": self.source_chunk_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "confidence": self.confidence,
        }


@dataclass
class ClaimPool:
    """All atomic claims extracted from the retrieved chunks for a query."""

    query: str
    claims: list[AtomicClaim] = field(default_factory=list)
    extractor_errors: list[str] = field(default_factory=list)

    def by_chunk(self) -> dict[str, list[AtomicClaim]]:
        out: dict[str, list[AtomicClaim]] = {}
        for c in self.claims:
            out.setdefault(c.source_chunk_id, []).append(c)
        return out

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "claims": [c.to_jsonable() for c in self.claims],
            "extractor_errors": list(self.extractor_errors),
        }


# JSON Schema for vLLM xgrammar guided decoding. Mirrors the prompt's
# "single subject / predicate / value" contract. Engine refuses to emit
# anything that would make `_parse_extractor_json` raise.
_EXTRACTOR_GUIDED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": 400},
                    "subject": {"type": ["string", "null"]},
                    "predicate": {"type": ["string", "null"]},
                    "value": {"type": ["string", "null"]},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}

_EXTRACTOR_SYSTEM_PROMPT = (
    "You are an atomic-claim extractor for a geological RAG system. "
    "Given a chunk of source text and the user's question, extract a "
    "list of ATOMIC CLAIMS that are (a) directly stated in the chunk "
    "and (b) relevant to the user's question. An atomic claim is a "
    "single-fact statement — one subject, one predicate, one value. "
    "Examples of GOOD atomic claims:\n"
    "  - 'PLS-22-08 has a total depth of 510 m'\n"
    "  - 'The Shakespeare Property is 80 km east of Sudbury'\n"
    "  - 'The 2023 resource estimate uses a 0.5% U3O8 cutoff grade'\n"
    "Examples of BAD claims (do NOT extract):\n"
    "  - Multi-fact: 'PLS-22-08 is 510 m deep AND was drilled in 2022'\n"
    "  - Inferred: 'The deposit is high-grade' (unless the chunk says so)\n"
    "  - Vague: 'Drilling was successful' (no measurable predicate)\n"
    "Never invent claims. If the chunk does not address the user's "
    "question, return an empty list."
)


def _build_extractor_prompt(query: str, chunk_text: str) -> str:
    return (
        f"USER QUESTION:\n{query}\n\n"
        f"SOURCE CHUNK:\n{chunk_text[:3000]}\n\n"
        "Extract atomic claims that the chunk directly states AND that "
        "are relevant to the user's question. Return ONLY valid JSON "
        "in this exact shape (no prose, no markdown):\n"
        '{"claims": ['
        '{"text": "<full claim sentence>", '
        '"subject": "<entity or null>", '
        '"predicate": "<relation or null>", '
        '"value": "<value or null>"}'
        ']}'
        "\nReturn at most 8 claims per chunk. Empty list is fine."
    )


def _parse_extractor_json(raw: str, chunk_id: str) -> list[AtomicClaim]:
    text = raw.strip()
    for fence in ("```json", "```JSON", "```"):
        if text.startswith(fence):
            text = text[len(fence):].lstrip()
        if text.endswith("```"):
            text = text[: -len("```")].rstrip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in extractor reply: {raw[:200]!r}")
    parsed = json.loads(text[start : end + 1])
    raw_claims = parsed.get("claims", [])
    if not isinstance(raw_claims, list):
        raise ValueError("'claims' is not a list")
    out: list[AtomicClaim] = []
    for c in raw_claims[:8]:
        if not isinstance(c, dict):
            continue
        claim_text = str(c.get("text", "")).strip()
        if not claim_text or len(claim_text) > 400:
            continue
        out.append(
            AtomicClaim(
                text=claim_text,
                source_chunk_id=chunk_id,
                subject=(c.get("subject") or None) if isinstance(c.get("subject"), str) else None,
                predicate=(c.get("predicate") or None) if isinstance(c.get("predicate"), str) else None,
                value=(c.get("value") or None) if isinstance(c.get("value"), str) else None,
            )
        )
    return out


async def extract_claims_from_chunk(
    query: str,
    chunk_id: str,
    chunk_text: str,
    *,
    anthropic_client: Any = None,
    openai_http_client: Any = None,
    pg_pool: Any = None,
    timeout_s: float | None = None,
) -> list[AtomicClaim]:
    """Run the LLM extractor on one (query, chunk) pair.

    Returns an empty list when the chunk has no relevant claims, when
    the LLM errors out, or when the JSON parse fails — never raises.
    """
    if not chunk_text or not chunk_text.strip():
        return []

    from app.agent.llm_calls import _call_llm  # noqa: PLC0415

    deadline = (
        timeout_s
        if timeout_s is not None
        else settings.CITATION_FIRST_EXTRACTOR_TIMEOUT_S
    )

    try:
        raw = await asyncio.wait_for(
            _call_llm(
                query=_build_extractor_prompt(query, chunk_text),
                context="",
                temperature=0.0,
                anthropic_client=anthropic_client,
                openai_http_client=openai_http_client,
                pg_pool=pg_pool,
                system_prompt=_EXTRACTOR_SYSTEM_PROMPT,
                audit_label="atomic_claim_extractor",
                response_format="json",
                guided_json=_EXTRACTOR_GUIDED_SCHEMA,
                enable_thinking=False,
            ),
            timeout=deadline,
        )
    except TimeoutError:
        logger.warning(
            "atomic_claim_extractor: timeout (%.1fs) for chunk=%s",
            deadline,
            chunk_id[:12],
        )
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "atomic_claim_extractor: LLM error for chunk=%s: %s",
            chunk_id[:12],
            exc,
        )
        return []

    try:
        return _parse_extractor_json(raw, chunk_id)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "atomic_claim_extractor: parse error for chunk=%s: %s",
            chunk_id[:12],
            exc,
        )
        return []


async def build_claim_pool(
    query: str,
    chunks: list[tuple[str, str]],
    *,
    anthropic_client: Any = None,
    openai_http_client: Any = None,
    pg_pool: Any = None,
    concurrency: int | None = None,
) -> ClaimPool:
    """Extract atomic claims from a list of (chunk_id, chunk_text) pairs.

    Calls the extractor in parallel with a concurrency cap so we don't
    stampede the LLM backend with 20 simultaneous chunks.
    """
    pool = ClaimPool(query=query)
    if not chunks:
        return pool

    sem = asyncio.Semaphore(
        concurrency
        if concurrency is not None
        else settings.CITATION_FIRST_EXTRACTOR_CONCURRENCY
    )

    async def _run_one(cid: str, text: str) -> list[AtomicClaim]:
        async with sem:
            return await extract_claims_from_chunk(
                query, cid, text,
                anthropic_client=anthropic_client,
                openai_http_client=openai_http_client,
                pg_pool=pg_pool,
            )

    results = await asyncio.gather(
        *(_run_one(cid, text) for cid, text in chunks),
        return_exceptions=False,
    )
    for claims in results:
        pool.claims.extend(claims)

    logger.info(
        "atomic_claim_extractor: built pool of %d claims from %d chunks",
        len(pool.claims),
        len(chunks),
    )
    return pool


# ---------------------------------------------------------------------------
# Composer (prototype — integration deferred)
# ---------------------------------------------------------------------------

_COMPOSER_SYSTEM_PROMPT = (
    "You are an answer composer for a geological RAG system. You are "
    "given a USER QUESTION and a list of ATOMIC CLAIMS (each tagged with "
    "the source chunk it came from). Compose a coherent answer using "
    "ONLY the claims provided. You may paraphrase, combine, or reorder "
    "claims into flowing sentences, but you MUST NOT introduce any "
    "fact that doesn't appear in the claim list. Every output sentence "
    "must cite the chunk(s) whose claims it derives from, using "
    "[CHUNK-N] markers where N is the chunk's index in the list.\n\n"
    "COMPARISON QUERIES (when the question asks how two projects "
    "differ, compare, or which has more/less of something): structure "
    "the answer as direct statements of fact for each side, then a "
    "concluding sentence that names the difference. Lead with the "
    "side that has more material in the claims. NEVER begin with "
    "phrases like 'I cannot', 'I don't have', 'unable to', 'I can only', "
    "'no data', or any other refusal language — the refusal detector "
    "treats those as a non-answer regardless of the rest of your text. "
    "If one side is genuinely unrepresented in the claims, structure "
    "it as: 'Project A shows [facts]; the retrieved claims do not "
    "describe Project B's [topic], which limits the comparison to "
    "Project A's evidence.' That's a real, useful answer, not a refusal.\n"
    "If the claims don't cover the question at all, lead with what "
    "the claims DO discuss; do not lead with what's missing."
)


async def compose_from_claims(
    query: str,
    claim_pool: ClaimPool,
    *,
    anthropic_client: Any = None,
    openai_http_client: Any = None,
    pg_pool: Any = None,
    timeout_s: float | None = None,
) -> str:
    """Compose an answer from a claim pool. Prototype.

    The composer takes the extracted atomic claims and asks the LLM to
    weave them into a coherent answer. Because every fact in the
    composer's input list traces to a chunk, the resulting answer is
    structurally citation-grounded — no path for the LLM to invent
    a claim, because there's nothing in its input that isn't already
    cited.

    NOT YET integrated into the orchestrator. The integration touches:

      * orchestrator/__init__.py run_deterministic_rag — replace the
        single _call_llm answer call with extract_claims → compose_from_claims
      * response_assembler — adapt to consume claim-shaped output
        (CHUNK-N markers → DATA-X / NI43-X based on chunk source)
      * Layer 2 typed-output validator — adjust to validate against
        the claim pool rather than the citations list

    That integration is a focused 1-day task with eval-driven A/B
    against the free-text pathway on the 1500 gap-question set.
    """
    if not claim_pool.claims:
        return (
            "I couldn't extract any source-grounded claims relevant to "
            "your question from the retrieved passages."
        )

    # Build a numbered list the composer can reference.
    chunk_index: dict[str, int] = {}
    lines: list[str] = []
    for c in claim_pool.claims:
        idx = chunk_index.get(c.source_chunk_id)
        if idx is None:
            idx = len(chunk_index) + 1
            chunk_index[c.source_chunk_id] = idx
        lines.append(f"[CHUNK-{idx}] {c.text}")
    claim_block = "\n".join(lines)

    composer_prompt = (
        f"USER QUESTION:\n{query}\n\n"
        f"ATOMIC CLAIMS:\n{claim_block}\n\n"
        "Compose a coherent answer to the user's question using only the "
        "claims above. Every sentence must end with the [CHUNK-N] marker(s) "
        "whose claims it draws from. Do not invent facts. Do not infer "
        "beyond what the claims state. If the claims don't cover the "
        "question, say so."
    )

    from app.agent.llm_calls import _call_llm  # noqa: PLC0415

    deadline = (
        timeout_s
        if timeout_s is not None
        else settings.CITATION_FIRST_COMPOSER_TIMEOUT_S
    )

    try:
        return await asyncio.wait_for(
            _call_llm(
                query=composer_prompt,
                context="",
                temperature=0.1,
                anthropic_client=anthropic_client,
                openai_http_client=openai_http_client,
                pg_pool=pg_pool,
                system_prompt=_COMPOSER_SYSTEM_PROMPT,
                audit_label="citation_first_composer",
                enable_thinking=False,
            ),
            timeout=deadline,
        )
    except TimeoutError:
        logger.warning("citation_first_composer: timed out after %.1fs", deadline)
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.exception("citation_first_composer: LLM error: %s", exc)
        return ""
