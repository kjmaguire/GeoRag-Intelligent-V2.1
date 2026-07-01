"""Guard 3 — Per-claim citation completeness (§04i Layer — Global Invariant 1).
Guard 4 — Refusal meta-guard (structured refusal on any guard failure).

Module 6 Phase B Chunk 3 — new guards per spec B2.

Architecture reference: georag-architecture-addendum-v1.10.html §04i;
module spec 06-citation-hallucination-guards.md §6 B2, B4.

Purpose
-------
Guard 3 (Completeness):
    Split the answer into sentences and verify that every declarative sentence
    either (a) contains a citation marker, or (b) is immediately followed by a
    sentence that opens with a marker.  Bare-assertion sentences are flagged.

    This is the enforcement arm of the system-prompt promise:
    "Every factual claim in your answer MUST include an inline citation marker."
    Without this guard, the completeness promise is made to the model but never
    verified post-hoc (GUARD-01 from the Phase A audit).

Guard 4 (Refusal meta-guard):
    Collects results from Guards 1-3 (numeric, entity, completeness) and emits
    a structured ``GuardBundle``.  The orchestrator uses ``GuardBundle.all_passed``
    to decide whether to transition to 'validated' or 'rejected'.

    The structured refusal payload (spec B4) is assembled by ``format_guard_failure``;
    Chunk 4 will expand the payload shape.  This chunk places the stub.

Design
------
- No NER model dep: proper-noun detection is regex-based (TitleCase heuristic).
- No nltk dep: sentence splitting is regex-based (re.split on punctuation).
- Refusal phrases dictionary: a small fixed set of known refusal patterns that
  are exempt from the completeness guard (they contain no facts to cite).
- All guard results carry a ``guard_name`` string for the rejection_reason column.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colon-form and dash-form citation marker pattern.
# Matches [DATA:1], [NI43:2], [PUB:3], [PGEO:4], [ev:abc1def2],
# and legacy dash-form [DATA-1] etc. (still present during rollout window).
# ---------------------------------------------------------------------------
_MARKER_RE = re.compile(r"\[(?:DATA|NI43|PUB|PGEO|ev)[:-][A-Za-z0-9-]+\]")

# Sentence splitter — splits on . ! ? followed by whitespace.
# Simple regex is intentional: no nltk dep, no spacy dep.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Refusal phrases that are exempt from the completeness guard.
# These sentences contain no factual claims and thus need no citation marker.
_REFUSAL_PHRASES: frozenset[str] = frozenset({
    "i don't have data on that",
    "i don't have enough information",
    "i cannot find information",
    "no information is available",
    "insufficient information",
    "i was unable to generate",
    "i can only answer geological",
    "the language model is currently unavailable",
    "please try again",
    "no data found",
    "no records found",
    "no results found",
    "based on the available data",
    "based on the provided context",
})

# Imperative/transitional phrases — exempt from completeness guard.
_IMPERATIVE_STARTERS: frozenset[str] = frozenset({
    "see table",
    "see figure",
    "refer to",
    "note that",
    "please note",
    "for more detail",
    "for further",
    "in summary",
    "in conclusion",
    "to summarize",
    "as shown",
    "as noted",
})


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class GuardResult:
    """Result from a single hallucination guard."""
    guard_name: str
    passed: bool
    # Guard-specific failure details — populated by each guard.
    failed_tokens: list[str] = field(default_factory=list)      # Layer 3
    failed_entities: list[str] = field(default_factory=list)    # Layer 4
    uncited_sentences: list[str] = field(default_factory=list)  # completeness
    derivation_log: list[str] = field(default_factory=list)     # Layer 3


@dataclass
class GuardBundle:
    """Aggregate result from all four guards.

    ``all_passed`` is True only when every individual guard passed.
    The orchestrator checks this to decide validated vs rejected transition.
    """
    all_passed: bool
    numeric: GuardResult
    entity: GuardResult
    completeness: GuardResult
    failed_guards: list[GuardResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Guard 3 — Completeness
# ---------------------------------------------------------------------------

def _is_exempt(sentence: str) -> bool:
    """Return True if the sentence is exempt from the completeness guard.

    Exempt sentences:
      - Questions (end with ?)
      - Refusal phrases (no facts to cite)
      - Imperative / transitional starters ("See Table 3…")
      - Very short sentences (< 5 chars) — likely headings or fragments
    """
    stripped = sentence.strip()
    if not stripped:
        return True
    if stripped.endswith("?"):
        return True
    if len(stripped) < 5:
        return True
    lowered = stripped.lower()
    for phrase in _REFUSAL_PHRASES:
        if phrase in lowered:
            return True
    return any(lowered.startswith(starter) for starter in _IMPERATIVE_STARTERS)


def _has_marker(sentence: str) -> bool:
    """Return True if the sentence contains at least one citation marker."""
    return bool(_MARKER_RE.search(sentence))


def verify_completeness(answer_text: str) -> GuardResult:
    """Guard 3: Every declarative sentence must have a citation marker.

    Per spec B2: split answer into sentences; each declarative sentence must
    have at least one citation marker within it OR in the immediately following
    sentence.  Bare-assertion sentence → guard fail.

    Phase F.5: strip the proactive-insights block before sentence-splitting.
    Insight bullets are deterministic system output (computed from raw
    tool_results data) and aren't part of the LLM's surface — they carry
    aggregate-trace markers if the orchestrator appended them, but the
    completeness guard is only meant to catch *LLM* bare assertions.

    Args:
        answer_text: The LLM answer text (normalized, post-dash-rewrite).

    Returns:
        GuardResult with passed=True if all declarative sentences are cited,
        or passed=False with uncited_sentences populated.
    """
    from app.agent.anomaly_detector import strip_proactive_insights  # noqa: PLC0415
    answer_text = strip_proactive_insights(answer_text)

    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(answer_text) if s.strip()]

    uncited: list[str] = []

    for i, sentence in enumerate(sentences):
        # Skip exempt sentences.
        if _is_exempt(sentence):
            continue

        # Does this sentence contain a marker?
        if _has_marker(sentence):
            continue

        # Does the next sentence open with a marker?
        if i + 1 < len(sentences):
            next_sent = sentences[i + 1].strip()
            if _MARKER_RE.match(next_sent) or _has_marker(next_sent[:40]):
                # Next sentence provides the citation for this one — OK.
                continue

        # No marker in this sentence or the next — bare assertion.
        uncited.append(sentence[:200])  # truncate for storage

    passed = len(uncited) == 0
    if not passed:
        logger.warning(
            "layer_completeness: %d uncited declarative sentence(s) found",
            len(uncited),
        )
    else:
        logger.debug("layer_completeness: all declarative sentences have citations")

    return GuardResult(
        guard_name="completeness",
        passed=passed,
        uncited_sentences=uncited,
    )


# ---------------------------------------------------------------------------
# Guard 4 — Refusal meta-guard
# ---------------------------------------------------------------------------

async def evaluate_guards(
    *,
    answer_text: str,
    tool_results: list,
    project_id: str,
    pg_pool: object,
    neo4j_driver: object,
    query_class: str | None = None,
) -> GuardBundle:
    """Guard 4: Evaluate all three content guards and aggregate into a GuardBundle.

    Runs Guards 1-3 (numeric, entity, completeness) and returns a bundle.
    The orchestrator uses ``bundle.all_passed`` to decide lifecycle transition.

    This function is called AFTER ``run_post_assembly_validation()`` has already
    fired (for LLM retry decisions).  It re-uses the same guard logic but
    returns structured GuardResult objects instead of raw warning strings,
    which feed the rejection_reason column and the refusal payload.

    Args:
        answer_text:    Canonical answer text (normalized, spans index into this).
        tool_results:   Tool results from orchestrator fan-out.
        project_id:     Current project UUID string.
        pg_pool:        asyncpg Pool (or None).
        neo4j_driver:   Neo4j async driver (or None).

    Returns:
        GuardBundle with all_passed and individual guard results.
    """
    import asyncio
    import time as _time

    from app.agent.hallucination.orchestrator_validators import (
        verify_entities as _verify_entities,
    )
    from app.agent.hallucination.orchestrator_validators import (
        verify_numbers as _verify_numbers,
    )

    # Module 6 Chunk 3.5 — parallel guard evaluation.
    #
    # Before (sequential): numeric(5s) + entity(30s via Neo4j) + completeness(2s) = ~37s
    # After  (parallel):   max(5s, 30s, 2s) = ~30s.  Saves 7-10s on dev GPU.
    #
    # Conservative exception posture (§04i spec): if any guard raises, treat as
    # *failed* (not passed). Log at WARNING with exc_info for post-hoc debugging.
    # Sync guards (numeric, completeness) are wrapped in asyncio.to_thread so they
    # don't block the event loop during the gather.

    t_guards_start = _time.monotonic()

    numeric_task = asyncio.to_thread(_verify_numbers, answer_text, tool_results)
    entity_task = _verify_entities(
        answer_text,
        project_id,
        pg_pool,
        neo4j_driver,
        tool_results=tool_results,
    )
    completeness_task = asyncio.to_thread(verify_completeness, answer_text)

    raw_numeric, raw_entity, raw_completeness = await asyncio.gather(
        numeric_task,
        entity_task,
        completeness_task,
        return_exceptions=True,
    )

    logger.info(
        "evaluate_guards: parallel evaluation completed in %.2fs",
        _time.monotonic() - t_guards_start,
    )

    # Doc-phase 186 — guard tolerance thresholds.
    # The strict "any failure → reject" behavior produces false positives
    # on noisy/fragmented retrieval contexts. Tolerance allows up to N
    # soft failures per guard before the bundle is marked rejected.
    from app.config import settings as _settings
    NUMERIC_TOL = getattr(_settings, "GUARD_TOLERANCE_NUMERIC_UNGROUNDED", 0)
    ENTITY_TOL = getattr(_settings, "GUARD_TOLERANCE_ENTITY_UNRESOLVED", 0)
    COMPLETENESS_TOL = getattr(_settings, "GUARD_TOLERANCE_COMPLETENESS_UNCITED", 0)

    # Eval 01 P3 — per-class tolerance overrides.
    # Different query classes have different evidence shapes:
    #   exploratory queries → coverage is sparse by design; loosen completeness
    #   computational queries → numbers are derived (avg, sum); loosen numeric
    #   factual queries → tighten everything; a fact must be cited
    # The base tolerances above are the global defaults; the per-class table
    # below additively overrides them. Unknown classes fall back to globals.
    _per_class_overrides: dict[str, dict[str, int]] = {
        "factual":      {"numeric": 0, "entity": 0, "completeness": 0},
        "computational":{"numeric": 3, "entity": 0, "completeness": 1},
        "exploratory":  {"numeric": 1, "entity": 1, "completeness": 3},
        "comparison":   {"numeric": 1, "entity": 0, "completeness": 1},
        "trend":        {"numeric": 2, "entity": 1, "completeness": 2},
    }
    if query_class and query_class in _per_class_overrides:
        _ov = _per_class_overrides[query_class]
        NUMERIC_TOL = max(NUMERIC_TOL, _ov["numeric"])
        ENTITY_TOL = max(ENTITY_TOL, _ov["entity"])
        COMPLETENESS_TOL = max(COMPLETENESS_TOL, _ov["completeness"])
        logger.info(
            "evaluate_guards: per-class tolerances active (class=%s, "
            "numeric=%d entity=%d completeness=%d)",
            query_class, NUMERIC_TOL, ENTITY_TOL, COMPLETENESS_TOL,
        )

    # --- Build GuardResult for numeric ---
    if isinstance(raw_numeric, BaseException):
        logger.warning(
            "evaluate_guards: numeric guard raised — treating as failed",
            exc_info=raw_numeric,
        )
        numeric_result = GuardResult(
            guard_name="numeric",
            passed=False,
            derivation_log=[f"guard_exception: {raw_numeric!r}"],
        )
    else:
        numeric_warnings: list[str] = raw_numeric  # type: ignore[assignment]
        # Tolerance: pass if ungrounded count <= NUMERIC_TOL
        numeric_passed = len(numeric_warnings) <= NUMERIC_TOL
        if numeric_warnings and numeric_passed:
            logger.info(
                "evaluate_guards: numeric guard within tolerance — "
                "%d ungrounded number(s) <= tolerance=%d",
                len(numeric_warnings), NUMERIC_TOL,
            )
        numeric_result = GuardResult(
            guard_name="numeric",
            passed=numeric_passed,
            failed_tokens=[
                w.split("Ungrounded number ")[1].split(" in response")[0]
                for w in numeric_warnings
                if "Ungrounded number" in w
            ],
            derivation_log=numeric_warnings,
        )

    # --- Build GuardResult for entity ---
    if isinstance(raw_entity, BaseException):
        logger.warning(
            "evaluate_guards: entity guard raised — treating as failed",
            exc_info=raw_entity,
        )
        entity_result = GuardResult(
            guard_name="entity",
            passed=False,
            failed_entities=[f"guard_exception: {raw_entity!r}"],
        )
    else:
        # Tolerance: pass if unresolved entity count <= ENTITY_TOL
        entity_passed = len(raw_entity) <= ENTITY_TOL  # type: ignore[arg-type]
        if raw_entity and entity_passed:
            logger.info(
                "evaluate_guards: entity guard within tolerance — "
                "%d unresolved entity(ies) <= tolerance=%d",
                len(raw_entity), ENTITY_TOL,  # type: ignore[arg-type]
            )
        entity_result = GuardResult(
            guard_name="entity",
            passed=entity_passed,
            failed_entities=list(raw_entity),  # type: ignore[arg-type]
        )

    # --- Build GuardResult for completeness ---
    if isinstance(raw_completeness, BaseException):
        logger.warning(
            "evaluate_guards: completeness guard raised — treating as failed",
            exc_info=raw_completeness,
        )
        completeness_result = GuardResult(
            guard_name="completeness",
            passed=False,
            uncited_sentences=[f"guard_exception: {raw_completeness!r}"],
        )
    else:
        completeness_result = raw_completeness  # type: ignore[assignment]
        # Re-evaluate completeness passed status with tolerance applied
        if not completeness_result.passed:
            uncited_count = len(completeness_result.uncited_sentences)
            if uncited_count <= COMPLETENESS_TOL:
                logger.info(
                    "evaluate_guards: completeness guard within tolerance — "
                    "%d uncited sentence(s) <= tolerance=%d",
                    uncited_count, COMPLETENESS_TOL,
                )
                # Construct a new GuardResult with passed=True (dataclass is
                # frozen-style but mutable; we mutate in place).
                completeness_result.passed = True

    all_passed = (
        numeric_result.passed
        and entity_result.passed
        and completeness_result.passed
    )
    failed = [
        g for g in [numeric_result, entity_result, completeness_result]
        if not g.passed
    ]

    return GuardBundle(
        all_passed=all_passed,
        numeric=numeric_result,
        entity=entity_result,
        completeness=completeness_result,
        failed_guards=failed,
    )


def format_guard_failure(failed_guards: list[GuardResult]) -> str:
    """Format a structured rejection_reason string for the failed guards.

    Chunk 4 expands the full B4 refusal payload shape.
    Chunk 3 places this stub: a compact, human-readable failure description.

    Args:
        failed_guards: List of GuardResult objects where passed=False.

    Returns:
        Rejection reason string suitable for storage in answer_runs.rejection_reason.
    """
    parts: list[str] = []
    for g in failed_guards:
        if g.guard_name == "numeric":
            tokens = g.failed_tokens[:5]  # max 5 tokens in reason
            parts.append(
                f"numeric_guard: {len(g.failed_tokens)} ungrounded number(s)"
                + (f" [{', '.join(tokens)}]" if tokens else "")
            )
        elif g.guard_name == "entity":
            parts.append(
                f"entity_guard: {len(g.failed_entities)} unresolved entity(ies)"
            )
        elif g.guard_name == "completeness":
            parts.append(
                f"completeness_guard: {len(g.uncited_sentences)} uncited sentence(s)"
            )
        else:
            parts.append(f"{g.guard_name}_guard: failed")
    return "; ".join(parts) if parts else "guard_failure"


def build_refusal_payload(bundle: GuardBundle) -> dict:
    """Synchronous fallback stub for the guard refusal payload.

    Chunk 3 placed this stub.  Chunk 4a supersedes it with the async
    ``build_guard_refusal_payload()`` in ``app.services.refusal_builder``.

    This stub is retained as an absolute fallback used only when the async
    builder raises an unexpected exception.  It does NOT populate the
    ``searched`` or ``missing`` blocks (no DB access available).

    Returns:
        Minimal dict with type='refusal', reason_code, failed_guards, message.
        Missing ``searched``/``missing`` blocks are a defect if this path is
        hit — check the orchestrator WARNING log for the builder exception.
    """
    guard_names = [g.guard_name for g in bundle.failed_guards]
    if "numeric" in guard_names:
        reason_code = "guard_numeric_fail"
    elif "entity" in guard_names:
        reason_code = "guard_entity_fail"
    elif "completeness" in guard_names:
        reason_code = "guard_completeness_fail"
    else:
        reason_code = "guard_completeness_fail"

    return {
        "type": "refusal",
        "reason_code": reason_code,
        "searched": {
            "stores_queried": [],
            "candidates_considered": 0,
            "query_class": "unknown",
        },
        "missing": {
            "what_was_needed": "Sufficient grounded evidence (fallback stub — DB lookup failed).",
            "nearest_candidates": [],
        },
        "failed_guards": guard_names,
        "message": (
            "We can't answer this from your corpus. "
            "Failure details: " + format_guard_failure(bundle.failed_guards)
        ),
    }
