"""Layer 6 — Geological Constraint Rules.

Architecture reference: Section 04i, Layer 6.

Purpose
-------
Apply SME-defined hard limits to numerical values that appear in the response
text alongside geological keywords.  These constraints encode physical and
practical impossibilities:

  - A drill hole cannot be deeper than 5 km in a typical exploration context.
  - Gold assay grades above 1000 ppm are implausibly high for a disseminated
    deposit (they can legitimately occur in high-grade veins, but the system
    prompt instructs the LLM to use tool-verified values — so if a tool
    returned an extreme grade, verify_numerical_claim has already confirmed it
    is real).  We therefore cap at 1000 ppm as a sanity gate.
  - U3O8 grades above 50% are thermodynamically impossible — pure U3O8 is 84.8%
    uranium by mass; 50% U3O8 by weight would be an extraordinarily rich
    sample.
  - Core recovery cannot exceed 100%.
  - Azimuth must be in [0, 360].
  - Dip must be in [-90, 0] (negative convention for downhole drilling).
  - RQD must be in [0, 100].
  - Confidence must be in [0.0, 1.0] (checked directly on output.confidence).

The constraint check is context-sensitive: a depth value is only flagged when
the surrounding text includes a keyword like "depth", "metres", "m", "meters".
This prevents false positives when a number happens to be large for an
unrelated reason (e.g. a UTM easting of 512345).

Design decisions
----------------
- Constraints are defined as a module-level dict so they can be patched in
  tests and, in future, loaded from an SME configuration file.
- Each constraint is a (keyword_patterns, min_value, max_value) tuple.
  A value is checked only when at least one keyword appears within 60
  characters of the number in the response text.
- The validator is disabled when settings.GEOLOGICAL_CONSTRAINTS_ENABLED is False.

Pydantic AI output_validator
-----------------------------
LIVE, but not the way this docstring used to say. It claimed
registration in geo_agent.py, which does not exist. What actually
reaches this module is orchestrator_validators.py importing
``_find_violations`` from it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_ai import ModelRetry, RunContext

from app.agent.deps import AgentDeps
from app.agent.hallucination.citation_markers import CITATION_MARKER_RE
from app.agent.hole_id_patterns import (
    HOLE_CONTEXT_RE as _HOLE_CONTEXT_RE,
)
from app.agent.hole_id_patterns import (
    HOLE_ID_RE as _HOLE_ID_RE,
)
from app.agent.hole_id_patterns import (
    NUMERIC_HOLE_ID_RE as _NUMERIC_HOLE_ID_RE,
)
from app.config import settings
from app.models.rag import GeoRAGResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constraint definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeologicalConstraint:
    """A single geological plausibility constraint.

    Attributes
    ----------
    name:
        Human-readable name used in log messages and ModelRetry text.
    keywords:
        One or more regex patterns.  A numeric value is only checked against
        this constraint if at least one keyword appears within ``context_chars``
        characters of the number in the response text.
    min_value:
        Inclusive lower bound.  None = no lower bound check.
    max_value:
        Inclusive upper bound.  None = no upper bound check.
    unit_hint:
        Appended to violation messages to make the correction actionable.
    context_chars:
        How far BACK from the number to look for a governing keyword. English
        puts the noun before the value — "azimuth of 045", "a dip of -60",
        "a total depth of 510 m" — so this window is what actually attaches a
        number to a quantity. Keep it tight: a wide window pulls in the
        *neighbouring* clause's keyword and flags a correct value against the
        wrong constraint.
    lookahead_chars:
        How far FORWARD to look, for the trailing-unit form ("1.85 g/t Au").
        Much shorter than the backward window, because a keyword that far
        after a number usually belongs to the next clause, not this one.
    absolute_value:
        Compare |value| against the bounds. Set for dip, where reports use
        both the negative downhole convention (-60) and the positive one (60)
        and neither is an error.
    unit_scales:
        Multipliers that bring a value written in some OTHER unit into the
        unit the bounds are expressed in, keyed by the unit token as it
        appears after the number.

        Without this the bounds silently assume every value is already in the
        constraint's unit. ``grade_gold_max_ppm`` caps gold at 1000 ppm and
        lists ``ppm`` and ``g/t`` as keywords — 1 g/t is 1 ppm, so those two
        agree. ppb does not: a perfectly ordinary "1020 ppb Au" (1.02 g/t)
        was compared against 1000 **ppm** and reported as a geological
        impossibility. ppb is the standard unit for low-grade gold, so the
        guard false-positived on a whole class of normal answers — and the
        error runs the other way too, since 2 % Au (20,000 ppm) passed.

        Unit arithmetic only. The BOUNDS remain an SME decision and are not
        touched by this: 1000 ppm is still 1000 ppm.
    """

    name: str
    keywords: Sequence[str]
    min_value: float | None
    max_value: float | None
    unit_hint: str = ""
    context_chars: int = 40
    negative_keywords: Sequence[str] = ()  # if any match, skip this constraint
    unit_scales: Mapping[str, float] = field(default_factory=dict)
    lookahead_chars: int = 15
    absolute_value: bool = False


# Phase 12 Step 3 (R-P11-l6-config) — SME-editable constraint table.
# The limits used to be inline Python literals; they now load from a
# JSON sibling file so the geologist can adjust without a code deploy.
# Module-load is cheap (one disk read + dataclass construction) — we
# don't bother caching across imports because Python's import machinery
# already does that for us.

_CONSTRAINTS_JSON_PATH = Path(__file__).parent / "layer6_constraints.json"


def _load_constraints_from_json() -> list[GeologicalConstraint]:
    """Read the SME-editable constraint table off disk.

    Reserved keys (prefixed with ``_``) at the document root are
    metadata for humans / tooling and are ignored. Each entry in
    ``constraints`` maps 1:1 onto :class:`GeologicalConstraint`.
    """
    with open(_CONSTRAINTS_JSON_PATH, encoding="utf-8") as fh:
        payload = json.load(fh)
    out: list[GeologicalConstraint] = []
    for entry in payload.get("constraints", []):
        out.append(
            GeologicalConstraint(
                name=entry["name"],
                keywords=tuple(entry.get("keywords", ())),
                min_value=entry.get("min_value"),
                max_value=entry.get("max_value"),
                unit_hint=entry.get("unit_hint", ""),
                context_chars=int(entry.get("context_chars", 40)),
                negative_keywords=tuple(entry.get("negative_keywords", ())),
                lookahead_chars=int(entry.get("lookahead_chars", 15)),
                absolute_value=bool(entry.get("absolute_value", False)),
                unit_scales={
                    str(k): float(v)
                    for k, v in (entry.get("unit_scales") or {}).items()
                },
            )
        )
    return out


GEOLOGICAL_CONSTRAINTS: list[GeologicalConstraint] = _load_constraints_from_json()

# Compiled number-plus-context extractor.
# Captures: optional sign, digits, optional decimal.
_NUMBER_WITH_CONTEXT_RE = re.compile(r"(-?\d+(?:\.\d+)?)")

# Citation marker pattern — numbers inside citation markers are never content
# numbers and must not be checked against geological constraints. The Layer 3
# numerical verifier uses the same exclusion logic (shared pattern).
_CITATION_MARKER_RE = CITATION_MARKER_RE


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _governing_constraint(
    text: str,
    number_start: int,
    number_end: int,
) -> tuple[GeologicalConstraint, str] | None:
    """Return the constraint whose keyword actually governs this number.

    The number belongs to exactly ONE quantity, and in English that quantity
    is named by the nearest keyword — almost always immediately before the
    value ("a dip of -60"), occasionally immediately after it as a unit
    ("1.85 g/t Au"). So we pick the single nearest keyword occurrence and
    check that constraint alone.

    This replaces a symmetric ±200-character window that fired *every*
    constraint whose keyword appeared anywhere nearby. On the ordinary
    sentence "…at an azimuth of 045 degrees and a dip of -60 degrees,
    reaching a total depth of 510 m", that window put `azimuth`, `dip` and
    `depth` in scope of all three numbers, so 045 was tested against the dip
    range (max 0) and 510 against the azimuth range (max 360) — two
    violations on a factually perfect answer. Every Layer 6 warning is
    graded `high`, which sets should_retry, floors confidence to 0.2 and
    prepends a fabrication banner, so a clean drill-geometry answer arrived
    looking like a suspected hallucination.

    Returns (constraint, matched_keyword_text) or None when no keyword is
    near enough to attach.
    """
    best: tuple[int, GeologicalConstraint, str] | None = None

    for constraint in GEOLOGICAL_CONSTRAINTS:
        back_start = max(0, number_start - constraint.context_chars)
        behind = text[back_start:number_start]
        ahead = text[number_end:number_end + constraint.lookahead_chars]

        for kw in constraint.keywords:
            # Backward: distance from the END of the keyword to the number.
            for m in re.finditer(kw, behind, re.IGNORECASE):
                distance = len(behind) - m.end()
                if best is None or distance < best[0]:
                    best = (distance, constraint, m.group(0))
            # Forward: distance from the number to the START of the keyword.
            for m in re.finditer(kw, ahead, re.IGNORECASE):
                distance = m.start()
                if best is None or distance < best[0]:
                    best = (distance, constraint, m.group(0))

    if best is None:
        return None
    return best[1], best[2]


def _unit_scale(
    text: str, number_end: int, constraint: GeologicalConstraint,
) -> float:
    """Multiplier bringing the value at ``number_end`` into the bounds' unit.

    Reads the unit token written immediately after the number — the form
    English actually uses for measurements ("1020 ppb", "2.31 g/t Au") — and
    looks it up in ``constraint.unit_scales``. Returns 1.0 when no declared
    unit follows, which is both the common case and the safe one: an
    unrecognised unit means "compare as written", exactly the old behaviour.

    Deliberately does NOT search backwards. A preceding unit is nearly always
    the previous clause's ("... 1.02 g/t Au, and 40 % of the core"), and
    attaching it here would scale a number by a unit that is not its own.
    """
    if not constraint.unit_scales:
        return 1.0

    tail = text[number_end:number_end + 12].lstrip()
    for token, scale in constraint.unit_scales.items():
        if tail.lower().startswith(token.lower()):
            # Guard against "ppm" matching the "pp" of a longer token: the
            # next character must not continue the word.
            rest = tail[len(token):]
            if not rest or not (rest[0].isalnum() or rest[0] == "/"):
                return scale
    return 1.0


def _check_value_against_constraint(
    value: float,
    text: str,
    number_start: int,
    number_end: int,
    constraint: GeologicalConstraint,
) -> bool:
    """Return True if this value violates this constraint.

    The caller has already established that ``constraint`` is the one
    governing this number (see :func:`_governing_constraint`); this decides
    only whether the value is out of bounds.
    """
    # Negative keywords still use a wide window: "easting" or "UTM" anywhere
    # near a number is reason to leave it alone regardless of what governs it.
    if constraint.negative_keywords:
        ctx_start = max(0, number_start - 200)
        ctx_end = min(len(text), number_end + 200)
        context = text[ctx_start:ctx_end]
        if any(
            re.search(nk, context, re.IGNORECASE)
            for nk in constraint.negative_keywords
        ):
            return False

    compared = abs(value) if constraint.absolute_value else value
    compared *= _unit_scale(text, number_end, constraint)

    if constraint.min_value is not None and compared < constraint.min_value:
        return True
    return bool(constraint.max_value is not None and compared > constraint.max_value)


@dataclass
class ConstraintViolation:
    """A single detected constraint violation."""

    value: float
    constraint: GeologicalConstraint
    context_snippet: str  # short excerpt around the number for the retry message


def _masked_ranges(text: str) -> list[tuple[int, int]]:
    """Character ranges holding numbers that are not geological values.

    Two kinds:

    * Citation markers — [DATA-3] is a reference index, not a measurement.
    * Drill-hole identifiers — PLS-22-08 is a name. Its digits were being
      read as two signed numbers, -22 and -8, and tested against whatever
      constraint happened to be nearby: mentioning a hole by name in a
      sentence about its depth produced two depth violations before the
      sentence said anything about depth at all.

    Numeric-only hole IDs (36-1085) are masked only when the text actually
    talks about holes, matching the gate viz_builder puts on the same
    pattern — otherwise a depth interval like "20-30 m" would be masked.
    """
    ranges: list[tuple[int, int]] = [
        (m.start(), m.end()) for m in _CITATION_MARKER_RE.finditer(text)
    ]
    ranges.extend((m.start(), m.end()) for m in _HOLE_ID_RE.finditer(text))
    if _HOLE_CONTEXT_RE.search(text):
        ranges.extend((m.start(), m.end()) for m in _NUMERIC_HOLE_ID_RE.finditer(text))
    return ranges


def _find_violations(text: str) -> list[ConstraintViolation]:
    """Scan response text and return all geological constraint violations.

    Numbers inside citation markers and drill-hole identifiers are excluded
    (see :func:`_masked_ranges`), and each remaining number is tested against
    the one constraint that governs it (see :func:`_governing_constraint`).
    """
    masked = _masked_ranges(text)

    violations: list[ConstraintViolation] = []

    for m in _NUMBER_WITH_CONTEXT_RE.finditer(text):
        try:
            value = float(m.group(1))
        except ValueError:
            continue

        start, end = m.start(), m.end()

        # Skip any number that overlaps a masked range at all — a hole ID
        # yields several numbers and every one of them must go.
        if any(ms < end and start < me for ms, me in masked):
            continue

        governing = _governing_constraint(text, start, end)
        if governing is None:
            continue
        constraint, _keyword = governing

        if _check_value_against_constraint(value, text, start, end, constraint):
            snippet_start = max(0, start - 30)
            snippet_end = min(len(text), end + 30)
            snippet = text[snippet_start:snippet_end].replace("\n", " ").strip()
            violations.append(
                ConstraintViolation(
                    value=value,
                    constraint=constraint,
                    context_snippet=snippet,
                )
            )

    return violations


# ---------------------------------------------------------------------------
# Output validator
# ---------------------------------------------------------------------------


async def check_geological_constraints(
    ctx: RunContext[AgentDeps],
    output: GeoRAGResponse,
) -> GeoRAGResponse:
    """Output validator: check all numerical claims against geological reality.

    This is hallucination prevention Layer 6.

    LIVE via orchestrator_validators.py, which imports
    ``_find_violations`` from this module. (Not via geo_agent.py —
    that file does not exist.)

    Checks performed:
    1. All numbers with geological keyword context against the constraint table.
    2. output.confidence is in [0.0, 1.0] (enforced by Pydantic, but we log
       if it is suspiciously high for a partially verified response).

    Raises:
        ModelRetry: if any numerical value violates a geological constraint.

    Returns:
        The unchanged output if all values pass.
    """
    if not settings.GEOLOGICAL_CONSTRAINTS_ENABLED:
        logger.debug("layer6_constraints: disabled via settings — skipping")
        return output

    # Confidence range is enforced by Pydantic (ge=0.0, le=1.0) — no need to
    # re-check here.  But log a warning if confidence is suspiciously high
    # when there are no citations beyond the minimum.
    if output.confidence > 0.95 and len(output.citations) == 1:
        logger.warning(
            "layer6_constraints: confidence=%.2f with only 1 citation — "
            "this is likely over-confident; consider lowering confidence",
            output.confidence,
        )

    violations = _find_violations(output.text)

    if not violations:
        logger.debug("layer6_constraints: no geological constraint violations detected")
        return output

    # Build a detailed violation report for the retry message.
    violation_lines: list[str] = []
    for v in violations:
        bound_desc = []
        if v.constraint.min_value is not None:
            bound_desc.append(f"min {v.constraint.min_value}")
        if v.constraint.max_value is not None:
            bound_desc.append(f"max {v.constraint.max_value}")
        bounds = ", ".join(bound_desc)
        violation_lines.append(
            f"- Value {v.value} violates constraint '{v.constraint.name}' "
            f"({bounds} {v.constraint.unit_hint}). "
            f"Context: '...{v.context_snippet}...'"
        )
        logger.warning(
            "layer6_constraints: constraint '%s' violated — value=%.4f "
            "bounds=[%s, %s] context='%.80s'",
            v.constraint.name,
            v.value,
            v.constraint.min_value,
            v.constraint.max_value,
            v.context_snippet,
        )

    raise ModelRetry(
        "Geological constraint violation(s) detected (hallucination prevention "
        "Layer 6):\n\n"
        + "\n".join(violation_lines)
        + "\n\nVerify these values against the tool call results. If the tool "
        "returned a value that violates these physical constraints, do not "
        "include it in the response — report it as anomalous data and note "
        "that it requires SME review. If the value was generated by you rather "
        "than from a tool, remove it and use only tool-sourced data."
    )
