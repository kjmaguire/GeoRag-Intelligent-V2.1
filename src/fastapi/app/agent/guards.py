"""Plan §4b — structured guard error codes + classifier.

This module is the FOUNDATION for the citation-guard arm of plan §4b. It
defines the 16-code :class:`GuardErrorCode` enum exactly as specified in
the plan, plus a pure classifier :func:`classify_guards` that maps the
existing post-assembly validation outputs into typed codes.

What this module does NOT do (deferred to a follow-up):

  - Dispatch repair strategies per code
  - Trigger targeted re-retrieval
  - Track repair attempts or enforce max-attempts limits

The classifier is a thin translation layer over what the hallucination
sub-system already produces (``layer_*`` validators, the orchestrator's
post-assembly pass, the demoter). Once :func:`classify_guards` is wired
into ``persist_node`` (already integrated for plan §0e trace logging),
``silver.query_traces.guard_failure_codes`` starts populating with
typed codes instead of raw warning strings — which is the data we need
to decide which repair strategies are worth implementing first.

Plan reference: `docs/architecture/user_facing_error_catalog.md` lists
every code's user-facing message + UI surface. The renderer side (i18n
JSON + React components) is Job 3; the canonical code list lives here.

Pure module: no I/O, no DB calls, no LLM calls. Side-effect-free.
Testable in isolation.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


__all__ = [
    "GuardErrorCode",
    "RepairAttempt",
    "classify_guards",
    "detect_death_loop",
]


# ---------------------------------------------------------------------------
# Enum — verbatim from plan §4b
# ---------------------------------------------------------------------------


class GuardErrorCode(str, Enum):
    """Plan §4b — 16 structured codes for post-retrieval guard failures.

    Distinct from :class:`app.agent.errors.ErrorCode`, which handles
    infrastructure-level errors (timeouts, DB outages, rate limits).
    Guard codes are quality-of-evidence signals — the answer path
    succeeded but something about the retrieved evidence or the
    generated answer doesn't pass our standards.
    """

    # ── Retrieval-failure codes (9) ─────────────────────────────────────
    NO_EVIDENCE_FOUND = "NO_EVIDENCE_FOUND"
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    AMBIGUOUS_HOLE_ID = "AMBIGUOUS_HOLE_ID"
    AMBIGUOUS_FORMATION_NAME = "AMBIGUOUS_FORMATION_NAME"
    AMBIGUOUS_PROPERTY_NAME = "AMBIGUOUS_PROPERTY_NAME"
    OVER_FILTERED_QUERY = "OVER_FILTERED_QUERY"
    SPATIAL_QUERY_EMPTY = "SPATIAL_QUERY_EMPTY"
    SPATIAL_CRS_MISMATCH = "SPATIAL_CRS_MISMATCH"
    GRAPH_PATH_NOT_FOUND = "GRAPH_PATH_NOT_FOUND"

    # ── Evidence-quality codes (6) ──────────────────────────────────────
    NUMERIC_GROUNDING_FAILED = "NUMERIC_GROUNDING_FAILED"
    CITATION_INCOMPLETE = "CITATION_INCOMPLETE"
    CONFLICTING_SOURCES = "CONFLICTING_SOURCES"
    MISSING_DEPTH_INTERVAL = "MISSING_DEPTH_INTERVAL"
    MISSING_ASSAY_UNITS = "MISSING_ASSAY_UNITS"
    SOURCE_SCOPE_VIOLATION = "SOURCE_SCOPE_VIOLATION"

    # ── Query-failure codes (1) ─────────────────────────────────────────
    UNSUPPORTED_QUERY_TYPE = "UNSUPPORTED_QUERY_TYPE"

    # ── Egress / policy codes (1) ───────────────────────────────────────
    # Z.1 / Appendix C §5 — external-LLM egress gate. Raised when the
    # active backend wants to call out to a third-party LLM provider
    # (currently Anthropic) but the current workspace's profile does not
    # have ``allow_external_llm`` set to true. The user-facing render is
    # a hard refusal — no call to Anthropic is made and no workspace
    # data leaves the trust boundary. Operators flip the workspace
    # setting ``profile.allow_external_llm = true`` to permit egress.
    EGRESS_BLOCKED = "EGRESS_BLOCKED"


# ---------------------------------------------------------------------------
# Warning-string → GuardErrorCode pattern table
# ---------------------------------------------------------------------------
#
# Order matters: longest-prefix / most-specific patterns first. The
# classifier short-circuits on the first match per warning. Strings come
# from app.agent.hallucination.orchestrator_validators (the layer_*
# validators that produce state.validation_warnings).


_WARNING_PATTERNS: tuple[tuple[str, GuardErrorCode], ...] = (
    # Layer 3 — numeric grounding
    ("ungrounded number", GuardErrorCode.NUMERIC_GROUNDING_FAILED),
    ("different unit family", GuardErrorCode.MISSING_ASSAY_UNITS),
    ("layer 3 tuple", GuardErrorCode.NUMERIC_GROUNDING_FAILED),
    ("layer 3:", GuardErrorCode.NUMERIC_GROUNDING_FAILED),
    # Layer 4 — entity grounding (drill-hole, commodity, formation)
    ("drill-hole id", GuardErrorCode.ENTITY_NOT_FOUND),
    ("drillhole id", GuardErrorCode.ENTITY_NOT_FOUND),
    ("commodity", GuardErrorCode.ENTITY_NOT_FOUND),
    ("formation/entity", GuardErrorCode.ENTITY_NOT_FOUND),
    ("entity name", GuardErrorCode.ENTITY_NOT_FOUND),
    ("layer 4:", GuardErrorCode.ENTITY_NOT_FOUND),
    # Layer 5 — provenance / source scope
    ("source scope", GuardErrorCode.SOURCE_SCOPE_VIOLATION),
    ("outside your workspace", GuardErrorCode.SOURCE_SCOPE_VIOLATION),
    ("layer 5:", GuardErrorCode.SOURCE_SCOPE_VIOLATION),
    # Layer 6 — geological constraints (treated as NUMERIC_GROUNDING_FAILED
    # since the constraint violations are numeric-range failures)
    ("violates constraint", GuardErrorCode.NUMERIC_GROUNDING_FAILED),
    ("layer 6:", GuardErrorCode.NUMERIC_GROUNDING_FAILED),
    # Spatial path
    ("crs mismatch", GuardErrorCode.SPATIAL_CRS_MISMATCH),
    ("crs not specified", GuardErrorCode.SPATIAL_CRS_MISMATCH),
    ("no spatial matches", GuardErrorCode.SPATIAL_QUERY_EMPTY),
    # Graph path
    ("no path between", GuardErrorCode.GRAPH_PATH_NOT_FOUND),
    ("graph traversal empty", GuardErrorCode.GRAPH_PATH_NOT_FOUND),
    # Hole-ID ambiguity (distinct from "not found" — multiple matches)
    ("multiple drillholes match", GuardErrorCode.AMBIGUOUS_HOLE_ID),
    ("hole id matches multiple", GuardErrorCode.AMBIGUOUS_HOLE_ID),
    # Property / formation ambiguity (multiple-match variants)
    ("multiple projects", GuardErrorCode.AMBIGUOUS_PROPERTY_NAME),
    ("multiple formations", GuardErrorCode.AMBIGUOUS_FORMATION_NAME),
    # Filter relaxation hints
    ("over-filtered", GuardErrorCode.OVER_FILTERED_QUERY),
    ("filters relaxed", GuardErrorCode.OVER_FILTERED_QUERY),
    # Depth / unit hints
    ("missing depth", GuardErrorCode.MISSING_DEPTH_INTERVAL),
    ("depth interval missing", GuardErrorCode.MISSING_DEPTH_INTERVAL),
    ("units unclear", GuardErrorCode.MISSING_ASSAY_UNITS),
    ("missing units", GuardErrorCode.MISSING_ASSAY_UNITS),
)


def _classify_warning(warning: str) -> GuardErrorCode | None:
    """Map one warning string to a code. Returns None if no rule matches."""
    if not warning:
        return None
    needle = warning.lower()
    for pattern, code in _WARNING_PATTERNS:
        if pattern in needle:
            return code
    return None


# ---------------------------------------------------------------------------
# Public classifier
# ---------------------------------------------------------------------------


def classify_guards(
    *,
    validation_warnings: list[str] | None = None,
    demotion_reasons: list[str] | None = None,
    tool_results: list[Any] | None = None,
    response_citations: list[Any] | None = None,
    citation_lifecycle_state: str | None = None,
    conflicting_evidence_present: bool = False,
) -> list[GuardErrorCode]:
    """Classify the agentic-retrieval state into typed guard codes.

    The function is pure: it inspects only the values passed in (no DB,
    no LLM, no state mutation), and returns a deduplicated list of
    :class:`GuardErrorCode` values in stable order.

    Args:
        validation_warnings: Strings emitted by the hallucination
            layer validators (see
            :mod:`app.agent.hallucination.orchestrator_validators`).
        demotion_reasons: Strings emitted by the confidence demoter
            when answer confidence is lowered.
        tool_results: The agentic graph's ``state.tool_results`` —
            list of ``(tool_name, payload)`` tuples. Used to detect
            the no-evidence case (every tool returned empty).
        response_citations: ``response.citations`` from the final
            assembled answer. Empty list → CITATION_INCOMPLETE.
        citation_lifecycle_state: The Module 6 citation state
            ('draft' / 'generated' / 'validated' / 'committed' /
            'rejected'). 'rejected' → CITATION_INCOMPLETE.
        conflicting_evidence_present: Whether the assemble step
            detected conflicting numeric values across sources
            (Phase 1.3 conflict_detection_enabled).

    Returns:
        A deduplicated list of GuardErrorCodes in stable iteration
        order. Empty list when no guard fired.
    """
    seen: dict[GuardErrorCode, None] = {}  # dict preserves insertion order

    # 1. Warning-string → code mapping (the bulk of the classification)
    for w in validation_warnings or []:
        code = _classify_warning(w)
        if code is not None and code not in seen:
            seen[code] = None

    for d in demotion_reasons or []:
        code = _classify_warning(d)
        if code is not None and code not in seen:
            seen[code] = None

    # 2. No-evidence-found detection. The agentic graph's tool_results
    # is a list of (name, payload) tuples. A query that retrieved
    # absolutely nothing from any source falls into NO_EVIDENCE_FOUND.
    if tool_results is not None:
        any_payload = False
        for entry in tool_results:
            # Tuple-shaped (name, payload). Payload could be list, dict, str, etc.
            payload = entry[1] if isinstance(entry, tuple) and len(entry) >= 2 else entry
            if payload:  # truthy check handles list, dict, str, etc.
                any_payload = True
                break
        if not any_payload and GuardErrorCode.NO_EVIDENCE_FOUND not in seen:
            seen[GuardErrorCode.NO_EVIDENCE_FOUND] = None

    # 3. Citation-completeness. Only fires when the caller EXPLICITLY
    # passes a signal — either an empty `response_citations` list, OR
    # `citation_lifecycle_state == "rejected"`. `None` means the caller
    # didn't pass that field; no inference.
    cite_signal_provided = (
        response_citations is not None or citation_lifecycle_state is not None
    )
    if cite_signal_provided:
        no_citations = response_citations is not None and len(response_citations) == 0
        rejected = citation_lifecycle_state == "rejected"
        if (no_citations or rejected) and GuardErrorCode.CITATION_INCOMPLETE not in seen:
            seen[GuardErrorCode.CITATION_INCOMPLETE] = None

    # 4. Conflicting-sources signal from the assemble step.
    if conflicting_evidence_present and GuardErrorCode.CONFLICTING_SOURCES not in seen:
        seen[GuardErrorCode.CONFLICTING_SOURCES] = None

    return list(seen.keys())


# ---------------------------------------------------------------------------
# Plan §4c — death-loop detection
# ---------------------------------------------------------------------------


from dataclasses import dataclass, field as _field  # noqa: E402 — keep
                                                   #  detector co-located


@dataclass(frozen=True)
class RepairAttempt:
    """One iteration of the §4b repair loop, retained on
    :class:`AgenticRetrievalState` for the death-loop detector to inspect.

    The repair loop is not yet wired (§4b dispatcher pending). The
    detector is shipped now as a pure function so that as soon as the
    dispatcher lands, death loops are caught without additional work.

    Attributes:
        tool_name: The tool that was re-invoked on this repair attempt
            (e.g. ``"query_assay_data"``).
        filters: The filter dict passed to the tool. Compared with
            ``==`` on the previous attempt — identical filters AND
            identical empty/low result triggers the death-loop check.
        result_count: How many records / chunks the tool returned. The
            plan §4c condition: a death loop fires when two consecutive
            attempts each returned ≤ 1 result with the same tool +
            filters.
        attempted_at_monotonic: ``time.monotonic()`` at the start of
            this attempt — only used for logging / Sentry tagging.
    """

    tool_name: str
    filters: dict[str, str | int | float | None] = _field(default_factory=dict)
    result_count: int = 0
    attempted_at_monotonic: float = 0.0


def detect_death_loop(repair_attempts: list[RepairAttempt]) -> bool:
    """Plan §4c — return True when the repair loop is stuck on the same
    fruitless tool+filter combination.

    Trigger condition (verbatim plan §4c):

        Same tool name + same parameters + same filters + same empty
        or low-value result, repeated more than once.

    With < 2 attempts, no comparison can be made and the function
    returns False. The caller (the future repair-loop dispatcher in
    plan §4b) checks this after each attempt and short-circuits the
    loop into a refusal-with-diagnostic when True.

    Pure function: no I/O, no logging. Caller is responsible for the
    Sentry event + the user-facing refusal text (lang/en/guard_errors
    `DEATH_LOOP` key, rendered by GuardErrorRenderer).
    """
    if len(repair_attempts) < 2:
        return False
    last = repair_attempts[-1]
    prev = repair_attempts[-2]
    return (
        last.tool_name == prev.tool_name
        and last.filters == prev.filters
        and last.result_count <= 1
        and prev.result_count <= 1
    )
