"""§04i hallucination-prevention validators (shared across evaluators).

Each validator is a pure function that takes:
  - The (response_text, citations, raw_response_payload) tuple from
    the evaluator-specific call
  - The QuestionRecord
and returns a ValidatorOutcome.

Validators graduate per-tick:

  Doc-phase 159 — Layer 6 / §2.9 refusal correctness
  Doc-phase 163 — Layer 2 citation presence
  (future)       Layer 3 numeric claims
  (future)       Layer 4 entity resolution
  (future)       Layer 5 chunk provenance
  (future)       Layer 1 retrieval quality

The evaluator chains validators with AND semantics on `passed`. The
first failing validator's layer goes on `QuestionResult.failure_layer`.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from app.services.eval.workspace_evaluator import QuestionRecord


# Refusal patterns. Case-insensitive substring match.
REFUSAL_PATTERNS: list[str] = [
    "i cannot",
    "i can't",
    "i am unable",
    "i'm unable",
    "i do not have",
    "i don't have",
    "cannot provide",
    "unable to provide",
    "not able to",
    "no information available",
    "no public data within 25",  # §2.9 public/private boundary template
    "insufficient evidence",
    "no answer is available",
    "i cannot answer",
    "i'm not able to answer",
    # Orchestrator refusal phrases (doc-phase 162).
    "i can only answer",
    "not possible",
    "evidence does not support",
    "not referenced in the provided",
    "no such data",
    "no data available",
]


class ValidatorOutcome(NamedTuple):
    """Result of one §04i validator.

    Attributes:
        layer: '6_refusal' | '2_citation_presence' | '3_numeric' | '4_entity'
            | '5_provenance' | '1_retrieval'. Becomes the prefix of
            QuestionResult.failure_layer when this validator fails.
        passed: True if the validator's check is satisfied.
        detail: Free-form structured payload — surfaced in actual_payload.
        failure_message: One-line human-readable failure reason when
            passed is False. None when passed.
    """

    layer: str
    passed: bool
    detail: dict[str, Any]
    failure_message: str | None


def detect_refusal(text: str) -> bool:
    """Return True if the response text reads as a refusal.

    Pattern-match against `REFUSAL_PATTERNS` (case-insensitive substring).

    Doc-phase 186 (Phase E.3.1) — substance-vs-disclaimer heuristic:
    the orchestrator sometimes appends a refusal disclaimer (e.g. "I
    can only answer geological questions") to the END of an answer
    that otherwise contains substantive content. A pure substring
    match flags those as refusals, even though the actual answer is
    above the disclaimer.

    The fix: if a refusal phrase appears, check whether it's in the
    LAST 20% of the response. If yes AND there's substantive content
    before it (≥200 chars), treat it as a disclaimer (not a refusal).
    """
    if not text:
        return False
    lower = text.lower()
    for pattern in REFUSAL_PATTERNS:
        idx = lower.find(pattern)
        if idx == -1:
            continue
        # Pattern found. Determine if it's a disclaimer at the tail
        # of a substantive answer.
        text_len = len(lower)
        is_in_tail = idx >= text_len * 0.80  # last 20%
        has_substance_before = idx >= 200    # ≥200 chars of content prior
        if is_in_tail and has_substance_before:
            # Treat as appended disclaimer; not a refusal.
            continue
        return True
    return False


def validate_refusal_correctness(
    *,
    response_text: str,
    question: QuestionRecord,
) -> ValidatorOutcome:
    """§04i Layer 6 / §2.9 — graduated doc-phase 159.

    Detects refusal in the response text. Compares to
    `question.expected_refusal`. Passes iff they match.
    """
    detected = detect_refusal(response_text)
    matches = detected == question.expected_refusal
    return ValidatorOutcome(
        layer="6_refusal",
        passed=matches,
        detail={
            "detected_refusal": detected,
            "expected_refusal": question.expected_refusal,
        },
        failure_message=(
            None
            if matches
            else (
                f"expected_refusal={question.expected_refusal} "
                f"but detected_refusal={detected}"
            )
        ),
    )


def validate_citation_presence(
    *,
    citations: list[Any],
    question: QuestionRecord,
) -> ValidatorOutcome:
    """§04i Layer 2 typed-output / citation presence — graduated doc-phase 163.

    Per master-plan §04i and CLAUDE.md hard-rule #4: "Citations are
    mandatory on every RAG response. Every claim the LLM makes must
    include a source_chunk_id or be rejected by Pydantic AI's typed
    output validation. There is no 'best-effort' citation mode."

    Two cases:
      - expected_refusal=False (answer expected) → must have ≥1 citation
      - expected_refusal=True (refusal expected) → citations optional;
        validator passes vacuously

    When `expected_citations` is non-empty (SME-authored questions),
    we additionally require `len(citations) >= len(expected_citations)`.
    For mechanical seed questions where expected_citations is empty,
    this clause is a no-op.
    """
    n_actual = len(citations)
    n_expected = len(question.expected_citations or [])

    # Vacuous pass: refusal-only path bypasses citation requirement.
    if question.expected_refusal:
        return ValidatorOutcome(
            layer="2_citation_presence",
            passed=True,
            detail={
                "citation_count": n_actual,
                "expected_citation_count": n_expected,
                "vacuous_pass_refusal_path": True,
            },
            failure_message=None,
        )

    # Layer 2 hard rule: ≥1 citation when not refusing.
    if n_actual == 0:
        return ValidatorOutcome(
            layer="2_citation_presence",
            passed=False,
            detail={
                "citation_count": 0,
                "expected_citation_count": n_expected,
            },
            failure_message=(
                "Layer 2 violation: non-refusal response has zero "
                "citations. §04i requires at least one source_chunk_id "
                "per response."
            ),
        )

    # Optional stronger check: enough citations to cover expected_citations.
    if n_expected > 0 and n_actual < n_expected:
        return ValidatorOutcome(
            layer="2_citation_presence",
            passed=False,
            detail={
                "citation_count": n_actual,
                "expected_citation_count": n_expected,
            },
            failure_message=(
                f"Layer 2 violation: response has {n_actual} citations "
                f"but expected at least {n_expected} per question spec."
            ),
        )

    return ValidatorOutcome(
        layer="2_citation_presence",
        passed=True,
        detail={
            "citation_count": n_actual,
            "expected_citation_count": n_expected,
        },
        failure_message=None,
    )


async def validate_chunk_provenance(
    *,
    citations: list[Any],
    qdrant_client: Any,
    qdrant_collection: str = "georag_reports",
    question: QuestionRecord,
) -> ValidatorOutcome:
    """§04i Layer 5 chunk provenance — graduated doc-phase 165.

    Each citation's `source_chunk_id` must resolve to a real chunk in
    Qdrant. Catches hallucinated source IDs that look valid but don't
    exist in the vector store.

    Behavior:
      - `expected_refusal=True` + zero citations → vacuous pass
      - Each `source_chunk_id` is looked up in Qdrant via `.retrieve()`
      - Citations with `corpus='public_geo'` are skipped (those
        IDs aren't Qdrant point ids; a future graduation adds a
        PostGIS-backed lookup)
      - Pass iff every Qdrant-bound citation resolves to a real point
    """
    # Vacuous pass on refusal-expected questions: refusal responses
    # often carry sentinel citations (e.g. "georag_reports:empty")
    # that are not real Qdrant chunks — penalising them duplicates
    # what Layer 6 already catches.
    if question.expected_refusal:
        return ValidatorOutcome(
            layer="5_chunk_provenance",
            passed=True,
            detail={
                "citation_count": len(citations),
                "qdrant_lookups": 0,
                "vacuous_pass_refusal_path": True,
            },
            failure_message=None,
        )

    # Coerce each citation to (source_chunk_id, corpus, citation_type,
    # citation_id) so we can skip DATA + PGEO types appropriately.
    def _coerce(c: Any) -> tuple[str, str | None, str | None, str]:
        if hasattr(c, "source_chunk_id"):
            return (
                c.source_chunk_id,
                getattr(c, "corpus", None),
                getattr(c, "citation_type", None),
                getattr(c, "citation_id", c.source_chunk_id),
            )
        return (
            c.get("source_chunk_id", ""),
            c.get("corpus"),
            c.get("citation_type"),
            c.get("citation_id", c.get("source_chunk_id", "")),
        )

    qdrant_lookups = 0
    qdrant_resolved = 0
    pgeo_skipped = 0
    sql_skipped = 0
    unresolved: list[dict[str, Any]] = []

    for c in citations:
        chunk_id, corpus, citation_type, citation_id = _coerce(c)
        if corpus == "public_geo":
            # PGEO IDs are structured strings, not Qdrant point IDs.
            pgeo_skipped += 1
            continue
        if citation_type == "DATA":
            # DATA citations point at silver.* SQL tables, not Qdrant.
            # The doc-phase 167 SQL-provenance layer will validate these.
            sql_skipped += 1
            continue
        qdrant_lookups += 1
        try:
            points = await qdrant_client.retrieve(
                collection_name=qdrant_collection,
                ids=[chunk_id],
                with_payload=False,
                with_vectors=False,
            )
            if points:
                qdrant_resolved += 1
            else:
                unresolved.append({
                    "citation_id": citation_id,
                    "source_chunk_id": chunk_id,
                    "reason": "not_found_in_qdrant",
                })
        except Exception as e:  # noqa: BLE001
            unresolved.append({
                "citation_id": citation_id,
                "source_chunk_id": chunk_id,
                "reason": f"qdrant_error: {type(e).__name__}: {str(e)[:80]}",
            })

    if unresolved:
        return ValidatorOutcome(
            layer="5_chunk_provenance",
            passed=False,
            detail={
                "citation_count": len(citations),
                "qdrant_lookups": qdrant_lookups,
                "qdrant_resolved": qdrant_resolved,
                "pgeo_skipped": pgeo_skipped,
                "sql_skipped": sql_skipped,
                "unresolved": unresolved[:10],
                "unresolved_count": len(unresolved),
            },
            failure_message=(
                f"Layer 5 violation: {len(unresolved)} of {qdrant_lookups} "
                f"Qdrant citations failed to resolve in collection "
                f"'{qdrant_collection}'. First: {unresolved[0]['source_chunk_id']!r} "
                f"({unresolved[0]['reason']})"
            ),
        )

    return ValidatorOutcome(
        layer="5_chunk_provenance",
        passed=True,
        detail={
            "citation_count": len(citations),
            "qdrant_lookups": qdrant_lookups,
            "qdrant_resolved": qdrant_resolved,
            "pgeo_skipped": pgeo_skipped,
            "sql_skipped": sql_skipped,
        },
        failure_message=None,
    )


def _extract_entity_names(expected_entities: list[Any]) -> list[str]:
    """Pull human-readable entity names out of the structured
    expected_entities list.

    SME-style entries have a `name` (or `entity_name`) field with a
    string value to search for in the response text. Mechanical /
    structural entries (e.g. `{"expected_route": "accept"}` for
    ocr_triage, `{"required_section_ids": [...]}` for report_section)
    return no extractable names — Layer 4 vacuously passes on those.
    """
    names: list[str] = []
    for entry in expected_entities or []:
        if not isinstance(entry, dict):
            continue
        for key in ("name", "entity_name", "expected_value"):
            v = entry.get(key)
            if isinstance(v, str) and v.strip():
                names.append(v.strip())
                break
    return names


def validate_entity_resolution(
    *,
    response_text: str,
    question: QuestionRecord,
) -> ValidatorOutcome:
    """§04i Layer 4 entity resolution — graduated doc-phase 166.

    Checks that every named entity in `question.expected_entities`
    appears in the response text. Names are matched case-insensitive
    as substrings.

    Vacuous pass cases:
      - `expected_refusal=True` — refusal exempts entity checks
      - `_extract_entity_names()` returns no names (structural specs
        without a `name`/`entity_name`/`expected_value` field — common
        for mechanical question sets)

    SME-authored questions land with entries like:
        {"entity_kind": "rock", "name": "Athabasca Sandstone"}
    and the validator picks them up automatically.
    """
    if question.expected_refusal:
        return ValidatorOutcome(
            layer="4_entity_resolution",
            passed=True,
            detail={
                "expected_entity_count": len(question.expected_entities or []),
                "extractable_names": [],
                "vacuous_pass_refusal_path": True,
            },
            failure_message=None,
        )

    names = _extract_entity_names(question.expected_entities)
    if not names:
        return ValidatorOutcome(
            layer="4_entity_resolution",
            passed=True,
            detail={
                "expected_entity_count": len(question.expected_entities or []),
                "extractable_names": [],
                "vacuous_pass_no_extractable_names": True,
            },
            failure_message=None,
        )

    lower_text = (response_text or "").lower()
    missing = [n for n in names if n.lower() not in lower_text]

    if missing:
        return ValidatorOutcome(
            layer="4_entity_resolution",
            passed=False,
            detail={
                "expected_entity_count": len(question.expected_entities or []),
                "extractable_names": names,
                "missing_entities": missing,
            },
            failure_message=(
                f"Layer 4 violation: {len(missing)} expected "
                f"{'entity' if len(missing) == 1 else 'entities'} not "
                f"found in response: {missing[:3]}"
            ),
        )

    return ValidatorOutcome(
        layer="4_entity_resolution",
        passed=True,
        detail={
            "expected_entity_count": len(question.expected_entities or []),
            "extractable_names": names,
            "all_entities_found": True,
        },
        failure_message=None,
    )


import re

# Match decimals, percentages, negatives. Excludes years-as-numbers
# by requiring a decimal point or unit-following pattern.
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _extract_numbers(text: str) -> list[float]:
    """Pull all decimal numbers out of a text. Returns floats."""
    return [float(m) for m in _NUMBER_RE.findall(text or "")]


def validate_numeric_claims(
    *,
    response_text: str,
    question: QuestionRecord,
) -> ValidatorOutcome:
    """§04i Layer 3 numeric-claim verification — graduated doc-phase 167.

    For each entry in `question.expected_numeric_values`, either:
      - If the entry has an `expected_value` (concrete ground truth),
        scan the response for any number within `tolerance_pct` of it.
        Pass iff every expected_value finds a match in the response.
      - If the entry has only structural keys (path/source_table —
        ground truth computed from silver tables), Layer 3 reports
        `vacuous_pass_needs_silver_data` for that entry. Full
        ground-truth computation lands when project ingestion +
        silver-tracked sources are wired in.

    Vacuous pass on:
      - `expected_refusal=True`
      - `expected_numeric_values` is empty
      - all entries lack `expected_value` (structural-only specs)
    """
    if question.expected_refusal:
        return ValidatorOutcome(
            layer="3_numeric_claims",
            passed=True,
            detail={
                "expected_count": len(question.expected_numeric_values or []),
                "checked_count": 0,
                "vacuous_pass_refusal_path": True,
            },
            failure_message=None,
        )

    if not question.expected_numeric_values:
        return ValidatorOutcome(
            layer="3_numeric_claims",
            passed=True,
            detail={
                "expected_count": 0,
                "checked_count": 0,
                "vacuous_pass_no_expectations": True,
            },
            failure_message=None,
        )

    # Find entries with concrete expected_value (caller-supplied ground truth).
    checkable: list[dict[str, Any]] = []
    structural_only = 0
    for entry in question.expected_numeric_values:
        if isinstance(entry, dict) and isinstance(entry.get("expected_value"), (int, float)):
            checkable.append(entry)
        else:
            structural_only += 1

    if not checkable:
        return ValidatorOutcome(
            layer="3_numeric_claims",
            passed=True,
            detail={
                "expected_count": len(question.expected_numeric_values),
                "structural_only_count": structural_only,
                "vacuous_pass_needs_silver_data": True,
            },
            failure_message=None,
        )

    response_numbers = _extract_numbers(response_text)
    misses: list[dict[str, Any]] = []

    for entry in checkable:
        expected_val = float(entry["expected_value"])
        tol_pct = float(entry.get("tolerance_pct", 0.0))
        # If tol_pct = 0, demand exact (within float epsilon).
        if tol_pct <= 0:
            matched = any(abs(n - expected_val) < 1e-9 for n in response_numbers)
        else:
            tolerance = abs(expected_val) * (tol_pct / 100.0)
            matched = any(abs(n - expected_val) <= tolerance for n in response_numbers)
        if not matched:
            misses.append({
                "expected_value": expected_val,
                "tolerance_pct": tol_pct,
                "path": entry.get("path"),
                "unit": entry.get("unit"),
            })

    if misses:
        return ValidatorOutcome(
            layer="3_numeric_claims",
            passed=False,
            detail={
                "expected_count": len(question.expected_numeric_values),
                "checkable_count": len(checkable),
                "response_numbers": response_numbers[:20],
                "misses": misses,
            },
            failure_message=(
                f"Layer 3 violation: {len(misses)} expected numeric "
                f"value(s) not found in response. First miss: "
                f"{misses[0]['expected_value']!r} (path={misses[0].get('path')})"
            ),
        )

    return ValidatorOutcome(
        layer="3_numeric_claims",
        passed=True,
        detail={
            "expected_count": len(question.expected_numeric_values),
            "checkable_count": len(checkable),
            "all_values_found": True,
        },
        failure_message=None,
    )


def validate_retrieval_quality(
    *,
    citations: list[Any],
    question: QuestionRecord,
    min_relevance_score: float = 0.5,
) -> ValidatorOutcome:
    """§04i Layer 1 retrieval-quality gate — graduated doc-phase 168.

    Per master-plan §04i Layer 1: chunks below the retrieval quality
    gate threshold must not appear as citations. Default threshold
    matches the existing chat-side cross-encoder gate (0.5).

    Behavior:
      - `expected_refusal=True` → vacuous pass (sentinel citations
        often have synthesized relevance scores)
      - Any citation with `relevance_score < min_relevance_score` →
        fail with the offending citation_id surfaced
      - Citations lacking a `relevance_score` field are treated as
        unscored (skipped — Layer 1 only gates what was scored)
    """
    if question.expected_refusal:
        return ValidatorOutcome(
            layer="1_retrieval_quality",
            passed=True,
            detail={
                "citation_count": len(citations),
                "min_relevance_score": min_relevance_score,
                "vacuous_pass_refusal_path": True,
            },
            failure_message=None,
        )

    def _get_score(c: Any) -> float | None:
        if hasattr(c, "relevance_score"):
            return c.relevance_score
        if isinstance(c, dict):
            v = c.get("relevance_score")
            return float(v) if isinstance(v, (int, float)) else None
        return None

    def _get_id(c: Any) -> str:
        if hasattr(c, "citation_id"):
            return c.citation_id
        if isinstance(c, dict):
            return c.get("citation_id", c.get("source_chunk_id", "<unknown>"))
        return "<unknown>"

    below_gate: list[dict[str, Any]] = []
    scored_count = 0
    unscored_count = 0

    for c in citations:
        score = _get_score(c)
        if score is None:
            unscored_count += 1
            continue
        scored_count += 1
        if score < min_relevance_score:
            below_gate.append({
                "citation_id": _get_id(c),
                "relevance_score": score,
            })

    if below_gate:
        return ValidatorOutcome(
            layer="1_retrieval_quality",
            passed=False,
            detail={
                "citation_count": len(citations),
                "scored_count": scored_count,
                "unscored_count": unscored_count,
                "min_relevance_score": min_relevance_score,
                "below_gate": below_gate,
            },
            failure_message=(
                f"Layer 1 violation: {len(below_gate)} citation(s) "
                f"below relevance_score gate {min_relevance_score}. "
                f"First: {below_gate[0]['citation_id']} "
                f"(score={below_gate[0]['relevance_score']:.3f})"
            ),
        )

    return ValidatorOutcome(
        layer="1_retrieval_quality",
        passed=True,
        detail={
            "citation_count": len(citations),
            "scored_count": scored_count,
            "unscored_count": unscored_count,
            "min_relevance_score": min_relevance_score,
        },
        failure_message=None,
    )


def chain_validators(outcomes: list[ValidatorOutcome]) -> tuple[bool, str | None, str | None]:
    """Combine validator outcomes with AND semantics.

    Returns:
        (all_passed, failure_layer, failure_message)
        - all_passed: True iff every outcome passed
        - failure_layer: first failing outcome's `layer` (None if all passed)
        - failure_message: first failing outcome's message (None if all passed)
    """
    for outcome in outcomes:
        if not outcome.passed:
            return False, outcome.layer, outcome.failure_message
    return True, None, None


__all__ = [
    "REFUSAL_PATTERNS",
    "ValidatorOutcome",
    "chain_validators",
    "detect_refusal",
    "validate_chunk_provenance",
    "validate_citation_presence",
    "validate_entity_resolution",
    "validate_numeric_claims",
    "validate_refusal_correctness",
    "validate_retrieval_quality",
]
