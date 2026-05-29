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
Registered in geo_agent.py with ``@geo_agent.output_validator``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import ModelRetry, RunContext

from app.agent.deps import AgentDeps
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
        How many characters around the number to scan for keywords (default 80).
    """

    name: str
    keywords: Sequence[str]
    min_value: float | None
    max_value: float | None
    unit_hint: str = ""
    context_chars: int = 200
    negative_keywords: Sequence[str] = ()  # if any match, skip this constraint


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
                context_chars=int(entry.get("context_chars", 200)),
                negative_keywords=tuple(entry.get("negative_keywords", ())),
            )
        )
    return out


GEOLOGICAL_CONSTRAINTS: list[GeologicalConstraint] = _load_constraints_from_json()

# Compiled number-plus-context extractor.
# Captures: optional sign, digits, optional decimal.
_NUMBER_WITH_CONTEXT_RE = re.compile(r"(-?\d+(?:\.\d+)?)")

# Citation marker pattern — numbers inside [DATA-X], [NI43-X], [PUB-X] are
# never content numbers and must not be checked against geological constraints.
# The Layer 3 numerical verifier uses the same exclusion logic.
_CITATION_MARKER_RE = re.compile(r"\[(?:DATA|NI43|PUB)-\d+\]")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_value_against_constraint(
    value: float,
    text: str,
    number_start: int,
    number_end: int,
    constraint: GeologicalConstraint,
) -> bool:
    """Return True if this value violates this constraint.

    Violation requires:
      1. A keyword for this constraint appears within context_chars of the number.
      2. The value is outside [min_value, max_value].
    """
    # Extract the surrounding context window.
    ctx_start = max(0, number_start - constraint.context_chars)
    ctx_end = min(len(text), number_end + constraint.context_chars)
    context = text[ctx_start:ctx_end].lower()

    # Check whether any keyword fires in the context window.
    keyword_matched = any(
        re.search(kw, context, re.IGNORECASE) for kw in constraint.keywords
    )
    if not keyword_matched:
        return False

    # Check negative keywords — if any match, this is a false positive.
    if constraint.negative_keywords:
        negative_matched = any(
            re.search(nk, context, re.IGNORECASE)
            for nk in constraint.negative_keywords
        )
        if negative_matched:
            return False

    # Check bounds.
    if constraint.min_value is not None and value < constraint.min_value:
        return True
    return bool(constraint.max_value is not None and value > constraint.max_value)


@dataclass
class ConstraintViolation:
    """A single detected constraint violation."""

    value: float
    constraint: GeologicalConstraint
    context_snippet: str  # short excerpt around the number for the retry message


def _find_violations(text: str) -> list[ConstraintViolation]:
    """Scan response text and return all geological constraint violations.

    Numbers that appear inside citation markers ([DATA-X], [NI43-X], [PUB-X])
    are excluded — they are reference indices, not geological values, and must
    not be tested against physical plausibility constraints.
    """
    # Collect all character ranges covered by citation markers so we can skip
    # numbers that fall inside them.
    citation_ranges: list[tuple[int, int]] = [
        (m.start(), m.end()) for m in _CITATION_MARKER_RE.finditer(text)
    ]

    violations: list[ConstraintViolation] = []

    for m in _NUMBER_WITH_CONTEXT_RE.finditer(text):
        try:
            value = float(m.group(1))
        except ValueError:
            continue

        start, end = m.start(), m.end()

        # Skip any number whose match position overlaps a citation marker.
        if any(cs <= start < ce for cs, ce in citation_ranges):
            continue

        for constraint in GEOLOGICAL_CONSTRAINTS:
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
                break  # one violation per number is enough

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

    Registration: @geo_agent.output_validator in geo_agent.py.

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
