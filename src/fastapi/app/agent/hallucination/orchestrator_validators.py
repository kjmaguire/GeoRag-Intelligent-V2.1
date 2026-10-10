"""Orchestrator-compatible hallucination validators.

This module IS the live post-assembly validation path. It holds Layers 3
(numerical grounding), 4 (entity resolution), 6 (geological constraints)
and the completeness guard, all working against the deterministic
orchestrator's tool_results list rather than Pydantic AI's ctx.messages.
Since 2026-09-24 it also holds the ADVISORY half of Layer 1 (retrieval
quality) — see ``verify_retrieval_quality`` below; the HARD half (zero-
evidence refusal) runs earlier, in assemble_node, before the LLM is called.

History (2026-08-21): the Pydantic-AI-shaped originals — layer3_numerical.py,
layer4_entity.py, layer1_retrieval.py and layer_completeness.py — were
deleted. They had accumulated no production callers: the orchestrator never
adopted the output_validator decorator pattern they were built for, so they
sat alongside this module looking like controls that were in force while
only this one ever executed. The completeness guard and the guard-tolerance
model were the only logic unique to them and were ported here; the rest was
a second, unreachable implementation of what verify_numbers and
verify_entities already do. Layers 2 and 6 remain in their own modules and
are wired into agentic_retrieval/nodes.py. Layer 5 (chunk provenance) is
split since 2026-09-24: enrichment stays in layer5_provenance.py; the gate
half also lives there (``gate_citation_provenance``) and is called directly
from validate_node, not through this module, because it must run BEFORE
Layer 2's marker-stripping pass rather than alongside Layers 3/4/6.

Usage in orchestrator:
    from app.agent.hallucination.orchestrator_validators import run_post_assembly_validation
    response, warnings, should_retry = await run_post_assembly_validation(
        response, tool_results, deps
    )
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import logging
import re
import time
from typing import Any

from pydantic import BaseModel

from app.agent.deps import AgentDeps
from app.agent.hallucination.citation_markers import (
    ALL_MARKER_RE,
    CITATION_MARKER_RE,
    CITATION_PREFIXES,
)
from app.agent.hallucination.claim_sentences import split_units
from app.agent.hole_id_patterns import (
    DESIGNATION_RE,
    HOLE_CONTEXT_RE,
    HOLE_ID_RE,
    NUMERIC_HOLE_ID_RE,
    canonical_hole_id,
    find_lettered_hole_ids,
    find_numeric_hole_ids,
    hole_id_key,
    iter_compact_hole_id_matches,
)
from app.config import settings
from app.models.rag import GeoRAGResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module 6 Chunk 3.5 — Formation name cache (per-process, TTL-based)
#
# The entity guard's Neo4j Formation-name lookup is the single most expensive
# guard operation (~200-500 ms on a warm Neo4j, dominating guard runtime when
# sequential).  Since Formation nodes change only on ingestion runs
# (much less frequent than 5 minutes), a per-process TTL cache is safe and
# keeps the entity guard cheap on the hot path.
#
# Cache key: project_id string → (frozenset[str], fetched_at_epoch_s)
# TTL: 300 s (5 minutes).  First call per window pays the Neo4j round-trip;
# subsequent calls within the window do a dict lookup (~microseconds).
# Cache misses are logged at INFO so hit rate is observable in logs.
# ---------------------------------------------------------------------------
_FORMATION_CACHE: dict[str, tuple[frozenset[str], float]] = {}
_FORMATION_CACHE_TTL_S: float = 300.0  # 5 minutes


async def _get_known_formations(
    neo4j_driver: Any,
    project_id: str,
    timeout_s: float = 3.0,
) -> frozenset[str]:
    """Fetch Formation node names from Neo4j, with a 5-minute TTL cache.

    Returns an empty frozenset when:
      - neo4j_driver is None
      - Neo4j has no Formation nodes (fail-open)
      - The query times out or errors (fail-open)

    Cache miss is logged at INFO so hit rate is observable.
    """
    import asyncio

    now = time.monotonic()
    cached = _FORMATION_CACHE.get(project_id)
    if cached is not None:
        formations, fetched_at = cached
        if now - fetched_at < _FORMATION_CACHE_TTL_S:
            return formations
        # Cache expired — fall through to refresh

    # B1 (2026-07-28): Neo4j was REMOVED from the stack — deps.neo4j_driver
    # is always None in production, so this branch fires on every call and
    # the Layer-4 formation check below is PERMANENTLY fail-open (no
    # formation warnings can ever be emitted). Kept rather than deleted so
    # the check springs back to life if a graph store returns; flagged
    # explicitly per the RAG-quality audit 2026-08-14 (finding 7) so nobody
    # mistakes it for live coverage.
    if neo4j_driver is None:
        return frozenset()

    logger.info(
        "orchestrator_validators._get_known_formations: cache miss for project=%s "
        "(TTL=%.0fs) — querying Neo4j",
        project_id,
        _FORMATION_CACHE_TTL_S,
    )

    cypher = (
        "MATCH (f:Formation {project_id: $project_id}) "
        "RETURN f.name AS name"
    )
    try:
        async def _run() -> frozenset[str]:
            async with neo4j_driver.session() as session:
                result = await session.run(cypher, project_id=project_id)
                rows = await result.data()
            return frozenset(
                r["name"].lower() for r in rows if r.get("name")
            )

        formations = await asyncio.wait_for(_run(), timeout=timeout_s)
        _FORMATION_CACHE[project_id] = (formations, now)
        logger.info(
            "orchestrator_validators._get_known_formations: cached %d formation(s) "
            "for project=%s",
            len(formations),
            project_id,
        )
        return formations
    except Exception:
        logger.debug(
            "orchestrator_validators._get_known_formations: fetch failed "
            "(fail-open — graph may not be populated)",
            exc_info=True,
        )
        return frozenset()


# ---------------------------------------------------------------------------
# Layer 3 — Numerical Claim Verification (orchestrator version)
# ---------------------------------------------------------------------------

#: A number as prose writes it.
#:
#: * Thousands separators are part of the number: "12,400 m" is 12400, not
#:   12 and 400 (audit 2026-09-29, RAG-2). The old ``[-+]?\d+\.?\d*`` split
#:   every comma-formatted tonnage, ounce count and depth in an NI 43-101
#:   into small pieces, and a fabricated "12,345,000 tonnes" was invisible
#:   to Layer 3 and Layer 6 alike.
#: * A sign is a sign only when nothing word-like precedes it: the "-" in
#:   "120-126 m" or "36-1085" is a range dash or part of a name, and used to
#:   produce a spurious -126 / -1085.
#: * Digits glued to letters ("U3O8", "NI43", "BH12", "eU3O8") are part of a
#:   name or a formula, not a quantity.
_NUMBER_PATTERN = (
    r"(?:(?<![\w.,])[-+])?(?<![\w.])(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)?"
)
_NUMBER_RE = re.compile(_NUMBER_PATTERN)


def _parse_number(token: str) -> float:
    return float(token.replace(",", ""))


def _written_tolerance(token: str) -> tuple[float, float]:
    """How far a value may sit from the one it was rounded from.

    Returns ``(exact, rounded)``: ``exact`` is half a unit in the last
    written place; ``rounded`` additionally reads trailing zeros as
    rounding. The wider ``rounded`` window is only ever compared against
    the evidence's own values, never against their unit conversions —
    "1,300 m" is how an answer rounds a stated 1,340 m, but "5,000" is not
    how it states 154 x 31.1035.

    Half a unit in the last place the number was written to: "7.4" stands
    for anything in [7.35, 7.45], "7.44" for [7.435, 7.445]. Trailing zeros
    on an integer are read as rounding ("12,400" for 12,350..12,450, "1,300"
    for the 1,340 m a report states) but never wider than 5 % of the value,
    so "1,000" may stand for 987 but not for 500.

    Replaces a flat ``abs(num - g) < 0.1``, which grounded EVERY value below
    about 0.1 against the ``g / 10000`` and ``g / 31.1`` entries the unit
    expansion adds for every grounded number — including a typical roll-front
    grade such as 0.087 % eU3O8 — and was far too tight for rounded large
    values at the other end (RAG-1).
    """
    digits = token.lstrip("+-").replace(",", "")
    if "." in digits:
        exact = 0.5 * 10 ** -len(digits.split(".", 1)[1])
        return exact, exact
    trailing_zeros = len(digits) - len(digits.rstrip("0"))
    if trailing_zeros and len(digits) > trailing_zeros:
        value = abs(float(digits))
        return 0.5, max(0.5, min(0.5 * 10**trailing_zeros, 0.05 * value))
    return 0.5, 0.5

#: Drill-hole and sample identifiers, removed before any number extraction.
#:
#: `_NUMBER_RE` has no idea what a hole ID is, so "PLS-22-08" yielded the two
#: numbers **-22.0 and -8.0** — the hyphens read as minus signs. Both sides of
#: Layer 3 did this: the response text, and the grounded-number collection, which
#: regexes digit runs straight out of the serialised tool results where every
#: collar row carries a `hole_id`.
#:
#: That was not merely noise. It disabled the guard. The derivation tolerance
#: accepts a number that sits at the same order of magnitude as some grounded
#: value, and a corpus of hole IDs injects a dense spread of small magnitudes
#: (-8, -9, -22, -41 …) into the grounded set. Gold grades in g/t, widths in
#: metres and most other geological quantities live in exactly that range, so
#: a FABRICATED grade was blessed by the ID of a hole that had nothing to do
#: with it. Measured 2026-08-21: with three collars named PLS-22-08/09/10 in
#: evidence, an invented "7.44 g/t Au" produced zero warnings; strip the IDs
#: and it is flagged.
#:
#: Only the lettered form is stripped. Bare numeric hole IDs (36-1085,
#: 36-1042 — the Cameco Shirley Basin convention) are deliberately NOT
#: matched here: the same shape is a year range, a page range and an interval
#: written with a hyphen, and stripping those would silently remove real
#: numbers from grounding, which is the more dangerous error. See
#: test_layer_golden_outputs.py for that gap pinned as a test.
_IDENTIFIER_TOKEN_RE = re.compile(r"\b[A-Za-z]{1,8}[-_]\d{1,6}(?:[-_]\d{1,6})*\b")
# Marker regex the AGENTIC path's verify_numbers uses (shared pattern —
# see citation_markers.py for the colon/dash + PGEO rationale).
_CITATION_MARKER_RE = CITATION_MARKER_RE
_SMALL_NUMBERS = {0.0, 1.0, 2.0, 3.0}  # too common to verify

# ────────────────────────────────────────────────────────────────────────
# Eval 01 P3 follow-up — L3 numeric-tuple atomicity (Phase A: shadow).
#
# The current L3 guard treats numbers as bare floats. That misses the
# unit-pair fabrication mode where the model writes "37 oz/t" when the
# evidence carries "37 g/t" — both 37s are in the grounded set so the
# guard passes, but the unit is wrong by a factor of ~31.
#
# Phase A introduces a SHADOW extractor: pairs each number with its
# trailing unit token and logs (value, unit) tuples to telemetry. The
# guard's pass/fail decision is unchanged. Phase B (next sprint) will
# promote the tuple check to a real warning once we've validated that
# the extractor doesn't produce false positives on real traffic.
#
# Unit tokens are matched greedily on the 6-char window after the number,
# limited to the geological-evidence unit set we care about.
# ────────────────────────────────────────────────────────────────────────

# The terminator used to be a bare \\b, which made the percent arm of
# this pattern unmatchable. "%" is a non-word character, so \\b after it
# demands a WORD character immediately following — which never happens in
# real prose. Every one of these returned nothing:
#
#     "grade of 5% U3O8"   ->  []      (space follows)
#     "grade of 5%."       ->  []      (period follows)
#     "0.45 wt% Cu"        ->  []
#
# while "37 oz/t Au" and "12.5 m of core" matched fine, because those
# units end in word characters. So on a uranium platform, where grade is
# quoted in percent, the unit-pair guard was blind to exactly the values
# it exists for — and ppm-vs-% confusion is the 10,000x error class.
#
# A negative lookahead says what was actually meant: the unit token must
# not run straight into more alphanumerics (so "5 mm" does not read as
# 5 metres), and anything else — space, period, comma, end of string —
# ends the token.
_NUMBER_WITH_UNIT_RE = re.compile(
    r"(" + _NUMBER_PATTERN + r")\s*"
    r"(g/t|oz/t|ppm|ppb|wt%|%|m|ft|km|kt|Mt|mt|tonnes?|lbs?|kg)"
    r"(?![A-Za-z0-9/])",
    re.IGNORECASE,
)

# Unit families — values within a family are convertible to each other
# via the existing _expand_grounded_with_conversions() table. A response
# tuple whose value matches a grounded value BUT whose unit lives in a
# different family is a unit-pair fabrication (the value happens to
# coincide; the unit is wrong).
# Families are grouped by SCALE, not by dimension.
#
# This table used to put g/t, oz/t, ppm, ppb, wt% and % in a single
# "mass_conc" family, and _detect_unit_mismatches only warns when a value's
# unit family differs from every grounded occurrence of that value. So the
# entire class of grade-unit errors was invisible by construction — including
# the g/t-versus-percent confusion the config comment cites as the reason the
# guard was promoted from shadow to warn. It could not fire on it.
#
# What matters is the size of the mistake if the units are swapped:
#
#   g/t and ppm are the SAME unit (1 g/t = 1 ppm), so they stay together.
#   ppb is 1,000x off from ppm.
#   percent is 10,000x off from ppm — "1.85%" for "1.85 g/t" turns a
#     marginal intercept into a world-class one.
#   oz/t is ~34.29x off from g/t.
#
# Same reasoning for the other dimensions: metres and feet are a 3.3x error
# and kilometres a 1,000x one, so they are not interchangeable either.
_UNIT_FAMILIES: dict[str, str] = {
    # grade, parts-per-million scale (g/t IS ppm)
    "g/t": "conc_ppm",
    "g/tonne": "conc_ppm",
    "gpt": "conc_ppm",
    "ppm": "conc_ppm",
    # grade, parts-per-billion scale
    "ppb": "conc_ppb",
    # grade, percent scale
    "%": "conc_pct",
    "wt%": "conc_pct",
    "pct": "conc_pct",
    # grade, troy ounces per short ton
    "oz/t": "conc_ozt",
    "oz/ton": "conc_ozt",
    "opt": "conc_ozt",
    # length
    "m": "length_m",
    "metre": "length_m",
    "metres": "length_m",
    "meters": "length_m",
    "ft": "length_ft",
    "feet": "length_ft",
    "km": "length_km",
    # mass
    "kg": "mass_kg",
    "lb": "mass_lb",
    "lbs": "mass_lb",
    "tonne": "mass_t",
    "tonnes": "mass_t",
    "t": "mass_t",
    "kt": "mass_kt",
    "mt": "mass_mt",
}


def _extract_number_unit_tuples(text: str) -> list[tuple[float, str]]:
    """Pairs numbers with their immediately-following unit token (lower-cased)."""
    clean = _strip_non_claims(text)
    out: list[tuple[float, str]] = []
    for match in _NUMBER_WITH_UNIT_RE.finditer(clean):
        try:
            val = _parse_number(match.group(1))
            unit = match.group(2).lower()
            if val not in _SMALL_NUMBERS:
                out.append((val, unit))
        except ValueError:
            continue
    return out


def _collect_grounded_tuples(
    tool_results: list[tuple[str, Any]],
) -> list[tuple[float, str]]:
    """Same shape as _extract_number_unit_tuples, over the CONTENT strings of
    the tool results (identifier / score / metadata fields skipped, the same
    walk Layer 3's grounded set uses — see `_content_strings`)."""
    out: list[tuple[float, str]] = []
    for _tool_name, result in tool_results:
        try:
            for text in _content_strings(result):
                out.extend(_extract_number_unit_tuples(text))
        except Exception:
            continue
    return out


def _content_strings(obj: Any, key: str = "") -> list[str]:
    """String values under content keys only (see `_NON_CONTENT_KEYS`)."""
    lowered = key.lower()
    if lowered and (
        lowered in _NON_CONTENT_KEYS
        or _NON_CONTENT_KEY_RE.search(lowered)
        or _IDENTIFIER_KEY_RE.search(lowered)
    ):
        return []
    out: list[str] = []
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            out.extend(_content_strings(getattr(obj, f.name), f.name))
    elif isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            out.extend(_content_strings(getattr(obj, name), name))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_content_strings(v, str(k)))
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            out.extend(_content_strings(v, key))
    elif isinstance(obj, str):
        out.append(obj)
    return out


# ────────────────────────────────────────────────────────────────────────
# Quantities: a number, the unit it is written in, and what it may be
# grounded against (2026-10-10 audit, findings 1-3).
#
# Layer 3 used to ground a number three ways and none of them knew a unit:
#
#   * four "conversion factors" (10 000, 31.1035, 3.28084, 1 000) multiplied
#     into EVERY grounded value, so "850 ppm" was grounded by 0.85 % (x1000,
#     the ppb factor) and "18,500 ppb" by 1.85 g/t (x10 000, the % factor);
#   * no factor at all for the commonest restatements: 48,200,000 tonnes as
#     "48.2 Mt", "2.5 million ounces", oz per short ton;
#   * a derivation window that accepted a number within 0.5x-2x of ANY
#     numeric field of ANY structured result (an azimuth, a count, a depth, a
#     grade), so an invented "7.44 g/t over 12.6 m" or "87 drill holes" was
#     blessed by an unrelated number of about the right size.
#
# Now a number is read together with the unit written next to it
# (`_scan_quantities`), the evidence is read the same way, and a number is
# grounded by one of three things only: the same figure (literal, give or
# take its rounding), the same quantity in ANOTHER unit of the same dimension
# (the real family-to-family factor), or something the answer works out from
# the evidence and SAYS it works out -- a mean / median / percentile of a
# structured series (inside the series' own [min, max]), a bound inside that
# range, a recount of the rows, arithmetic on its own sentence's numbers. See
# "Figures the answer works out from the evidence" below for what each needs.
#
# The independent review of 2026-10-10 then found the other side of this:
# a guard that cannot tell "the 75th percentile" from a claim about the data
# floors correct answers (any Layer 3 finding forces a retry), so what is NOT
# a claim is stripped first (`_strip_labels`), and what a sentence works out
# is recomputed rather than guessed at.
# ────────────────────────────────────────────────────────────────────────

#: family -> (dimension, scale to the dimension's base unit). Two families
#: convert into each other only inside one dimension, and the factor is the
#: ratio of their scales -- so ppm -> % is 1e-4 and never 1e-3.
#:
#: Base units: ppm (1 g/t IS 1 ppm), metre, metric tonne, troy ounce, degree.
#: "oz/t" carries both readings of the token: troy ounces per SHORT ton
#: (34.2857 g/t, the North American convention) and per metric tonne
#: (31.1035 g/t).
_FAMILY_SCALES: dict[str, tuple[str, tuple[float, ...]]] = {
    "conc_ppm": ("grade", (1.0,)),
    "conc_ppb": ("grade", (1e-3,)),
    "conc_pct": ("grade", (1e4,)),
    "conc_ozt": ("grade", (34.2857, 31.1035)),
    "length_m": ("length", (1.0,)),
    "length_km": ("length", (1_000.0,)),
    "length_ft": ("length", (0.3048,)),
    "mass_kg": ("mass", (1e-3,)),
    "mass_lb": ("mass", (4.5359237e-4,)),
    "mass_mlb": ("mass", (453.59237,)),  # 1,000,000 lb
    "mass_st": ("mass", (0.90718474,)),  # short ton
    "mass_t": ("mass", (1.0,)),
    "mass_kt": ("mass", (1e3,)),
    "mass_mt": ("mass", (1e6,)),  # megatonne ("Mt")
    "oz": ("troy_oz", (1.0,)),
    "oz_k": ("troy_oz", (1e3,)),  # "koz"
    "oz_m": ("troy_oz", (1e6,)),  # "Moz"
    "angle_deg": ("angle", (1.0,)),
}

#: Every unit token the claim / evidence reader recognises, lower-cased. The
#: unit-pair guard's table plus the spellings prose uses for the same units.
_CLAIM_UNIT_FAMILY: dict[str, str] = {
    **_UNIT_FAMILIES,
    "oz/tons": "conc_ozt",
    "oz/tonne": "conc_ozt",
    "oz/tonnes": "conc_ozt",
    "percent": "conc_pct",
    "per cent": "conc_pct",
    "parts per million": "conc_ppm",
    "parts per billion": "conc_ppb",
    "gram per tonne": "conc_ppm",
    "grams per tonne": "conc_ppm",
    "ounce per ton": "conc_ozt",
    "ounces per ton": "conc_ozt",
    "ounces per short ton": "conc_ozt",
    "ounces per tonne": "conc_ozt",
    "meter": "length_m",
    "kilometre": "length_km",
    "kilometres": "length_km",
    "kilometer": "length_km",
    "kilometers": "length_km",
    "pound": "mass_lb",
    "pounds": "mass_lb",
    "mlb": "mass_mlb",
    "mlbs": "mass_mlb",
    "ton": "mass_st",
    "tons": "mass_st",
    "short ton": "mass_st",
    "short tons": "mass_st",
    "metric ton": "mass_t",
    "metric tons": "mass_t",
    "metric tonne": "mass_t",
    "metric tonnes": "mass_t",
    "oz": "oz",
    "ounce": "oz",
    "ounces": "oz",
    "koz": "oz_k",
    "moz": "oz_m",
    "°": "angle_deg",
    "deg": "angle_deg",
    "degree": "angle_deg",
    "degrees": "angle_deg",
}

#: "48.2 million tonnes", "71 million pounds", "48.2 M tonnes". A bare "M" is
#: only a magnitude when a unit word follows it ("48.2 Mt" is its own unit,
#: and "5 M" alone is left as metres).
_MAGNITUDES: dict[str, float] = {
    "thousand": 1e3, "million": 1e6, "billion": 1e9, "M": 1e6,
}
# A hyphen may stand in for the space: "a 320.1-m depth" is 320.1 metres, as
# "a 5-km strike" is 5 km. (A unit that runs into more letters is still not a
# unit, so "12-month" and "5-minute" are untouched.)
_QTY_AFTER_RE = re.compile(
    r"(?:\s*|-)(?:(?P<mag>million|billion|thousand|(?-i:M))\s+)?"
    r"(?P<unit>"
    + "|".join(re.escape(u) for u in sorted(_CLAIM_UNIT_FAMILY, key=len, reverse=True))
    + r")(?![A-Za-z0-9/²³])",
    re.IGNORECASE,
)
_QTY_MAGNITUDE_ONLY_RE = re.compile(r"\s*(?P<mag>million|billion)\b", re.IGNORECASE)

#: What may sit between two numbers that share one unit: "145.2 to 148.0 m",
#: "120-126 m", "656 and 984 ft". The first number takes the second's unit.
_RANGE_JOIN_RE = re.compile(
    r"\s*(?:-|–|—|&|to|and|or|through|thru|,\s*and|,\s*or)\s*", re.IGNORECASE
)


@dataclasses.dataclass(frozen=True)
class _Quantity:
    """A number as the text states it: ``48.2`` in "48.2 million tonnes".

    ``value`` is the number as written and ``exact`` / ``rounded`` its
    rounding windows (`_written_tolerance`) in the same terms; ``magnitude``
    is the "million" that follows it (1.0 when there is none) and ``family``
    the unit family of the unit written after it (None when it has none).
    """

    value: float
    exact: float
    rounded: float
    magnitude: float = 1.0
    family: str | None = None
    start: int = 0
    end: int = 0
    #: The sentence the number is written in, the text in front of it and
    #: behind it within that sentence, and the other numbers of the sentence.
    #: Filled in by `_extract_claims`; what a number means (a mean, a
    #: threshold, the difference of two depths) is read from its sentence.
    context: str = ""
    before: str = ""
    after: str = ""
    peers: tuple[_Quantity, ...] = ()
    #: The ``(from, to)`` pairs of the sentence that are written as a range
    #: ("from 145.2 to 152.5 m", "120-126 m"): a range has a width, its two
    #: ends are depths.
    ranges: tuple[tuple[_Quantity, _Quantity], ...] = ()

    @property
    def scaled(self) -> float:
        """The figure with its "million" applied: 71e6 for "71 million"."""
        return self.value * self.magnitude


def _scan_quantities(text: str) -> list[_Quantity]:
    """Every number in ``text`` with the unit it is written in.

    The text is expected to be stripped of identifiers already (see
    `_strip_non_claims`). A number with no unit of its own takes the unit of
    the number a range word joins it to: both ends of "145.2 to 148.0 m" are
    metres.
    """
    found: list[_Quantity] = []
    for match in _NUMBER_RE.finditer(text):
        token = match.group()
        try:
            value = _parse_number(token)
        except ValueError:
            continue
        exact, rounded = _written_tolerance(token)
        magnitude, family = 1.0, None
        after = _QTY_AFTER_RE.match(text, match.end())
        if after is not None:
            family = _CLAIM_UNIT_FAMILY.get(after.group("unit").lower())
            word = after.group("mag")
            if word:
                magnitude = _MAGNITUDES.get(word) or _MAGNITUDES.get(word.lower(), 1.0)
        else:
            only = _QTY_MAGNITUDE_ONLY_RE.match(text, match.end())
            if only is not None:
                magnitude = _MAGNITUDES[only.group("mag").lower()]
        found.append(_Quantity(value, exact, rounded, magnitude, family, match.start(), match.end()))

    for i in range(len(found) - 2, -1, -1):
        here, following = found[i], found[i + 1]
        if (
            here.family is None
            and here.magnitude == 1.0
            and following.family is not None
            and _RANGE_JOIN_RE.fullmatch(text[here.end : following.start])
        ):
            found[i] = dataclasses.replace(
                here, family=following.family, magnitude=following.magnitude
            )
    return found


def _conversion_factors(source: str, target: str) -> tuple[float, ...]:
    """Multipliers ``m`` with ``value_in_target = value_in_source * m``.

    Empty when either family is unknown or the two measure different things
    (a percentage is never a length). A family converts into itself with
    exactly 1: "oz/t" carries two readings of the token (short ton, metric
    tonne), but one figure is written in ONE of them -- applying both to a
    restatement in the same family made "1.10 oz/t" a restatement of
    "1.00 oz/t" (their ratio is 34.2857 / 31.1035).
    """
    src, dst = _FAMILY_SCALES.get(source), _FAMILY_SCALES.get(target)
    if src is None or dst is None or src[0] != dst[0]:
        return ()
    if source == target:
        return (1.0,)
    return tuple(a / b for a in src[1] for b in dst[1])


def _detect_unit_mismatches(
    response_tuples: list[tuple[float, str]],
    grounded_tuples: list[tuple[float, str]],
) -> list[str]:
    """Return one warning per response tuple whose unit family disagrees
    with every grounded tuple sharing the same numeric value.

    Logic: for each response (v, unit_r), look at every grounded
    (g, unit_g) where g is within 0.1 of v. If at least one grounded
    candidate shares the unit family with unit_r, the tuple is
    consistent. If none does, the model produced a value that exists in
    the evidence under a DIFFERENT unit family — the canonical
    unit-pair fabrication case.
    """
    warnings: list[str] = []
    for v, unit_r in response_tuples:
        family_r = _UNIT_FAMILIES.get(unit_r)
        if family_r is None:
            # Unknown unit — skip; we only flag mismatches across known families.
            continue
        # Relative, not the old flat 0.1: that matched every grounded value
        # below 0.1 to every other one, whatever its unit.
        candidates = [
            (g, unit_g) for (g, unit_g) in grounded_tuples
            if abs(g - v) <= 0.005 * max(abs(g), abs(v)) + 1e-9
        ]
        if not candidates:
            # No same-value grounded tuple at all → falls under the
            # ungrounded-number check; not our job to re-flag.
            continue
        if any(_UNIT_FAMILIES.get(u_g) == family_r for (_, u_g) in candidates):
            continue
        # Every grounded occurrence of this value carries a unit from a
        # different scale. Either the model swapped the unit, or it read the
        # number off an unrelated quantity — both are worth saying.
        known = [u for (_, u) in candidates if _UNIT_FAMILIES.get(u) is not None]
        if not known:
            # The evidence only carries this value under units we do not
            # recognise, so there is nothing to compare scales against.
            continue
        observed = sorted(set(known))
        warnings.append(
            f"Layer 3 tuple: value {v} reported as '{unit_r}' "
            f"but evidence carries it as {observed} (different unit family)"
        )
    return warnings


#: Page / figure / table / section / item references in the answer. The
#: number in "Section 14.2" or "page 112" is a pointer, not a claim, and the
#: evidence side no longer grounds section numbers and pages (they are
#: metadata, see _NON_CONTENT_KEY_RE).
_REFERENCE_RE = re.compile(
    r"\b(?:sections?|tables?|figs?\.?|figures?|pages?|pp?\.|appendix|appendices|"
    r"chapters?|items?|plates?)\s+\d+(?:[.\-]\d+)*[A-Za-z]?\b",
    re.IGNORECASE,
)

#: Keys whose values are identifiers, scores or provenance metadata rather
#: than evidence. Their digits used to be regexed straight out of the
#: serialised tool result: a chunk UUID "44a67709-..." contributed 44 and
#: 67709, document_type "NI43" contributed 43, and relevance_score,
#: ocr_confidence, section_number and page filled in the rest. With the
#: derivation window accepting anything within 2x of any grounded value,
#: that covered almost every magnitude, so an invented "7.44 g/t Au over
#: 12.6 m" in a document-grounded answer produced no warning (RAG-1).
_NON_CONTENT_KEYS: frozenset[str] = frozenset((
    "id", "chunk_id", "report_id", "source_document_id", "collar_id",
    "workspace_id", "project_id", "relevance_score", "ocr_confidence",
    "ocr_method", "ocr_status", "section_number", "data_source",
    "document_type", "rerank_degraded", "modality", "image_object_key",
    "pg_id", "source_id", "source_feature_id", "staleness_seconds",
    "source_row_id", "source_row_ids", "log_id", "entity_id", "slug",
    "license_url", "source_url", "license_summary", "canonical_type",
))
_NON_CONTENT_KEY_RE = re.compile(
    r"(?:_id|_ids|_uuid|_url|_at|_key|_sha256|_hash)$|^page|score|confidence|"
    r"base64|b64|png|image"
)

#: Keys whose string values are hole / sample names. Their digits are not
#: evidence either, but the names themselves are removed from the answer
#: before its numbers are read, so a numeric hole ID the evidence names
#: ("36-1085") is not mistaken for two numerical claims.
_IDENTIFIER_KEY_RE = re.compile(r"hole|sample_id|sample_number|sample_name")

#: Tool results that are document prose. Their numbers ground only what they
#: literally say (after rounding and unit conversion). The "derived
#: statistic" allowance is for structured rows -- a mean of collar depths lies
#: inside the collar depths -- and applying it to every number in 5,000
#: characters of report text is what let a fabricated grade pass whenever
#: the chunk happened to contain any value of the same magnitude.
_DOCUMENT_TOOL_NAMES: frozenset[str] = frozenset((
    "search_documents", "search_documents_adversarial", "search_public_geoscience",
))


@dataclasses.dataclass
class _Evidence:
    #: Every content number, unit-blind: what the answer may state verbatim.
    literal: set[float] = dataclasses.field(default_factory=set)
    #: ``(value, unit family)`` for each number the evidence states WITH a
    #: unit -- written ("37.3 g/t") or implied by a structured field's name
    #: (``total_depth`` is metres). The source of every unit conversion.
    quantities: list[tuple[float, str]] = dataclasses.field(default_factory=list)
    #: ``(unit family, series) -> (lowest, highest)`` over the values of one
    #: structured series (``total_depth`` across the collars returned). A
    #: mean, median or percentile of a series lies inside its own range, so
    #: the range is all a derived statistic is ever allowed to claim.
    bounds: dict[tuple[str, str], tuple[float, float]] = dataclasses.field(
        default_factory=dict
    )
    #: ``(family, series) -> [(value, hole id)]`` -- the rows the range above
    #: was taken over, so that "5 holes intersected more than 2 g/t" can be
    #: counted rather than guessed at. The hole is "" for a row that names none.
    rows: dict[tuple[str, str], list[tuple[float, str]]] = dataclasses.field(
        default_factory=dict
    )
    #: ``(figure with its "million" applied, unit family or None)`` for each
    #: number the evidence writes with a magnitude word ("48.2 million tonnes").
    #: Kept apart from `literal` on purpose: a scaled figure carries the unit
    #: it was written in, so it may ground a claim that states NO unit, or one
    #: of the same dimension -- never "48,200,000 ounces" (2026-10-10 review).
    scaled: list[tuple[float, str | None]] = dataclasses.field(default_factory=list)
    #: The tool's own aggregates of one result ({"min": .., "max": .., "mean":
    #: .., "median": .., "std": ..}), for the multiples an answer works out
    #: from them ("5 times the median", "2.4 standard deviations").
    stat_sets: list[dict[str, float]] = dataclasses.field(default_factory=list)
    identifiers: set[str] = dataclasses.field(default_factory=set)
    _indexes: dict[str, Any] = dataclasses.field(default_factory=dict, repr=False, compare=False)

    def literal_index(self) -> list[float]:
        """The unit-blind literals as a sorted list of magnitudes, for a
        bisect rather than a scan per claim. Built on first use, after the
        walk has finished adding to the evidence (a sentinel of 1e9 or more
        is not a measurement and is left out)."""
        index = self._indexes.get("literal")
        if index is None:
            index = self._indexes["literal"] = sorted(
                abs(g) for g in self.literal if abs(g) < 1e9
            )
        return index

    def quantity_index(self) -> dict[str, list[float]]:
        """``unit family -> sorted magnitudes`` of the quantities the evidence
        states with a unit (same sentinel cut-off as `literal_index`)."""
        index = self._indexes.get("quantities")
        if index is None:
            grouped: dict[str, list[float]] = {}
            for value, family in self.quantities:
                if abs(value) < 1e9:
                    grouped.setdefault(family, []).append(abs(value))
            for values in grouped.values():
                values.sort()
            index = self._indexes["quantities"] = grouped
        return index

    def add_quantity(
        self, value: float, family: str, series: str | None = None, hole: str = ""
    ) -> None:
        """Record ``value`` as a quantity of ``family``; a ``series`` name
        also widens that series' range (leave it None for values a mean of
        is never stated: coordinates, interval boundaries, a spread, and the
        tool's own full-set aggregates -- `_field_unit`).

        A value no measurement of the family can take -- a -999 or 1e10
        sentinel for a missing depth, a 9999 / 99999 / 999999 "no data" code,
        a zero-metre hole -- is kept as a quantity but never widens a range:
        one sentinel would otherwise make every number "derivable".
        """
        self.quantities.append((value, family))
        if series is None or not _plausible_measurement(value, family, series):
            return
        low, high = self.bounds.get((family, series), (value, value))
        self.bounds[(family, series)] = (min(low, value), max(high, value))
        self.rows.setdefault((family, series), []).append((value, hole))


#: Positive "no data" codes of assay and collar databases. (The negative ones
#: -- -999, -9999, a detection limit stored as its negative -- fall outside
#: every range below.)
_NULL_CODES: frozenset[float] = frozenset((9999.0, 99999.0, 999999.0))
#: No drill hole is deeper than this: the deepest ever drilled is ~12.3 km.
_MAX_LENGTH_M = 15_000.0
#: Terrain a collar can stand on, in metres.
_ELEVATION_RANGE_M = (-500.0, 9_000.0)


def _plausible_measurement(value: float, family: str, series: str) -> bool:
    """Whether ``value`` can be a measurement of ``series``, as opposed to a
    sentinel or a default. Only plausible values widen a series' range."""
    if value in _NULL_CODES:
        return False
    if family == "angle_deg":
        return abs(value) <= 360.0
    dimension, scales = _FAMILY_SCALES.get(family, ("", (1.0,)))
    if dimension == "length":
        metres = value * scales[0]
        if series == "elevation":
            # 0.0 is the usual stand-in for a collar with no surveyed height.
            return metres != 0.0 and _ELEVATION_RANGE_M[0] <= metres <= _ELEVATION_RANGE_M[1]
        return 0.0 < metres <= _MAX_LENGTH_M
    if family == "conc_pct":
        return 0.0 <= value <= 100.0
    return 0.0 <= value < 1e6


#: Structured numeric fields whose NAME fixes their unit -- the tools return
#: bare floats, so the name is the only unit evidence there is. ``True``
#: marks a measurement whose mean / median / percentile an answer may state;
#: ``False`` a position or coordinate (an easting, the depth a sample starts
#: at), which can be converted but is never averaged. A field not listed here
#: has no unit: it grounds a literal restatement and nothing more.
_FIELD_UNITS: dict[str, tuple[str, bool]] = {
    **dict.fromkeys(
        ("total_depth", "max_depth", "total_metres", "total_meters", "thickness",
         "width", "true_width", "length", "elevation"),
        ("length_m", True),
    ),
    **dict.fromkeys(
        ("depth", "depth_m", "depth_from", "depth_to", "from_depth", "to_depth",
         "from_m", "to_m", "easting", "northing", "radius_m", "buffer_m",
         "distance_m"),
        ("length_m", False),
    ),
    **dict.fromkeys(
        ("azimuth", "dip", "plunge", "trend", "strike", "dip_deg", "strike_deg",
         "plunge_deg", "trend_deg", "dip_direction_deg"),
        ("angle_deg", True),
    ),
    **dict.fromkeys(("rqd", "recovery"), ("conc_pct", True)),
}
_FIELD_SUFFIX_FAMILY: tuple[tuple[str, str], ...] = (
    ("_ppm", "conc_ppm"), ("_gpt", "conc_ppm"), ("_g_t", "conc_ppm"),
    ("_ppb", "conc_ppb"), ("_pct", "conc_pct"), ("_percent", "conc_pct"),
    ("_ft", "length_ft"), ("_km", "length_km"),
)
#: An assay value's unit is not in its field name (``value``) but in the
#: ``element`` key of the row or result that carries it ("Au_ppb",
#: "U3O8_pct_e") or in a sibling ``unit``.
_GRADE_FIELD_RE = re.compile(r"^(?:value|grade)$")
#: The tool's own aggregates over the FULL set the rows were drawn from
#: (``min_value``, ``mean_value`` ...). They ground a restatement of themselves
#: and convert like any grade, but they are not rows: the rows are LIMIT-capped
#: and the aggregates are not, so putting both in one series let one
#: full-set mean (or max) stretch the window the capped rows span (2026-10-10
#: review, "mean grade 0.07 g/t" over samples of 0.5-45 ppb).
_GRADE_AGGREGATE_RE = re.compile(r"^(?:(?:min|max|mean|median|avg)_(?:value|grade)|std_value)$")
_ELEMENT_UNIT_RE = re.compile(r"_(ppm|ppb|pct|percent|gpt|g_t|opt)(?:_e)?$", re.IGNORECASE)
_ELEMENT_UNIT_FAMILY: dict[str, str] = {
    "ppm": "conc_ppm", "gpt": "conc_ppm", "g_t": "conc_ppm", "ppb": "conc_ppb",
    "pct": "conc_pct", "percent": "conc_pct", "opt": "conc_ozt",
}
#: Pairs whose difference is an interval's width: the "over 2.8 m" an answer
#: states for a sample taken from 145.2 to 148.0 m is a derived figure of two
#: grounded depths, and used to pass only because it was near some other row.
_INTERVAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("from_depth", "to_depth"), ("depth_from", "depth_to"), ("from_m", "to_m"),
)


def _field_of(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _grade_family(element: Any, unit: Any) -> str | None:
    """Unit family of the grade values a row / result carries, if it says."""
    if isinstance(unit, str):
        family = _CLAIM_UNIT_FAMILY.get(unit.strip().lower())
        if family is not None:
            return family
    if isinstance(element, str):
        match = _ELEMENT_UNIT_RE.search(element)
        if match is not None:
            return _ELEMENT_UNIT_FAMILY[match.group(1).lower()]
    return None


def _field_unit(
    name: str, grade_family: str | None, element: str
) -> tuple[str, str | None] | None:
    """``(unit family, series)`` of a structured numeric field, or None.

    ``series`` is None for a value that is not averaged. The grade values of
    one element's ROWS form one series (``value`` of each sample); the
    result's own ``min_value`` / ``max_value`` / ``mean_value`` /
    ``median_value`` / ``std_value`` are literal-only (`_GRADE_AGGREGATE_RE`).
    """
    if grade_family is not None:
        if _GRADE_FIELD_RE.match(name):
            return grade_family, f"grade:{element}"
        if _GRADE_AGGREGATE_RE.match(name):
            return grade_family, None
    known = _FIELD_UNITS.get(name)
    if known is not None:
        family, averaged = known
        return family, (name if averaged else None)
    for suffix, family in _FIELD_SUFFIX_FAMILY:
        if name.endswith(suffix):
            return family, name
    return None


def _is_document_result(tool_name: str, result: Any) -> bool:
    if tool_name in _DOCUMENT_TOOL_NAMES:
        return True
    if isinstance(result, dict):
        return "chunks" in result or "records" in result
    from app.agent.public_geoscience_tool import (  # noqa: PLC0415
        PublicGeoscienceSearchResult,
    )
    from app.agent.tools import DocumentSearchResult  # noqa: PLC0415

    return isinstance(result, (DocumentSearchResult, PublicGeoscienceSearchResult))


def _evidence_text(text: str) -> str:
    """Evidence prose with the identifiers whose digits are not content removed."""
    text = DESIGNATION_RE.sub(" ", text)
    return _IDENTIFIER_TOKEN_RE.sub(" ", HOLE_ID_RE.sub(" ", text))


def _numbers_in(text: str) -> list[float]:
    """Every number an evidence string states, as it is written.

    The figure a following "million" makes of it ("48.2 million tonnes" is
    also 48,200,000) is NOT among them: this list is unit-blind, and a scaled
    figure must keep the unit it was written with -- see `_scaled_in`.
    """
    return [quantity.value for quantity in _scan_quantities(_evidence_text(text))]


def _scaled_in(text: str) -> list[tuple[float, str | None]]:
    """``(figure with its magnitude word applied, unit family)`` for every
    number an evidence string writes with a "million" / "billion" / "thousand"."""
    return [
        (quantity.scaled, quantity.family)
        for quantity in _scan_quantities(_evidence_text(text))
        if quantity.magnitude != 1.0
    ]


def _quantities_in(text: str) -> list[tuple[float, str]]:
    """``(value, unit family)`` for every number an evidence string writes a
    unit after, with a following "million" applied."""
    return [
        (quantity.scaled, quantity.family)
        for quantity in _scan_quantities(_evidence_text(text))
        if quantity.family is not None
    ]


def _row_grade_context(
    obj: Any, grade_family: str | None, element: str
) -> tuple[str | None, str]:
    """The grade unit and element of ``obj``'s numeric fields: its own when it
    names an ``element`` / ``unit`` (even one with no recognisable unit --
    the parent's unit is not its child's), else the enclosing object's."""
    own_element, own_unit = _field_of(obj, "element"), _field_of(obj, "unit")
    if isinstance(own_element, str) or isinstance(own_unit, str):
        return (
            _grade_family(own_element, own_unit),
            own_element if isinstance(own_element, str) else element,
        )
    return grade_family, element


def _add_interval_width(obj: Any, ev: _Evidence) -> None:
    """Record ``to - from`` of a sampled / logged interval as a length.

    A quantity only -- it grounds a restatement of THAT width ("over 2.8 m"),
    and deliberately feeds no series range. Lithology intervals run to tens of
    metres, so a range of widths would put almost any length an answer states
    inside the "derivable" window, which is the hole this module closed.
    """
    for start_name, end_name in _INTERVAL_FIELDS:
        start, end = _field_of(obj, start_name), _field_of(obj, end_name)
        if _is_number(start) and _is_number(end) and end >= start:
            ev.add_quantity(float(end) - float(start), "length_m")
            return


def _row_hole(obj: Any) -> str:
    """The hole a row belongs to ("" when it names none)."""
    hole = _field_of(obj, "hole_id")
    return hole if isinstance(hole, str) else ""


_STAT_FIELDS: dict[str, str] = {
    "min_value": "min", "max_value": "max", "mean_value": "mean", "avg_value": "mean",
    "median_value": "median", "std_value": "std",
}


def _note_stat_set(obj: Any, ev: _Evidence) -> None:
    """Remember the aggregates a result reports about its own rows."""
    stats: dict[str, float] = {}
    for field, name in _STAT_FIELDS.items():
        value = _field_of(obj, field)
        if _is_number(value):
            stats[name] = float(value)
    if len(stats) >= 2:
        ev.stat_sets.append(stats)


def _walk_evidence(
    obj: Any,
    ev: _Evidence,
    *,
    structured: bool,
    key: str = "",
    sample_sizes: bool = True,
    grade_family: str | None = None,
    element: str = "",
    hole: str = "",
) -> None:
    """Collect content numbers from ``obj``, skipping non-content keys.

    ``sample_sizes=False`` stops the length of a list -- and a ``count``
    field -- from counting as a number the answer may state. It is set for a
    result that reports its own ``total_count`` (a LIMIT-capped sample): the
    sample size is not a fact about the project, and offering it as one is
    how "50 holes" got grounded on a 567-hole project (audit item 3).

    ``grade_family`` / ``element`` carry the unit of the grade values of the
    row being walked down to its numeric fields (see `_field_unit`); ``hole``
    the hole that row belongs to.
    """
    lowered = key.lower()
    if lowered and _IDENTIFIER_KEY_RE.search(lowered):
        for value in _collect_value_strings(obj):
            if value and value != "None":
                ev.identifiers.add(value)
        return
    if lowered and (lowered in _NON_CONTENT_KEYS or _NON_CONTENT_KEY_RE.search(lowered)):
        return
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        reports_total = getattr(obj, "total_count", None) is not None
        grade_family, element = _row_grade_context(obj, grade_family, element)
        hole = _row_hole(obj) or hole
        if structured:
            _add_interval_width(obj, ev)
            _note_stat_set(obj, ev)
        for f in dataclasses.fields(obj):
            if reports_total and f.name == "count":
                continue  # rows returned, not rows matched
            _walk_evidence(
                getattr(obj, f.name), ev, structured=structured, key=f.name,
                sample_sizes=sample_sizes and not reports_total,
                grade_family=grade_family, element=element, hole=hole,
            )
    elif isinstance(obj, BaseModel):
        grade_family, element = _row_grade_context(obj, grade_family, element)
        hole = _row_hole(obj) or hole
        if structured:
            _add_interval_width(obj, ev)
            _note_stat_set(obj, ev)
        for name in type(obj).model_fields:
            _walk_evidence(
                getattr(obj, name), ev, structured=structured, key=name,
                sample_sizes=sample_sizes,
                grade_family=grade_family, element=element, hole=hole,
            )
    elif isinstance(obj, dict):
        grade_family, element = _row_grade_context(obj, grade_family, element)
        hole = _row_hole(obj) or hole
        if structured:
            _add_interval_width(obj, ev)
            _note_stat_set(obj, ev)
        for k, v in obj.items():
            _walk_evidence(
                v, ev, structured=structured, key=str(k), sample_sizes=sample_sizes,
                grade_family=grade_family, element=element, hole=hole,
            )
    elif isinstance(obj, (list, tuple, set, frozenset)):
        if structured and sample_sizes:
            # A row count is a number the answer may state ("12 samples").
            ev.literal.add(float(len(obj)))
        for v in obj:
            _walk_evidence(
                v, ev, structured=structured, key=key, sample_sizes=sample_sizes,
                grade_family=grade_family, element=element, hole=hole,
            )
    elif isinstance(obj, bool) or obj is None:
        return
    elif isinstance(obj, (int, float)):
        number = float(obj)
        ev.literal.add(number)
        if structured:
            unit = _field_unit(lowered, grade_family, element)
            if unit is not None:
                ev.add_quantity(number, *unit, hole=hole)
    elif isinstance(obj, str):
        ev.literal.update(_numbers_in(obj))
        ev.quantities.extend(_quantities_in(obj))
        ev.scaled.extend(_scaled_in(obj))


def _collect_evidence(tool_results: list[tuple[str, Any]]) -> _Evidence:
    ev = _Evidence()
    for tool_name, result in tool_results:
        try:
            _walk_evidence(
                result, ev, structured=not _is_document_result(tool_name, result)
            )
        except Exception:
            logger.debug("Layer 3: evidence walk failed for %s", tool_name, exc_info=True)
            continue
    return ev


# ────────────────────────────────────────────────────────────────────────
# What in an answer is not a claim (2026-10-10 review, finding 2)
#
# Layer 3 reads every number of an answer as a statement about the data. Some
# are not: the "5" of "5. Holes range from ...", the "75" of "the 75th
# percentile", the "5" of "the top 5 intervals", the index column of a table,
# the "5" of "Zone 5". They give a place in the answer, a rank, the size of a
# selection or a label; none can be found in a tool result, and every one was
# reported as an ungrounded number on a correct answer -- which floors the
# confidence and prints the banner (any Layer 3 finding forces a retry).
# ────────────────────────────────────────────────────────────────────────

#: First-column headings that make a table's leading column an index.
_TABLE_INDEX_HEADERS: frozenset[str] = frozenset((
    "#", "no", "no.", "nr", "nr.", "n°", "index", "idx", "rank", "item", "row", "s/n", "sn",
    "seq", "sr", "sr.", "serial",
))
_TABLE_FIRST_CELL_RE = re.compile(r"^(\s*\|\s*)\d{1,4}(\s*\|)")


def _strip_table_index_cells(text: str) -> str:
    """Blank the leading cell of each row of a markdown table whose own first
    column is an index ("#", "No.", "Rank" ...): its 4, 5, 6 count the rows."""
    if "|" not in text:
        return text
    lines = text.split("\n")
    in_table = index_column = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            in_table = index_column = False
            continue
        first = stripped[1:].split("|", 1)[0].strip().lower()
        if not in_table:
            in_table, index_column = True, first in _TABLE_INDEX_HEADERS
        elif index_column and first.isdigit():
            lines[i] = _TABLE_FIRST_CELL_RE.sub(r"\1 \2", line, count=1)
    return "\n".join(lines)


#: "1. ", "4) ", "(3) ", "- 2. ", "**5.**" at the start of a line.
_LIST_MARKER_RE = re.compile(
    r"(?m)^([ \t>]*(?:[-*+•][ \t]+)?\*{0,2})(?:\d{1,2}[.)]|\(\d{1,2}\))\*{0,2}(?=[ \t]+\S)"
)
#: "## 4. Results", "### 4.2 Summary".
_HEADING_NUMBER_RE = re.compile(
    r"(?m)^([ \t]{0,3}#{1,6}[ \t]*)\d{1,2}(?:\.\d{1,2}){0,3}\.?(?=[ \t]+\S)"
)
#: "75th", "1st", "22nd".
_ORDINAL_RE = re.compile(r"(?<![\w.])\d+(?:st|nd|rd|th)\b", re.IGNORECASE)
#: "75 percentile", "90-percentile" (the ordinal form is `_ORDINAL_RE`'s).
_PERCENTILE_RANK_RE = re.compile(
    r"(?<![\w.])\d+(?:\.\d+)?(?=[ \t-]*percentiles?\b)", re.IGNORECASE
)
#: "top 5", "the first 3", "last 10", "next 4": the size of a selection.
_SELECTOR_SIZE_RE = re.compile(
    r"\b(top|bottom|upper|lower|first|last|next|previous|preceding|following|leading|best|worst)"
    r"([\s-]+(?:the\s+)?)(\d+)(?![\d.,]*\d)",
    re.IGNORECASE,
)
#: "5 highest-grade intervals", "the 10 deepest holes".
_SUPERLATIVE_COUNT_RE = re.compile(
    r"(?<![\w.])\d+(?=[ \t]+(?:highest|lowest|deepest|shallowest|best|worst|richest|longest|"
    r"shortest|largest|smallest|strongest|weakest|top)\b)",
    re.IGNORECASE,
)
#: Nouns that take a number as a NAME: "Zone 5", "Phase 4", "Level 12", "Step 4".
#: A plural only does in front of a list ("Zones 4 and 5"): "targets 50 holes"
#: is a verb and a count.
_LABEL_SINGULAR = (
    r"(?:zone|lens|unit|phase|stage|level|area|block|vein|target|type|class|layer|seam|lode|"
    r"cycle|series|group|bench|stope|grid|line|step|case|option|scenario|pit|domain|package|"
    r"campaign|trench|adit|shaft|drift|action|recommendation|priority|tier|category|rank|"
    r"finding)"
)
_LABEL_PLURAL = (
    r"(?:zones|lenses|units|phases|stages|levels|areas|blocks|veins|targets|types|classes|"
    r"layers|seams|lodes|cycles|groups|benches|stopes|grids|lines|steps|cases|options|"
    r"scenarios|pits|domains|packages|campaigns|trenches|adits|shafts|drifts|actions|"
    r"recommendations|priorities|tiers|categories|ranks|findings)"
)
# A label's number is short ("Zone 5", "Level 12", "Line 100E"): a plain
# three-digit figure after one of these nouns ("area 450 ha") is a quantity.
_LABEL_ID = r"(?:\d{1,2}[A-Za-z]?|\d{3}[A-Za-z])"
_LABEL_LIST = rf"{_LABEL_ID}(?:\s*(?:,|&|and|or|to|-|–)\s*{_LABEL_ID})"
_LABEL_NUMBER_RE = re.compile(
    rf"\b(?:{_LABEL_SINGULAR}\s+({_LABEL_ID}(?:\s*(?:,|&|and|or|to|-|–)\s*{_LABEL_ID})*)"
    rf"|{_LABEL_PLURAL}\s+({_LABEL_LIST}+))"
    r"(?![\w.,]*\d)",
    re.IGNORECASE,
)


def _unit_follows(text: str, pos: int) -> bool:
    return _QTY_AFTER_RE.match(text, pos) is not None


def _keep_heading_number(match: re.Match[str]) -> str:
    # "## 2.5 g/t cut-off" opens with a grade, not a section number.
    return match.group(0) if _unit_follows(match.string, match.end()) else match.group(1)


def _drop_selector_size(match: re.Match[str]) -> str:
    selector, glue = match.group(1), match.group(2)
    after = _QTY_AFTER_RE.match(match.string, match.end(3))
    if after is not None:
        # "first 150 m" is a length. Only "top 10 %" -- a fraction of the
        # samples -- is a selection that carries a unit.
        family = _CLAIM_UNIT_FAMILY.get(after.group("unit").lower())
        if family != "conc_pct" or selector.lower() not in ("top", "bottom", "upper", "lower"):
            return match.group(0)
    return f"{selector}{glue}"


def _drop_label_number(match: re.Match[str]) -> str:
    group = 1 if match.group(1) is not None else 2
    if _unit_follows(match.string, match.end(group)):
        return match.group(0)  # "Level 5 m" is not a name
    return match.group(0)[: match.start(group) - match.start(0)]


def _strip_labels(text: str) -> str:
    """``text`` without the numbers that are labels, ranks, list positions or
    selection sizes -- see the header above."""
    text = _strip_table_index_cells(text)
    text = _LIST_MARKER_RE.sub(r"\1", text)
    text = _HEADING_NUMBER_RE.sub(_keep_heading_number, text)
    text = _ORDINAL_RE.sub(" ", text)
    text = _PERCENTILE_RANK_RE.sub(" ", text)
    text = _SELECTOR_SIZE_RE.sub(_drop_selector_size, text)
    text = _SUPERLATIVE_COUNT_RE.sub(" ", text)
    return _LABEL_NUMBER_RE.sub(_drop_label_number, text)


def _strip_non_claims(text: str, identifiers: set[str] | frozenset[str] = frozenset()) -> str:
    """Remove everything in the answer whose digits are not a claim."""
    clean = _CITATION_MARKER_RE.sub(" ", text)
    clean = DESIGNATION_RE.sub(" ", clean)
    clean = _REFERENCE_RE.sub(" ", clean)
    clean = _strip_labels(clean)
    for ident in sorted(identifiers, key=len, reverse=True):
        if any(ch.isdigit() for ch in ident) and len(ident) <= 40:
            pattern = r"(?<![\w-])" + re.escape(ident) + r"(?![\w-])"
            clean = re.sub(pattern, " ", clean, flags=re.IGNORECASE)
    return _IDENTIFIER_TOKEN_RE.sub(" ", HOLE_ID_RE.sub(" ", clean))


def _extract_claims(
    text: str, identifiers: set[str] | frozenset[str] = frozenset()
) -> list[_Quantity]:
    """Every numerical claim in ``text``, with the unit it is written in and
    the sentence it is written in (`_Quantity.context`).

    The numbers `_SMALL_NUMBERS` calls too common to verify are left out of
    the claims but stay among their sentence's ``peers``: "2.8 m from 100.0
    to 102.8 m" still has two depths to take a difference of.
    """
    clean = _strip_non_claims(text, identifiers)
    spans: list[tuple[int, str]] = []
    pos = 0
    for unit in split_units(clean):
        spans.append((pos, unit.text))
        pos += len(unit.text) + len(unit.sep)
    starts = [start for start, _ in spans]

    located: list[tuple[int, _Quantity]] = []
    for q in _scan_quantities(clean):
        index = max(0, bisect.bisect_right(starts, q.start) - 1)
        sentence_start, sentence = spans[index]
        offset = max(0, q.start - sentence_start)
        located.append((index, dataclasses.replace(
            q,
            context=sentence,
            before=sentence[max(0, offset - 48):offset],
            after=sentence[max(0, q.end - sentence_start):max(0, q.end - sentence_start) + 24],
        )))
    peers: dict[int, list[_Quantity]] = {}
    ranges: dict[int, list[tuple[_Quantity, _Quantity]]] = {}
    for index, q in located:
        peers.setdefault(index, []).append(q)
    for (index, first), (other, second) in zip(located, located[1:], strict=False):
        if index == other and _written_as_range(clean, first, second):
            ranges.setdefault(index, []).append((first, second))

    peer_tuples = {index: tuple(found) for index, found in peers.items()}
    range_tuples = {index: tuple(found) for index, found in ranges.items()}
    return [
        dataclasses.replace(q, peers=peer_tuples[index], ranges=range_tuples.get(index, ()))
        for index, q in located
        if q.value not in _SMALL_NUMBERS
    ]


_RANGE_WORD_RE = re.compile(r"\s*(?:to|through|thru|-|–|—)\s*", re.IGNORECASE)
_BETWEEN_BEFORE_RE = re.compile(r"\bbetween\s+$", re.IGNORECASE)
_AND_RE = re.compile(r"\s*(?:and|&)\s*", re.IGNORECASE)


def _written_as_range(text: str, first: _Quantity, second: _Quantity) -> bool:
    """Whether ``first`` and ``second`` are the two ends of one range:
    "145.2 to 152.5 m", "145.2 m to 152.5 m", "120-126 m", "between 100 m and
    110 m". The unit after ``first`` may stand between them."""
    unit = _QTY_AFTER_RE.match(text, first.end)
    middle_from = unit.end() if unit is not None and unit.end() <= second.start else first.end
    middle = text[middle_from:second.start]
    if _RANGE_WORD_RE.fullmatch(middle):
        return True
    return bool(
        _AND_RE.fullmatch(middle)
        and _BETWEEN_BEFORE_RE.search(text[max(0, first.start - 12):first.start])
    )


def _extract_number_tokens(
    text: str, identifiers: set[str] | frozenset[str] = frozenset()
) -> list[tuple[float, float, float]]:
    """(value, exact tolerance, rounded tolerance) for every numerical claim
    in ``text`` — see `_written_tolerance`."""
    return [
        (claim.value, claim.exact, claim.rounded)
        for claim in _extract_claims(text, identifiers)
    ]


def _extract_numbers_from_text(text: str) -> list[float]:
    """Extract all numbers from response text.

    Citation markers, drill-hole identifiers, standard designations
    ("NI 43-101"), page/figure/section references and the labels, ranks and
    list positions of `_strip_labels` are removed first -- none is a
    numerical claim, and all parse as one. See `_NUMBER_RE` and
    `_IDENTIFIER_TOKEN_RE`.
    """
    return [value for value, _exact, _rounded in _extract_number_tokens(text)]


def _matches_grounded(value: float, tolerance: float, grounded: list[float]) -> bool:
    """Is ``value`` (as written, give or take its rounding) a grounded value?

    Compared by magnitude: a dip written -60 in one place and 60 in another
    is the same measurement under two sign conventions.
    """
    target = abs(value)
    return any(abs(target - abs(g)) <= tolerance + 1e-9 * max(1.0, abs(g)) for g in grounded)


def _near_any(value: float, tolerance: float, magnitudes: list[float]) -> bool:
    """`_matches_grounded` against a SORTED list of magnitudes: one bisect
    where the scan was one comparison per number of the evidence, per claim --
    a long answer against a few thousand collars took seconds of event loop."""
    target = abs(value)
    slack = tolerance + 1e-9 * max(1.0, target)
    i = bisect.bisect_left(magnitudes, target - slack)
    return i < len(magnitudes) and magnitudes[i] <= target + slack


# ────────────────────────────────────────────────────────────────────────
# Figures the answer works out from the evidence (2026-10-10 review, 2 and 4)
#
# A number that is in no tool result can still be arithmetic on what is in
# them. Four kinds are accepted, each only where the sentence says what it is:
#
#   * a statistic of a series -- a mean, median or percentile of the rows of
#     one structured series lies inside that series' own range. Accepted when
#     the sentence says it is a statistic (`_STATISTIC_RE`) and the number lies
#     in the range of the series the sentence is about (`_series_ranges`).
#     This is the one weak spot: a fabricated mean INSIDE the range cannot be
#     told from a mean of some subset of the rows, so it passes.
#   * a threshold -- "deeper than 250 m", "over 2.5 g/t": a bound the answer
#     (or the question) picked, true of the data whenever it lies inside the
#     range, since rows then sit on both sides of it.
#   * a count or a share worked out from the rows -- "5 holes intersected
#     more than 2 g/t", "42% of samples exceed 1 g/t": recounted from the rows
#     (`_recomputed_count`). Equal to the recount, or flagged.
#   * arithmetic on numbers the same sentence states -- a width that is the
#     difference of two stated depths, a percentage that is 100 a/b of two
#     stated counts. The operands are checked on their own.
# ────────────────────────────────────────────────────────────────────────

#: A sentence that says its figure is a statistic of a set.
_STATISTIC_RE = re.compile(
    r"\b(?:means?|averages?|averaged|averaging|avg|medians?|mid-?range|percentiles?|"
    r"quartiles?|quantiles?|deciles?|typical(?:ly)?)\b",
    re.IGNORECASE,
)

_FILLER_BEFORE = r"\s*(?:(?:a|an|the|of)\s+)?(?:(?:cut-?off|threshold|grade|value|depth)\s+(?:of\s+)?)?$"
#: What stands in front of a bound: "deeper than ", "above a ", "exceeding ", "> ".
_ABOVE_BEFORE_RE = re.compile(
    r"(?:\b(?:more|greater|higher|larger|bigger|deeper|longer|thicker|wider)\s+than(?:\s+or\s+equal\s+to)?"
    r"|\b(?:above|beyond|exceed(?:s|ed|ing)?|surpass(?:es|ed|ing)?|at\s+least)"
    r"|>=?|≥)" + _FILLER_BEFORE,
    re.IGNORECASE,
)
_BELOW_BEFORE_RE = re.compile(
    r"(?:\b(?:less|lower|smaller|fewer|shallower|shorter|thinner|narrower)\s+than(?:\s+or\s+equal\s+to)?"
    r"|\b(?:below|at\s+most)"
    r"|<=?|≤)" + _FILLER_BEFORE,
    re.IGNORECASE,
)
#: "over" / "under" bound a grade ("over 2.5 g/t") but also open an interval
#: width ("7.44 g/t over 12.6 m"), so they count for a grade only.
_OVER_BEFORE_RE = re.compile(r"\bover" + _FILLER_BEFORE, re.IGNORECASE)
_UNDER_BEFORE_RE = re.compile(r"\bunder" + _FILLER_BEFORE, re.IGNORECASE)


def _dimension(family: str | None) -> str:
    return _FAMILY_SCALES[family][0] if family in _FAMILY_SCALES else ""


def _bound_direction(number: _Quantity) -> str | None:
    """``"above"`` / ``"below"`` when ``number`` is written as a bound
    ("deeper than 250 m", "under 0.5 g/t"), else None."""
    before = number.before
    if _ABOVE_BEFORE_RE.search(before):
        return "above"
    if _BELOW_BEFORE_RE.search(before):
        return "below"
    if _dimension(number.family) == "grade":
        if _OVER_BEFORE_RE.search(before):
            return "above"
        if _UNDER_BEFORE_RE.search(before):
            return "below"
    return None


#: What a sentence is about, to the series of the evidence it can be about.
#: A sentence that names its subject is checked against that series alone: the
#: elevation of a collar is 512-523 m, and "the average depth is 515 m" is not
#: a statistic of THAT.
_SERIES_CLASSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("depth", re.compile(
        r"\b(?:depths?|deep(?:er|est)?|shallow(?:er|est)?|lengths?|long(?:er|est)?)\b", re.I)),
    ("thickness", re.compile(r"\b(?:thick(?:er|est|ness(?:es)?)?|wid(?:th|ths|e|er|est))\b", re.I)),
    ("elevation", re.compile(r"\b(?:elevations?|altitudes?|RL)\b", re.I)),
    ("dip", re.compile(r"\bdips?\b", re.I)),
    ("azimuth", re.compile(r"\b(?:azimuths?|bearings?|trends?)\b", re.I)),
    ("plunge", re.compile(r"\bplunges?\b", re.I)),
    ("strike", re.compile(r"\bstrikes?\b", re.I)),
    ("rqd", re.compile(r"\brqd\b", re.I)),
    ("recovery", re.compile(r"\brecover(?:y|ies)\b", re.I)),
    ("grade", re.compile(r"\b(?:grades?|assays?|concentrations?|content)\b", re.I)),
)
_CLASS_SERIES: dict[str, frozenset[str]] = {
    "depth": frozenset(("total_depth", "max_depth", "total_metres", "total_meters", "length")),
    "thickness": frozenset(("thickness", "width", "true_width")),
    "elevation": frozenset(("elevation",)),
    "dip": frozenset(("dip", "dip_deg")),
    "azimuth": frozenset(("azimuth", "trend", "trend_deg")),
    "plunge": frozenset(("plunge", "plunge_deg")),
    "strike": frozenset(("strike", "strike_deg")),
    "rqd": frozenset(("rqd",)),
    "recovery": frozenset(("recovery",)),
}


def _series_is_about(subject: str, family: str, series: str) -> bool:
    if subject == "grade":
        return series.startswith("grade:") or (
            family.startswith("conc_") and series not in ("rqd", "recovery")
        )
    return series in _CLASS_SERIES.get(subject, ())


_ELEMENT_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


@functools.lru_cache(maxsize=512)
def _sentence_subjects(context: str) -> tuple[str, ...]:
    """The series classes (`_SERIES_CLASSES`) a sentence names."""
    return tuple(name for name, pattern in _SERIES_CLASSES if pattern.search(context))


@functools.lru_cache(maxsize=512)
def _named_element_prefixes(context: str) -> frozenset[str]:
    """Lower-cased assay-key prefixes of the commodities a sentence names
    ("gold" or "Au" -> "au"; "uranium" -> "u3o8", "eu3o8", "u")."""
    from app.agent.tools import (  # noqa: PLC0415
        COMMODITY_ELEMENT_PREFIXES,
        commodities_in_query,
    )

    return frozenset(
        prefix
        for commodity in commodities_in_query(context)
        for prefix in COMMODITY_ELEMENT_PREFIXES.get(commodity, ())
    )


def _series_in_scope(
    context: str, family: str | None, ev: _Evidence
) -> list[tuple[tuple[str, str], float]]:
    """``((family, series), factor)`` for the series of ``ev`` a sentence can
    be about, with the factor that takes the series' unit into ``family``.

    A sentence that names its subject ("average depth", "dip", "grade") is
    about the series of that subject and no other -- none in the evidence, none
    in scope. One that names none can be about any series of the dimension of
    ``family`` (RQD and recovery only for a percentage: a "g/t" figure is not
    a statistic of them). A sentence that names a commodity ("gold", "Cu") is
    about the grade series of that element only: a mean of copper in percent
    is no mean of gold in ppm. A figure with no unit has no dimension to go
    by: it needs a named subject.
    """
    subjects = _sentence_subjects(context)
    elements = _named_element_prefixes(context)
    scope: list[tuple[tuple[str, str], float]] = []
    for key in ev.bounds:
        series_family, series = key
        if subjects:
            if not any(_series_is_about(s, series_family, series) for s in subjects):
                continue
        elif family is None or (series in ("rqd", "recovery") and family != "conc_pct"):
            continue
        if elements and series.startswith("grade:"):
            token = _ELEMENT_KEY_RE.match(series[len("grade:"):])
            if token is None or token.group().lower() not in elements:
                continue
        scope.extend(
            (key, factor)
            for factor in _conversion_factors(series_family, family or series_family)
        )
    return scope


def _series_ranges(claim: _Quantity, ev: _Evidence) -> list[tuple[float, float]]:
    """The ``(low, high)`` of each series ``claim`` can be a statistic of, in
    the claim's own unit."""
    ranges: list[tuple[float, float]] = []
    for key, factor in _series_in_scope(claim.context, claim.family, ev):
        low, high = ev.bounds[key][0] * factor, ev.bounds[key][1] * factor
        ranges.append((min(low, high), max(low, high)))
    return ranges


@functools.lru_cache(maxsize=512)
def _says_statistic(context: str) -> bool:
    return _STATISTIC_RE.search(context) is not None


def _states_a_statistic_or_bound(claim: _Quantity) -> bool:
    return _says_statistic(claim.context) or _bound_direction(claim) is not None


#: What may follow a bare statistic: "average depth is 328.4." / "328.4 deep".
#: Anything else ("200 holes", "12 samples") makes the number a count.
_BARE_FIGURE_AFTER_RE = re.compile(
    r"\s*(?:$|[.,;:)\]\-–—]|(?:deep|long|thick|wide|high|down|and|or|but|with|while|whereas|"
    r"which|vs\.?|versus)\b)",
    re.IGNORECASE,
)


def _within_a_series(claim: _Quantity, ev: _Evidence) -> bool:
    """A statistic, or a bound, that lies inside the range of a series."""
    if not _states_a_statistic_or_bound(claim):
        return False
    if claim.family is None and not _BARE_FIGURE_AFTER_RE.match(claim.after):
        return False
    tolerance = claim.exact * claim.magnitude
    for low, high in _series_ranges(claim, ev):
        slack = tolerance + 1e-9 * max(1.0, abs(low), abs(high))
        if low - slack <= claim.scaled <= high + slack or low - slack <= -claim.scaled <= high + slack:
            return True
    return False


def _is_count(claim: _Quantity) -> bool:
    return claim.family is None and claim.magnitude == 1.0 and float(claim.value).is_integer()


def _recomputed_count(claim: _Quantity, ev: _Evidence) -> bool:
    """A count, or a percentage, that is the recount of the rows.

    "5 holes intersected more than 2 g/t Au" and "42% of samples exceed 1 g/t"
    state a bound ("more than 2 g/t") and a figure worked out against it. The
    bound is read from the sentence, the rows of the series it is about are
    counted -- samples, and the holes they belong to -- and the figure must be
    one of those counts (or their share of the rows), whether the bound is
    meant strictly or not. No bound in the sentence, no recount, and the
    figure is literal or flagged: a count has no range to lie inside.
    """
    percent = claim.family == "conc_pct"
    if not (percent or _is_count(claim)):
        return False
    for bound in claim.peers:
        if bound.start == claim.start or bound.family is None:
            continue
        direction = _bound_direction(bound)
        if direction is None:
            continue
        for key, factor in _series_in_scope(claim.context, bound.family, ev):
            rows = ev.rows.get(key, [])
            if not rows:
                continue
            limit = bound.scaled / factor if factor else 0.0
            for strict in (True, False):
                if direction == "above":
                    hits = [r for r in rows if (r[0] > limit if strict else r[0] >= limit)]
                else:
                    hits = [r for r in rows if (r[0] < limit if strict else r[0] <= limit)]
                counts = (
                    (len(hits), len(rows)),
                    (len({h for _, h in hits if h}), len({h for _, h in rows if h})),
                )
                for hit, total in counts:
                    if percent:
                        if total and abs(claim.value - 100.0 * hit / total) <= claim.exact + 1e-9:
                            return True
                    elif claim.value == hit:
                        return True
    return False


def _length_factor(source: str | None, target: str | None) -> float:
    """What takes a length of family ``source`` into ``target`` (0.0 if either
    is not a length)."""
    if source is None or target is None:
        return 0.0
    factors = _conversion_factors(source, target)
    return factors[0] if len(factors) == 1 and _dimension(source) == "length" else 0.0


def _stated_difference(claim: _Quantity) -> bool:
    """A length that is the width of a range its own sentence states:
    "7.3 m from 145.2 to 152.5 m" -- the width of a composite is the
    difference of its two depths, and the depths are checked on their own.

    Only a range's own two ends are subtracted, and only for a number that is
    not one of its ends: any three numbers with a - b = c make each the
    difference of the other two ("10 m from 140.2 to 150.2 m" would ground
    140.2 as 150.2 - 10), and then nothing in the sentence would be checked.
    """
    if _dimension(claim.family) != "length":
        return False
    if any(claim.start in (first.start, second.start) for first, second in claim.ranges):
        return False
    claimed = abs(claim.scaled)
    for first, second in claim.ranges:
        to_first = _length_factor(first.family, claim.family)
        to_second = _length_factor(second.family, claim.family)
        if not (to_first and to_second):
            continue
        gap = abs(second.scaled * to_second - first.scaled * to_first)
        tolerance = claim.exact + first.exact * to_first + second.exact * to_second
        if abs(gap - claimed) <= tolerance + 1e-9 * max(1.0, gap):
            return True
    return False


def _share_of_counts(claim: _Quantity) -> bool:
    """A percentage that is 100 a/b of two counts its sentence states:
    "8 of the 12 holes (67%)". The counts are checked on their own."""
    if claim.family != "conc_pct":
        return False
    counts = [
        p.value for p in claim.peers
        if p.family is None and p.magnitude == 1.0 and p.value > 0
        and float(p.value).is_integer() and p.start != claim.start
    ]
    return any(
        part < whole and abs(claim.value - 100.0 * part / whole) <= claim.exact + 1e-9
        for part in counts
        for whole in counts
    )


#: "5 times the median", "a 3-fold increase" / "2.4 standard deviations above".
_TIMES_AFTER_RE = re.compile(r"\s*(?:times\b|×|x\b|-?\s?fold\b)", re.IGNORECASE)
_SIGMAS_AFTER_RE = re.compile(
    r"\s*(?:standard\s+deviations?|std\.?\s*dev(?:iations?)?|sigmas?\b|σ|SDs?\b)", re.IGNORECASE
)


def _stated_multiple(claim: _Quantity, ev: _Evidence) -> bool:
    """A multiple worked out from the tool's own aggregates of one result:
    "about 5 times the median" is max / median, "2.4 standard deviations above
    the mean" is (max - mean) / std. Only the aggregates of ONE result are
    divided into each other (a ratio of any two grounded numbers would ground
    nearly any figure), and only a figure written as a multiple is read so."""
    if claim.family is not None or claim.magnitude != 1.0:
        return False
    in_sigmas = _SIGMAS_AFTER_RE.match(claim.after) is not None
    if not in_sigmas and _TIMES_AFTER_RE.match(claim.after) is None:
        return False
    for stats in ev.stat_sets:
        named = {name: value for name, value in stats.items() if name != "std"}
        if in_sigmas:
            std = stats.get("std")
            candidates = [] if not std else [
                abs(a - b) / std for x, a in named.items() for y, b in named.items() if x != y
            ]
        else:
            candidates = [
                abs(a / b) for x, a in named.items() for y, b in named.items() if x != y and b
            ]
        if any(abs(claim.value - c) <= claim.rounded + 1e-9 for c in candidates):
            return True
    return False


def _grounding_route(claim: _Quantity, ev: _Evidence) -> str | None:
    """How ``ev`` supports ``claim``: ``"literal"``, ``"converted"``,
    ``"derived"``, or None when it does not.

    * literal -- the same figure, give or take the rounding it was written
      with (`_written_tolerance`), under any unit; "71 million" is 71,000,000.
      Identifiers, scores, pages and section numbers are not content
      (`_NON_CONTENT_KEYS`). A figure the EVIDENCE scales with a "million"
      grounds only a claim that states no unit (`_Evidence.scaled`): the
      scaled figure carries its own unit, so "48.2 million tonnes" is never
      48,200,000 ounces.
    * converted -- the same quantity in ANOTHER unit of its dimension, at the
      real factor between the two (`_conversion_factors`): 0.02 % is 200 ppm
      and never 20 ppm, 48,200,000 t is 48.2 Mt, 1.85 g/t is 0.054 oz/ton.
      Only a number written with a unit can be converted, and only from a
      value the evidence states with one.
    * derived -- worked out from the evidence, in one of the ways listed under
      "Figures the answer works out from the evidence" above. Document prose
      has no series and no rows, so only arithmetic on the sentence's own
      numbers applies to it.
    """
    literal = ev.literal_index()
    if _near_any(claim.value, claim.rounded, literal):
        return "literal"
    if claim.magnitude != 1.0 and _near_any(
        claim.scaled, claim.rounded * claim.magnitude, literal
    ):
        return "literal"
    if claim.family is None and _matches_grounded(
        claim.scaled, claim.rounded * claim.magnitude, [figure for figure, _ in ev.scaled]
    ):
        return "literal"

    if claim.family is not None:
        # The written precision, in the claim's own unit and with its "million".
        tolerance = claim.exact * claim.magnitude
        target = abs(claim.scaled)
        for family, values in ev.quantity_index().items():
            for factor in _conversion_factors(family, claim.family):
                # |target - value * factor| <= tolerance, as a window on value
                if _near_any(target / factor, (tolerance + 1e-9 * max(1.0, target)) / factor, values):
                    return "converted"

    if (
        _stated_difference(claim)
        or _share_of_counts(claim)
        or _stated_multiple(claim, ev)
        or _recomputed_count(claim, ev)
        or _within_a_series(claim, ev)
    ):
        return "derived"
    return None


def verify_numbers(
    text: str,
    tool_results: list[tuple[str, Any]],
    *,
    proactive_insights_offset: int | None = None,
) -> list[str]:
    """Layer 3: Check that every number in the response is grounded in tool results.

    C3 tightening (Module 6 Chunk 3): removed the silent-skip for ≤ 3
    ungrounded numbers.  Every numeric token must be derivable from cited
    evidence or a valid unit conversion of a cited value.

    Phase F.5: strip the proactive-insights block before extracting numbers.
    Those numbers (mean depth, σ multiples) are deterministically computed
    by ``anomaly_detector`` from raw tool_results rows and don't appear
    verbatim in the cited tool results — they're grounded by construction,
    not by retrieval. ``proactive_insights_offset`` (normally
    ``response.proactive_insights_offset``) is the structural boundary the
    strip uses — see ``anomaly_detector.strip_proactive_insights`` for why
    it must come from assembly-time bookkeeping rather than a text search.

    Returns a list of warning strings for ungrounded numbers.
    """
    if not settings.NUMERICAL_VERIFICATION_ENABLED:
        return []

    from app.agent.anomaly_detector import strip_proactive_insights  # noqa: PLC0415
    text = strip_proactive_insights(text, proactive_insights_offset)

    response_numbers = _extract_numbers_from_text(text)
    if not response_numbers:
        return []

    # L3 numeric-tuple atomicity check. Three modes per
    # settings.L3_TUPLE_GUARD_MODE:
    #   shadow → log mismatches, do not warn (Phase A, default)
    #   warn   → append warnings to the return list (Phase B)
    #   fail   → same as warn (the existing tolerance pipeline decides
    #            whether warnings reject the answer; this guard doesn't
    #            need to short-circuit independently)
    _l3_tuple_warnings: list[str] = []
    try:
        _mode = getattr(settings, "L3_TUPLE_GUARD_MODE", "shadow") or "shadow"
        _resp_tuples = _extract_number_unit_tuples(text)
        if _resp_tuples:
            _grounded_tuples = _collect_grounded_tuples(tool_results)
            _mismatches = _detect_unit_mismatches(_resp_tuples, _grounded_tuples)
            if _mismatches:
                logger.info(
                    "L3 tuple mode=%s: %d mismatch(es) detected — %s",
                    _mode,
                    len(_mismatches),
                    _mismatches[:3],
                )
                if _mode in ("warn", "fail"):
                    _l3_tuple_warnings.extend(_mismatches)
    except Exception:
        logger.debug("L3 tuple guard: extractor raised — skipping", exc_info=True)

    # Grounding (reworked 2026-09-29, audit RAG-1; unit-aware since
    # 2026-10-10, see `_grounding_route`): literal, converted at the real
    # factor between two units of one dimension, or derived -- worked out from
    # the evidence in a way the sentence says it is (a statistic or a bound
    # inside the range of the series it is about, a recount of the rows,
    # arithmetic on its own numbers). The old
    # "equals the number of distinct grounded values" rule is gone: that
    # count is an artefact of serialisation, not of the data. Row counts ARE
    # grounded now; every list in a structured result adds its length
    # (`_walk_evidence`).
    evidence = _collect_evidence(tool_results)

    warnings = []
    for claim in _extract_claims(text, evidence.identifiers):
        route = _grounding_route(claim, evidence)
        if route == "derived":
            logger.debug(
                "Layer 3 derivation: %s %s lies inside a structured series of "
                "its dimension, likely a mean/median/percentile",
                claim.value, claim.family,
            )
        if route is not None:
            continue

        warnings.append(
            f"Layer 3: Ungrounded number {claim.value} in response — "
            f"not found in any tool result (direct or via unit conversion)"
        )

    # C3: silent-skip threshold REMOVED. Report every ungrounded number.
    if warnings:
        logger.warning(
            "orchestrator_validators: %d ungrounded number(s) detected "
            "(threshold removed per Module 6 Chunk 3 tightening; a derived "
            "figure is accepted only where its sentence says what it is and "
            "the evidence bears it out -- see _grounding_route)",
            len(warnings),
        )

    # Append L3 tuple warnings only if we collected any AND the mode is
    # not shadow. In shadow mode this list is always empty — the
    # mismatches were logged but never elevated. The combined list is
    # what the orchestrator sees; existing tolerance logic
    # (GUARD_TOLERANCE_NUMERIC_UNGROUNDED) handles both warning kinds
    # uniformly.
    if _l3_tuple_warnings:
        warnings.extend(_l3_tuple_warnings)
    return warnings


#: Prefix of the per-sentence, advisory Layer 3 finding: a number that IS in
#: the retrieved evidence, but not in the evidence of the id the sentence
#: cites. Deliberately NOT one of ``LAYER3_WARNING_PREFIXES``: it opens with
#: "Layer 3 advisory:", so neither the severity classifier (retry / floor /
#: banner) nor ``confidence_computer._is_layer3_warning`` (the x0.7 demotion)
#: counts it, while ``nodes._banner_reason`` -- which reads only the layer
#: digit -- still maps it to the Layer 3 "numbers" reason, and it stays in
#: ``validation_warnings`` for the logs and the lineage row.
LAYER3_CITED_ELSEWHERE_PREFIX = "Layer 3 advisory: number "


def _evidence_by_citation_id(
    tool_results: list[tuple[str, Any]],
) -> dict[str, _Evidence]:
    """Content numbers per citation id, in the ids ``assemble_response`` emits.

    A document search yields one id per CHUNK, a public-geoscience search one
    per RECORD, every other tool one for the whole result
    (``response_assembler.assign_citation_ids``) -- the same ids the answer's
    markers carry, so a sentence's own citations can be looked up directly.
    """
    from app.agent.public_geoscience_tool import (  # noqa: PLC0415
        PublicGeoscienceSearchResult,
    )
    from app.agent.response_assembler import assign_citation_ids  # noqa: PLC0415
    from app.agent.tools import DocumentSearchResult  # noqa: PLC0415

    out: dict[str, _Evidence] = {}
    bundles = assign_citation_ids(tool_results)
    for (tool_name, result), bundle in zip(tool_results, bundles, strict=False):
        units: list[tuple[str, Any, bool]] = []
        if isinstance(result, PublicGeoscienceSearchResult):
            units = [(cid, rec, False) for cid, rec in zip(bundle, result.records, strict=False)]
        elif isinstance(result, DocumentSearchResult) and result.chunks:
            units = [(cid, ch, False) for cid, ch in zip(bundle, result.chunks, strict=False)]
        elif bundle:
            units = [(bundle[0], result, not _is_document_result(tool_name, result))]
        for citation_id, obj, structured in units:
            ev = _Evidence()
            try:
                _walk_evidence(obj, ev, structured=structured)
            except Exception:
                logger.debug("Layer 3: per-id evidence walk failed for %s", citation_id, exc_info=True)
                continue
            out[citation_id] = ev
    return out


def _number_in_evidence(claim: _Quantity, ev: _Evidence) -> bool:
    """The grounding test of :func:`verify_numbers`, against one evidence set."""
    return _grounding_route(claim, ev) is not None


def verify_cited_number_support(
    text: str,
    tool_results: list[tuple[str, Any]],
    *,
    proactive_insights_offset: int | None = None,
) -> list[str]:
    """Layer 3, per sentence: a number must be in the evidence it CITES.

    :func:`verify_numbers` grounds every number against ALL retrieved
    evidence, so a figure lifted from chunk [NI43-7] and cited to [NI43-2]
    passes. Here, for each sentence carrying citation markers, the sentence's
    numbers are checked against the evidence of the ids it cites; one that is
    absent there but present in OTHER retrieved evidence yields

        ``Layer 3: number N cited to [X] appears only in [Y]``

    Advisory by construction (it never sets ``should_retry`` -- see
    ``LAYER3_CITED_ELSEWHERE_PREFIX``): a number found in no evidence at all
    is :func:`verify_numbers`' finding, not this one, and a sentence may
    legitimately cite one chunk for context and its neighbour for a figure.
    """
    if not settings.NUMERICAL_VERIFICATION_ENABLED:
        return []

    from app.agent.anomaly_detector import strip_proactive_insights  # noqa: PLC0415
    from app.agent.hallucination.citation_markers import (  # noqa: PLC0415
        CITATION_MARKER_CAPTURE_RE,
        canonical_marker,
    )
    text = strip_proactive_insights(text, proactive_insights_offset)
    per_id = _evidence_by_citation_id(tool_results)
    if len(per_id) < 2:
        return []
    identifiers: set[str] = set()
    for ev in per_id.values():
        identifiers |= ev.identifiers

    warnings: list[str] = []
    seen: set[tuple[float, tuple[str, ...]]] = set()
    for unit in split_units(text):
        cited = list(dict.fromkeys(
            canonical_marker(m.group(1), m.group(3))
            for m in CITATION_MARKER_CAPTURE_RE.finditer(unit.text)
        ))
        cited = [c for c in cited if c in per_id]
        if not cited:
            continue
        for claim in _extract_claims(unit.text, identifiers):
            num = claim.value
            if any(_number_in_evidence(claim, per_id[c]) for c in cited):
                continue
            elsewhere = [
                cid for cid, ev in per_id.items()
                if cid not in cited and _number_in_evidence(claim, ev)
            ]
            if not elsewhere:
                continue  # in nothing at all: verify_numbers' finding
            key = (num, tuple(cited))
            if key in seen:
                continue
            seen.add(key)
            warnings.append(
                f"{LAYER3_CITED_ELSEWHERE_PREFIX}{num:g} cited to "
                f"{', '.join(cited)} appears only in {', '.join(elsewhere[:3])}"
            )
    if warnings:
        logger.warning(
            "orchestrator_validators: %d number(s) cited to evidence that does "
            "not contain them (found only in other retrieved chunks): %s",
            len(warnings), warnings,
        )
    return warnings


# ---------------------------------------------------------------------------
# Layer 4 — Entity Resolution (orchestrator version)
# ---------------------------------------------------------------------------

# Layer 4's hole-ID check is the ONE warning the severity classifier treats
# as critical on its own — every other Layer 4 warning needs three of them
# to escalate — so a format it cannot see has no backstop.
#
# It used to carry its own pattern: letters plus TWO dash-separated numeric
# groups, case-sensitive. The retrieval side recognises three shapes, and
# that pattern matched one of them. A model inventing "hole 36-9999
# intersected 4.2 m at 8.1 g/t Au" on a Cameco project, or "DDH-1234", was
# never checked against silver.collars at all: no query, no warning, no
# retry, and the fabricated hole shipped at whatever confidence retrieval
# happened to produce.
#
# Now reads the shared definitions in app.agent.hole_id_patterns, which is
# also what viz_builder routes queries with. One place to add a format.
_HOLE_ID_RE = HOLE_ID_RE
_NUMERIC_HOLE_ID_RE = NUMERIC_HOLE_ID_RE
_HOLE_CONTEXT_RE = HOLE_CONTEXT_RE
_CITATION_PREFIX_SET = CITATION_PREFIXES

# Known commodity codes (Module 4 identifier-boost list).
# Any of these tokens, if mentioned bare, must appear in the cited evidence.
_COMMODITY_CODES: frozenset[str] = frozenset({
    "Au", "Ag", "Cu", "Zn", "Pb", "Mo", "Ni", "Co", "U", "U3O8",
    "W", "Sn", "Bi", "Te", "V", "Pt", "Pd", "Rh", "REE", "Li",
})

# Proper-noun heuristic: token is TitleCase (starts uppercase, ≥4 chars,
# not all-caps, contains ≥1 lowercase).  Used to detect formation / project
# names without an NER model dependency.
_TITLE_CASE_RE = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*)\b")

# Colon-form and dash-form citation markers — stripped before entity extraction.
_ALL_MARKER_RE = ALL_MARKER_RE


# Phase F.6+ (Layer 4 tolerance fix).
#
# Common English words that pass the TitleCase regex at sentence starts —
# they aren't formations, project names, or anything else worth grounding
# against Neo4j. Skipping them at extraction time avoids false-positive
# Layer 4 warnings on every "This deposit is..." sentence the LLM writes.
#
# Compared against the lower-cased single-word match. Compound matches
# ("Knowledge Graph") are checked word-by-word later in `_is_grounded_name`.
_TITLE_CASE_STOPWORDS: frozenset[str] = frozenset({
    # Demonstratives + articles
    "this", "that", "these", "those", "the",
    # Pronouns / possessives
    "they", "their", "them", "theirs",
    "his", "her", "hers", "its",
    # Transitional sentence-starters
    "then", "thus", "therefore", "however", "moreover", "additionally",
    "furthermore", "consequently", "meanwhile", "nevertheless",
    "also", "besides", "indeed", "instead", "otherwise",
    # Interrogatives / wh-words
    "when", "where", "why", "what", "which", "who", "whom", "whose", "how",
    # Modal / auxiliary verbs (sentence starts)
    "can", "may", "might", "could", "would", "should", "must", "shall",
    "will", "have", "has", "had", "is", "are", "was", "were", "been", "being",
    # Imperative / transitional cues
    "consider", "note", "see", "below", "above", "verify",
    "based", "given", "assuming", "since", "because",
    # System / UI / explanatory terminology the LLM repeats from prompts
    "knowledge", "graph", "report", "reports", "deposit", "deposits",
    "drilling", "drill", "hole", "holes", "data", "tool", "tools",
    "result", "results", "response", "answer", "query", "search",
    # Plan / process language that surfaces in answers
    "proactive", "insights", "depth", "anomaly", "anomalies",
    "summary", "section", "chapter", "table", "figure", "appendix",
})

# Phase F.6+ geographic whitelist.
#
# Place names the LLM mentions when sourcing answers from geological
# context. These are grounded in geography itself; we don't require them
# to appear as Formation nodes in Neo4j (they aren't formations).
# Lower-cased for case-insensitive lookup.
_GEOGRAPHIC_PROPER_NOUNS: frozenset[str] = frozenset({
    # US states (50)
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada", "ohio",
    "oklahoma", "oregon", "pennsylvania", "tennessee", "texas", "utah",
    "vermont", "virginia", "washington", "wisconsin", "wyoming",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "south carolina", "south dakota",
    "rhode island", "west virginia",
    # DC + US territories
    "district of columbia", "puerto rico", "guam",
    # Canadian provinces + territories
    "alberta", "british columbia", "manitoba", "new brunswick",
    "newfoundland", "labrador", "nova scotia", "ontario",
    "prince edward island", "quebec", "québec", "saskatchewan",
    "yukon", "nunavut", "northwest territories",
    # Country names that commonly surface in geological text
    "canada", "united states", "usa", "america",
    # Compass / geographic qualifiers paired with TitleCase regions
    "north", "south", "east", "west", "central",
    "northern", "southern", "eastern", "western", "northeast",
    "northwest", "southeast", "southwest",
})


def _is_grounded_name(
    name: str,
    formations: frozenset[str],
    tool_tokens: set[str],
) -> bool:
    """Return True when *name* is a known geographic noun, English stopword,
    cached formation, or appears in the tool-result token bag.

    Compound names (multi-word TitleCase) are accepted when **every**
    non-stopword constituent word is itself grounded — e.g. "Cameco
    Shirley Basin Uranium" passes if "cameco", "shirley", "basin", and
    "uranium" each appear in tool_tokens or formations, even if no
    Formation node exists for the literal compound.
    """
    lower = name.lower()
    if lower in _TITLE_CASE_STOPWORDS:
        return True
    if lower in _GEOGRAPHIC_PROPER_NOUNS:
        return True
    if lower in formations:
        return True
    if lower in tool_tokens:
        return True

    # Compound names: split + recurse-without-recursing.
    if " " in lower:
        parts = lower.split()
        # Strip stopwords first so "Cameco Shirley Basin Uranium" doesn't
        # fail on "Basin" by itself. Every remaining word must be grounded.
        meaningful = [p for p in parts if p not in _TITLE_CASE_STOPWORDS]
        if not meaningful:
            return True
        return all(
            p in _GEOGRAPHIC_PROPER_NOUNS
            or p in formations
            or p in tool_tokens
            for p in meaningful
        )

    return False


def _collect_value_strings(obj: Any) -> list[str]:
    """Recursively collect stringified leaf VALUES from a tool-result object.

    Deliberately skips dict KEYS — structural field names (``section_title``,
    ``document_type``, ``hole_id``, ``relevance_score``, …) are part of the
    response *schema*, not evidence the tools returned, and must not ground a
    fabricated entity name. Only the values the tools actually produced count.

    Dataclass and Pydantic instances are walked the same way, field VALUES
    only. They used to fall through every branch and contribute nothing, and
    the tools nest them: ``DocumentSearchResult.chunks`` is a list of
    ``DocumentChunk`` dataclasses, so no retrieved passage text ever reached
    the bag. Every commodity and proper noun in a document-grounded answer
    was reported as "not found in any tool result" -- the first live AWS
    query (2026-09-28) flagged 'Au' on an answer quoting "15.6 g/t Au"
    straight from its sources -- and three such warnings escalate to
    critical, which floors confidence and re-calls the LLM.
    """
    out: list[str] = []
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for field in dataclasses.fields(obj):
            out.extend(_collect_value_strings(getattr(obj, field.name)))
    elif isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            out.extend(_collect_value_strings(getattr(obj, name)))
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_collect_value_strings(v))
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            out.extend(_collect_value_strings(v))
    elif isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, (int, float, bool)) or obj is None:
        out.append(str(obj))
    return out


_SUBSCRIPT_DIGITS = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")

# Two or more element-and-count fragments separated only by whitespace:
# "U3 O8", "Fe2\nO3". PDF text extraction puts a formula's subscripts on
# their own baseline, so "U3O8" in a report arrives as "U3\r\nO8".
_SPLIT_FORMULA_RE = re.compile(r"\b[A-Z][a-z]?\d+(?:\s+[A-Z][a-z]?\d+)+\b")


def _formula_tokens(value: str) -> set[str]:
    """Chemical formulas in ``value`` as the answer would write them.

    Covers the two ways a formula stops matching its plain spelling in
    extracted text: Unicode subscripts ("U₃O₈") and subscripts split onto
    their own line ("U3\r\nO8"). The first live rehearsal (2026-09-23)
    warned "Commodity 'U3O8' mentioned but not found in any tool result"
    on an answer citing twelve chunks of a uranium report, every one of
    which carried the formula in the split form. Only element-and-count
    fragments are joined, so ordinary prose cannot assemble a formula.
    """
    text = value.translate(_SUBSCRIPT_DIGITS)
    return {re.sub(r"\s+", "", m.group(0)).lower() for m in _SPLIT_FORMULA_RE.finditer(text)} | {
        tok.lower() for tok in re.findall(r"\b(?:[A-Z][a-z]?\d+){2,}\b", text)
    }


def _extract_entities_from_tool_results(
    tool_results: list[tuple[str, Any]],
) -> set[str]:
    """Collect entity-like tokens from tool-result VALUES for grounding.

    Returns a set of lower-cased tokens that appear in the *values* of the tool
    output. Used to verify entities mentioned in the answer came from the tools,
    not from the LLM's training data.

    Audit 2026-06-28: previously this serialized the whole result with
    ``json.dumps`` (KEYS INCLUDED) and tokenised that. Structural field names
    leaked into the bag, so a fabricated compound entity grounded as long as
    each constituent word coincided with some key or value anywhere in any
    payload — a false sense of grounding (the formation/entity check would not
    warn on plausible fabrications). Now we walk VALUES ONLY. The 2+ char floor
    is kept on purpose: this same bag grounds 2-char commodity codes (Au, Ag,
    Cu) in the commodity check, which a 3-char floor would break.
    """
    entity_tokens: set[str] = set()
    for _tool_name, result in tool_results:
        try:
            if hasattr(result, "model_dump"):
                payload: Any = result.model_dump()
            elif hasattr(result, "__dict__"):
                payload = result.__dict__
            else:
                payload = result
            for value in _collect_value_strings(payload):
                for tok in re.findall(r"\b[A-Za-z][A-Za-z0-9_-]{1,}\b", value):
                    entity_tokens.add(tok.lower())
                    # Split column-style compounds so "Au_ppm" also grounds
                    # "au" (and "ppm") — assay fields arrive as unit-suffixed
                    # identifiers far more often than as bare symbols.
                    for part in re.split(r"[_\-]", tok):
                        if part:
                            entity_tokens.add(part.lower())
                # Single-letter commodities (U, W, V) can never pass the
                # 2+ char token floor above — capture them when they appear
                # as standalone tokens.
                for tok in re.findall(r"\b[UWV]\b", value):
                    entity_tokens.add(tok.lower())
                entity_tokens.update(_formula_tokens(value))
        except Exception:
            continue
    return entity_tokens


def _commodity_grounded(sym: str, bag: set[str]) -> bool:
    """True when commodity symbol *sym* is grounded by the tool-result bag.

    Accepts three grounding forms: the bare symbol ("au"), the spelled-out
    name from the query-expansion table ("gold"; multi-word names like
    "rare earth elements" need every word present), or a column-style
    compound token ("au_ppm" / "au-ppm").
    """
    from app.services.geological_query_expansion import _ABBREVIATIONS  # noqa: PLC0415

    s = sym.lower()
    if s in bag:
        return True
    full = _ABBREVIATIONS.get(sym)
    if full and all(w in bag for w in full.lower().split()):
        return True
    return any(
        tok == s or tok.startswith(s + "_") or tok.startswith(s + "-")
        for tok in bag
    )


#: "BH 12", "DDH 7" — the spaced form of a lettered hole ID as reports and
#: OCR text often write it. Only used on the EVIDENCE side, to recognise a
#: hole the evidence names; never on the answer.
_SPACED_HOLE_ID_RE = re.compile(r"\b[A-Z]{2,6}\d{0,4} \d{1,6}\b")
_ID_LIKE_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _./-]{0,23}$")


def _evidence_hole_ids(tool_results: list[tuple[str, Any]]) -> set[str]:
    """Position-aware keys (see `hole_id_key`) of every hole the evidence names.

    Reads tool-result VALUES only: lettered IDs and bare numeric IDs found
    in any text (chunk prose, titles), their spaced variants, and short
    ID-shaped values in their own right (the ``hole_id`` of a collar row).
    Deliberately generous — it only ever DOWNGRADES a Layer 4 finding.
    """
    out: set[str] = set()
    for _tool_name, result in tool_results:
        try:
            if hasattr(result, "model_dump"):
                payload: Any = result.model_dump()
            elif hasattr(result, "__dict__"):
                payload = result.__dict__
            else:
                payload = result
            for value in _collect_value_strings(payload):
                if not any(ch.isdigit() for ch in value):
                    continue
                for m in HOLE_ID_RE.finditer(value):
                    out.add(hole_id_key(m.group(1)))
                for m in NUMERIC_HOLE_ID_RE.finditer(value):
                    out.add(hole_id_key(m.group(1)))
                for m in _SPACED_HOLE_ID_RE.finditer(value.upper()):
                    out.add(hole_id_key(m.group(0)))
                if _ID_LIKE_VALUE_RE.match(value.strip()):
                    out.add(hole_id_key(value))
        except Exception:
            continue
    return out


def _measured_holes(answer: str, hole_ids: list[str]) -> set[str]:
    """`hole_id_key` keys of the holes the answer attributes a measured value to.

    Keyed with `hole_id_key`, the same identity `verify_entities` uses, NOT
    the separator-free `canonical_hole_id`: that merges "PLS-2-28" with
    "PLS-22-8", so a measured value attributed to one would be charged to the
    other (audit item H).

    A measured value is a number with a unit (`_NUMBER_WITH_UNIT_RE`). Each
    is attributed to the nearest hole mention BEFORE it in the same
    sentence — "Unlike BH-21, BH-12 intersected 7.4 g/t" gives 7.4 g/t to
    BH-12 — or, when none precedes it, to the first one after it.
    """
    measured: set[str] = set()
    for unit in split_units(answer):
        sentence = unit.text
        mentions: list[tuple[int, int, str]] = []
        for hid in hole_ids:
            for m in re.finditer(r"(?<![\w-])" + re.escape(hid) + r"(?![\w-])", sentence, re.IGNORECASE):
                mentions.append((m.start(), m.end(), hole_id_key(hid)))
        if not mentions:
            continue
        masked = [(a, b) for a, b, _ in mentions] + [
            (m.start(), m.end()) for m in _ALL_MARKER_RE.finditer(sentence)
        ]
        for m in _NUMBER_WITH_UNIT_RE.finditer(sentence):
            if any(a < m.end() and m.start() < b for a, b in masked):
                continue
            before = [x for x in mentions if x[1] <= m.start()]
            owner = max(before, key=lambda x: x[1]) if before else min(mentions, key=lambda x: x[0])
            measured.add(owner[2])
    return measured


def _not_in_evidence_warning(hole_id: str, answer: str, hole_ids: list[str]) -> str:
    """Layer 4 finding for a real hole the retrieved evidence never mentions.

    Critical when the answer attributes a measured value (a number with a
    unit) to the hole: that is the swapped-hole shape — BH-12's intercept
    reported against BH-21 — and nothing else would catch it, because BH-21
    exists and the number exists. Otherwise advisory: a hole named in
    passing ("unlike BH-21, ...") is not a claim about it.
    """
    if hole_id_key(hole_id) in _measured_holes(answer, hole_ids):
        return (
            f"Layer 4: Drill-hole ID '{hole_id}' exists in silver.collars but "
            f"appears in none of the evidence retrieved for this answer, and the "
            f"answer attributes a measured value to it"
        )
    return (
        f"Layer 4 advisory: Hole '{hole_id}' exists in silver.collars but is "
        f"not in any evidence retrieved for this answer"
    )


async def verify_entities(
    text: str,
    project_id: str,
    pg_pool: Any,
    neo4j_driver: Any,
    tool_results: list[tuple[str, Any]] | None = None,
    *,
    proactive_insights_offset: int | None = None,
) -> list[str]:
    """Layer 4: Check that entities in the response exist in the data stores.

    Module 6 Chunk 3 expansion (beyond hole IDs):
      - Formations/lithologies: check proper-noun-heuristic tokens against
        Neo4j Formation nodes for the project (fail-open if Neo4j empty).
      - Commodities: commodity codes (Au, Ag, Cu, …) must appear in cited
        tool results.
      - Project names / quoted names: proper-noun tokens from tool result
        grounding (lightweight dictionary, no NER dep).

    Returns a list of warning strings for unresolved entities.
    """
    import asyncio

    if not settings.ENTITY_RESOLUTION_ENABLED:
        return []

    # Phase F.5: strip the proactive-insights block before entity
    # extraction.  Insight bullets contain common-word TitleCase tokens
    # ("Depth", "Consider") and the literal "Proactive Insights" header that
    # would otherwise be flagged as unresolved formations.
    # ``proactive_insights_offset`` (normally
    # ``response.proactive_insights_offset``) is the structural boundary —
    # see ``anomaly_detector.strip_proactive_insights`` for why it must
    # come from assembly-time bookkeeping rather than a text search.
    from app.agent.anomaly_detector import strip_proactive_insights  # noqa: PLC0415
    text = strip_proactive_insights(text, proactive_insights_offset)

    # Strip all citation markers before extraction.
    clean = _ALL_MARKER_RE.sub("", text)

    # --- Hole IDs (original check) ---
    # Standard designations first: "NI43-101" would match the lettered
    # pattern and the "43-101" of "NI 43-101" the numeric one.
    clean = DESIGNATION_RE.sub(" ", clean)
    # Lettered IDs, minus the shapes that are words, dates, standards or
    # isotopes ("Pre-2010", "Zone-3", "Oct-2011", "Pb-206"): each used to be
    # reported as a critical fabricated drill hole (2026-10-10 audit,
    # finding 8). A hole word right in front of the token still makes it one.
    candidates = find_lettered_hole_ids(clean)
    # Compact IDs ("BH21", "DDH0023", "SRE0912") have no dash for HOLE_ID_RE to
    # find; they count with a drill-type prefix or a hole word in front, and a
    # compact spelling of a hole already named with its dash is the same hole
    # (finding 9).
    _named = {hole_id_key(c) for c in candidates}
    for _m in iter_compact_hole_id_matches(clean):
        if hole_id_key(_m.group(1)) not in _named:
            candidates.append(_m.group(1))
            _named.add(hole_id_key(_m.group(1)))
    # Bare numeric IDs (36-1085, the Cameco Shirley Basin shape), only when
    # the answer talks about holes, and never the numeric tail of an
    # alphanumeric ID already matched above: "22-08" inside "PLS-22-08" was
    # reported as a second, fabricated hole — critical on its own — on
    # correct answers (fixed 2026-09-15; containment is deliberately
    # conservative, an answer naming both "PLS-22-08" and a separate "22-08"
    # loses the check on the latter).
    #
    # `find_numeric_hole_ids` also drops the shapes that are not holes at
    # all: a depth interval followed by its unit ("120-126 m"), a page /
    # figure / section / item reference, an interval preposition ("from
    # 120-126") and a bare year range. Each of those was reported as a
    # critical fabricated drill hole on correct answers (audit 2026-09-29,
    # RAG-3). A candidate with no hole word shortly before it is kept but
    # only ever advisory — see `numeric_far` below.
    _alpha = [hid.upper() for hid in candidates]
    numeric_far: set[str] = set()
    for cand in find_numeric_hole_ids(clean):
        if any(cand.value.upper() in alpha for alpha in _alpha):
            continue
        candidates.append(cand.value)
        if not cand.near_context:
            numeric_far.add(cand.value.upper())
    # A numeric ID seen both near and far is near.
    numeric_far -= {
        c.value.upper() for c in find_numeric_hole_ids(clean) if c.near_context
    }

    hole_ids = [
        hid.upper() for hid in dict.fromkeys(candidates)
        if hid.split("-", 1)[0].upper() not in _CITATION_PREFIX_SET
    ]

    warnings: list[str] = []

    # Tool-result token bag — built once, shared by the hole-ID, commodity,
    # and formation checks below (previously built inside the commodity
    # branch only, which the formation block reached via a fragile F821
    # cross-reference).
    grounded_tokens: set[str] = (
        _extract_entities_from_tool_results(tool_results) if tool_results else set()
    )
    # Holes named anywhere in the evidence, separator-free and upper-cased
    # (so "BH-12", "BH12" and "bh 12" are one hole — RAG-16).
    evidence_holes: set[str] = (
        _evidence_hole_ids(tool_results) if tool_results else set()
    )

    # --- Hole ID resolution via PostGIS ---
    if hole_ids:
        try:
            canon_ids = [canonical_hole_id(h) for h in hole_ids]
            # ONE TIMEOUT_POSTGIS_S for the whole lookup, ``pool.acquire()``
            # included (AL-10). Only the fetch used to be bounded; with every
            # connection checked out the acquire is the call that waits, so an
            # exhausted pool hung validation -- and the user's stream with
            # it -- with no timeout at all. A TimeoutError lands in the
            # fail-closed handler below, like any other lookup failure.
            async with asyncio.timeout(settings.TIMEOUT_POSTGIS_S), pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    # Case- and separator-insensitive (RAG-16): a hole
                    # stored "Gh08-212" or "BH12" is the hole the answer
                    # calls "GH08-212" / "BH-12". hole_id_canonical is
                    # the ingest-side normal form (same rule as
                    # canonical_hole_id); the regexp_replace arm covers
                    # rows ingested before that column was populated.
                    "SELECT hole_id, hole_id_canonical FROM silver.collars "
                    "WHERE project_id = $2::uuid AND ("
                    "UPPER(hole_id) = ANY($1) "
                    "OR hole_id_canonical = ANY($3) "
                    "OR regexp_replace(UPPER(hole_id), '[[:space:]_./-]+', '', 'g') = ANY($3))",
                    hole_ids,
                    project_id,
                    canon_ids,
                )
            # The SQL above fetches CANDIDATES by the separator-free canonical
            # form (that is what silver.collars.hole_id_canonical holds), which
            # merges "PLS-2-28" with "PLS-22-8". The match itself is confirmed
            # on hole_id_key, which keeps the separator between digit groups,
            # so a fabricated hole no longer passes as a real neighbour
            # (audit item 24).
            found: set[str] = {hole_id_key(r["hole_id"]) for r in rows}
            for hid in hole_ids:
                key = hole_id_key(hid)
                in_evidence = key in evidence_holes or hid.lower() in grounded_tokens
                if key in found:
                    # Exists in the project — but is it the hole the
                    # EVIDENCE is about? An answer that moves BH-12's
                    # intercept onto real hole BH-21 used to pass silently
                    # (RAG-16).
                    if tool_results and not in_evidence:
                        warnings.append(_not_in_evidence_warning(hid, clean, hole_ids))
                    continue
                if hid in numeric_far:
                    warnings.append(
                        f"Layer 4 advisory: '{hid}' has the shape of a numeric "
                        f"hole ID but no hole reference governs it and it is "
                        f"not in silver.collars — not treated as a hole claim"
                    )
                    continue
                # RAG-quality audit 2026-08-14 (finding 3, the "ZRY" case):
                # a hole named verbatim in retrieved document chunks but
                # absent from silver.collars is NOT a fabrication — the
                # structured drill database simply doesn't cover it. Only a
                # hole absent from BOTH the DB and the retrieved evidence
                # stays critical (the prefix "Layer 4: Drill-hole ID" is
                # what run_post_assembly_validation classifies as critical;
                # the advisory prefix is deliberately different).
                if in_evidence:
                    warnings.append(
                        f"Layer 4 advisory: Hole '{hid}' is not in the "
                        f"structured drill database (silver.collars) for "
                        f"this project; the answer is grounded in retrieved "
                        f"documents instead"
                    )
                else:
                    warnings.append(
                        f"Layer 4: Drill-hole ID '{hid}' not found in silver.collars "
                        f"for this project"
                    )
        except Exception:
            # FAIL CLOSED. This used to swallow at DEBUG and return as though
            # every hole ID had resolved, which quietly undid the 2026-08-15
            # fix one level above: `validate_node` wraps
            # run_post_assembly_validation in a fail-closed handler, but this
            # except sits BELOW that call, so nothing ever reached it.
            #
            # The cost of the old behaviour was specific. This is the ONE
            # warning the severity classifier treats as critical on its own
            # (see the header comment on this section, and the
            # `startswith("Layer 4: Drill-hole ID")` bucket in
            # run_post_assembly_validation) — so a fabricated hole ID that
            # coincided with a PgBouncer blip or a PostGIS timeout shipped at
            # whatever confidence retrieval produced, unflagged.
            #
            # The warning below carries that same critical prefix, which is
            # what escalates it, but it does NOT claim any particular hole is
            # missing — only that fabrication could not be ruled out. That
            # distinction matters: the answer may be perfectly sound, and the
            # banner the caller sees should say "unverified", not "wrong".
            #
            # Deliberately still inside the try, so the commodity and
            # formation checks below still run. Re-raising would hand the
            # whole response to the outer handler and lose them for an error
            # that only invalidates this one check.
            logger.warning(
                "orchestrator_validators: hole-ID entity resolution failed for "
                "%d hole ID(s) — failing CLOSED, escalating as unverified",
                len(hole_ids),
                exc_info=True,
            )
            warnings.append(
                f"Layer 4: Drill-hole ID resolution could not complete for "
                f"{len(hole_ids)} hole ID(s) mentioned in this answer — "
                f"silver.collars was unreachable, so fabricated hole IDs "
                f"could not be ruled out. This answer is UNVERIFIED on that "
                f"check, not confirmed clean."
            )

    # --- Commodity codes: must appear in tool results ---
    if tool_results:
        # Find bare commodity tokens in the answer text.
        commodity_pattern = re.compile(
            r"\b(" + "|".join(re.escape(c) for c in sorted(_COMMODITY_CODES, key=len, reverse=True)) + r")\b"
        )
        cited_commodities = [m.group(1) for m in commodity_pattern.finditer(clean)]
        cited_commodities = list(dict.fromkeys(cited_commodities))
        for commodity in cited_commodities:
            if not _commodity_grounded(commodity, grounded_tokens):
                warnings.append(
                    f"Layer 4: Commodity '{commodity}' mentioned but not found "
                    f"in any tool result — verify this appears in cited evidence"
                )

    # --- Formation / lithology check via Neo4j (fail-open, cached) ---
    # Module 6 Chunk 3.5: formation set is fetched once per 5-minute window via
    # _get_known_formations() and cached in _FORMATION_CACHE keyed by project_id.
    # First call pays the Neo4j round-trip (~200-500 ms); subsequent calls within
    # the TTL window do an in-process dict lookup, reducing entity guard wall-time
    # from ~30 s (sequential Neo4j round-trip per query) to ~1 s (regex match only).
    #
    # Phase F.6+ Layer 4 tolerance fix: extraction now skips English stopwords
    # ("This", "That", "Knowledge", "Graph", …) and geographic proper nouns
    # ("Wyoming", "Saskatchewan", …) at the regex level — they aren't
    # formations and were producing pure noise. Compound TitleCase names
    # ("Cameco Shirley Basin Uranium") are accepted when each non-stopword
    # word is grounded in formations OR tool_results, even if the literal
    # compound isn't a Formation node.
    proper_nouns = list(dict.fromkeys(
        m.group(1) for m in _TITLE_CASE_RE.finditer(clean)
        if m.group(1).lower() not in _TITLE_CASE_STOPWORDS
        and m.group(1).lower() not in _GEOGRAPHIC_PROPER_NOUNS
    ))
    if proper_nouns:
        known_formations = await _get_known_formations(
            neo4j_driver, project_id, timeout_s=settings.TIMEOUT_NEO4J_S
        )
        # Token bag was built once at the top of this function.
        tool_tokens = grounded_tokens

        if known_formations:
            # Graph is populated — check each proper noun against the cached
            # set OR the tool-result token bag. Compound names check
            # word-by-word; see `_is_grounded_name`.
            for name in proper_nouns:
                if not _is_grounded_name(name, known_formations, tool_tokens):
                    warnings.append(
                        f"Layer 4: Formation/entity name '{name}' could not be "
                        f"resolved in the Neo4j knowledge graph for this project"
                    )
        # If known_formations is empty, fail-open (no warnings). NOTE: since
        # Neo4j was removed from the stack (B1, 2026-07-28) known_formations
        # is ALWAYS empty in production — the formation/entity branch above
        # is dead code kept only for a future graph-store return; the
        # tool-token grounding it would add is partially covered by the
        # commodity check. See audit 2026-08-14 finding 7.

    return warnings


# ---------------------------------------------------------------------------
# Layer 1 — Retrieval Quality Gate (advisory half, orchestrator version)
#
# The HARD half of Layer 1 (zero-evidence refusal) runs earlier, in
# assemble_node, before the LLM is ever called — see
# app.agent.hallucination.layer1_retrieval.assess_retrieval_quality. This
# function re-runs the same assessment POST-assembly so a "weak" verdict
# (some document chunks cleared the floor, but only marginally, or the
# reranker fell back to RRF/cosine order and returned too few candidates to
# trust) is visible in validation_warnings and GeoRAGResponse.validation_state
# — the same way the Layer 3/6 findings are. Restored 2026-09-24.
# ---------------------------------------------------------------------------


def verify_retrieval_quality(tool_results: list[tuple[str, Any]]) -> list[str]:
    """Layer 1 (advisory half): surface weak-but-passing retrieval.

    Deliberately advisory-only — the "Layer 1:" prefix this emits matches
    none of the severity buckets in run_post_assembly_validation (which key
    off "Layer 3"/"Layer 4:"/"Layer 6:"), so it never sets should_retry on
    its own, matching the completeness guard's posture for a check that has
    not yet been calibrated against a real corpus. The hard refusal case
    (assess_retrieval_quality(...).refuse) is handled separately in
    assemble_node and never reaches this function — a refused query has no
    LLM-generated text to validate.

    Returns a list of at most one warning string. Never raises.
    """
    if not settings.RETRIEVAL_QUALITY_GATE_ENABLED:
        return []

    from app.agent.hallucination.layer1_retrieval import (  # noqa: PLC0415
        assess_retrieval_quality,
    )

    verdict = assess_retrieval_quality(tool_results)
    if verdict.weak and verdict.reason:
        return [verdict.reason]
    return []


# ---------------------------------------------------------------------------
# Layer 6 — Geological Constraints (orchestrator version)
# Delegates to the existing constraint checker which only needs the text.
# ---------------------------------------------------------------------------

def verify_constraints(
    text: str, *, proactive_insights_offset: int | None = None
) -> list[str]:
    """Layer 6: Check geological plausibility of numerical claims.

    Phase F.5: strip the proactive-insights block before constraint checking.
    Anomaly insights are by definition statistical outliers (e.g. "445 m TD
    — 2.2σ deeper than project average of 374 m") and tripping the depth /
    grade ceilings on those numbers is exactly the noise the strip avoids.
    ``proactive_insights_offset`` (normally
    ``response.proactive_insights_offset``) is the structural boundary —
    see ``anomaly_detector.strip_proactive_insights`` for why it must come
    from assembly-time bookkeeping rather than a text search.

    Returns a list of warning strings for constraint violations.
    """
    if not settings.GEOLOGICAL_CONSTRAINTS_ENABLED:
        return []

    from app.agent.anomaly_detector import strip_proactive_insights  # noqa: PLC0415
    text = strip_proactive_insights(text, proactive_insights_offset)

    from app.agent.hallucination.layer6_constraints import _find_violations

    violations = _find_violations(text)
    warnings = []
    for v in violations:
        warnings.append(
            f"Layer 6: Value {v.value} violates constraint "
            f"'{v.constraint.name}' ({v.constraint.unit_hint}) — "
            f"context: '{v.context_snippet}'"
        )

    return warnings


# ---------------------------------------------------------------------------
# Completeness guard — every declarative sentence must carry a citation.
#
# Ported here 2026-08-21 from the deleted layer_completeness.py, which held
# the only implementation of this guard AND the only guard-tolerance model.
# Neither ever ran: layer_completeness.evaluate_guards had no production
# caller, so despite being named as a control in CLAUDE.md hard rule 5, the
# completeness promise the system prompt makes to the model ("every factual
# claim MUST include an inline citation marker") was never verified post-hoc.
# It is verified here, on the live path, as of this port.
#
# Architecture reference: §04i Global Invariant 1; spec
# 06-citation-hallucination-guards.md §6 B2.
#
# Design (carried over unchanged): no NER and no nltk dependency — sentence
# splitting and marker detection are regex-based, and the exemption lists
# below are a small fixed vocabulary rather than a model.
# ---------------------------------------------------------------------------

# Sentences come from claim_sentences.split_units (shared with Layers 2 and
# 5 since 2026-09-29): it does not break on "approx." / "Fig." / "e.g."
# (RISK-4) and folds a trailing marker-only fragment onto its sentence.
# Still regex-based: no nltk dep, no spacy dep.

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


def _is_exempt(sentence: str) -> bool:
    """Return True if the sentence is exempt from the completeness guard.

    Exempt sentences:
      - Questions (end with ?)
      - Refusal phrases (no facts to cite)
      - Imperative / transitional starters ("See Table 3...")
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
    return bool(ALL_MARKER_RE.search(sentence))


def verify_completeness(
    answer_text: str, *, proactive_insights_offset: int | None = None
) -> list[str]:
    """Every declarative sentence must have a citation marker.

    Per spec B2: split the answer into sentences; each declarative sentence
    must have at least one citation marker within it OR at the start of the
    immediately following sentence.  A bare-assertion sentence is flagged.

    The proactive-insights block is stripped before sentence-splitting.
    Insight bullets are deterministic system output (computed from raw
    tool_results data), not part of the LLM's surface — this guard is only
    meant to catch *LLM* bare assertions.  ``proactive_insights_offset`` is
    the structural boundary recorded at assembly time by
    ``anomaly_detector.append_insights_block``; see
    ``anomaly_detector.strip_proactive_insights`` for why it must come from
    assembly-time bookkeeping rather than a text search.

    Args:
        answer_text: The LLM answer text (normalized, post-dash-rewrite).
        proactive_insights_offset: Boundary recorded at assembly time, or
            None if no insights block was appended to this response.

    Returns:
        A list of human-readable warning strings, one per uncited declarative
        sentence, each prefixed ``"Completeness: "``.  Empty list means every
        declarative sentence is cited.

    Note:
        The ``"Completeness: "`` prefix deliberately matches none of the
        severity buckets in :func:`run_post_assembly_validation` (which key
        off ``"Layer 3"`` / ``"Layer 4:"`` / ``"Layer 6:"``), so these
        warnings are advisory and never on their own trigger an LLM retry.
        See the tolerance note in that function.
    """
    from app.agent.anomaly_detector import strip_proactive_insights  # noqa: PLC0415
    from app.agent.hallucination.claim_sentences import (  # noqa: PLC0415
        is_non_claim,
        split_units,
    )
    from app.agent.hallucination.layer2_typed_output import (  # noqa: PLC0415
        _is_system_text,
    )
    from app.agent.response_assembler import _is_refusal  # noqa: PLC0415

    answer_text = strip_proactive_insights(answer_text, proactive_insights_offset)

    # A refusal makes no claims (RAG-21). The Layer 1 refusal is three
    # sentences, none of which matched the exemption list, so every refused
    # query was persisted with three "uncited declarative sentence" findings
    # and rendered as validation_state="flagged". A refusal that DOES carry
    # markers is a qualified answer and is still checked.
    if _is_system_text(answer_text) or (
        _is_refusal(answer_text) and not ALL_MARKER_RE.search(answer_text)
    ):
        return []

    sentences = [u.text.strip() for u in split_units(answer_text) if u.text.strip()]

    uncited: list[str] = []

    for i, sentence in enumerate(sentences):
        # Skip exempt sentences.
        if _is_exempt(sentence) or is_non_claim(sentence):
            continue

        # Does this sentence contain a marker?
        if _has_marker(sentence):
            continue

        # Does the next sentence open with a marker?
        if i + 1 < len(sentences):
            next_sent = sentences[i + 1].strip()
            if ALL_MARKER_RE.match(next_sent) or _has_marker(next_sent[:40]):
                # Next sentence provides the citation for this one — OK.
                continue

        # No marker in this sentence or the next — bare assertion.
        uncited.append(sentence[:200])  # truncate for storage

    if uncited:
        logger.warning(
            "verify_completeness: %d uncited declarative sentence(s) found",
            len(uncited),
        )
    else:
        logger.debug("verify_completeness: all declarative sentences have citations")

    return [f"Completeness: uncited declarative sentence: {s}" for s in uncited]


# ---------------------------------------------------------------------------
# Guard tolerances
#
# Ported from layer_completeness.evaluate_guards (Doc-phase 186 + Eval 01 P3).
# The strict "any failure -> reject" posture produces false positives on noisy
# or fragmented retrieval contexts, so each guard gets a budget of soft
# failures.  Different query classes have different evidence shapes:
#   exploratory   -> coverage is sparse by design; loosen completeness
#   computational -> numbers are derived (avg, sum); loosen numeric
#   factual       -> tighten everything; a fact must be cited
# The GUARD_TOLERANCE_* settings are the global defaults; the per-class table
# below additively overrides them (max, never a reduction).  Unknown or absent
# classes fall back to the globals.
# ---------------------------------------------------------------------------

_PER_CLASS_TOLERANCE_OVERRIDES: dict[str, dict[str, int]] = {
    "factual":       {"numeric": 0, "entity": 0, "completeness": 0},
    "computational": {"numeric": 3, "entity": 0, "completeness": 1},
    "exploratory":   {"numeric": 1, "entity": 1, "completeness": 3},
    "comparison":    {"numeric": 1, "entity": 0, "completeness": 1},
    "trend":         {"numeric": 2, "entity": 1, "completeness": 2},
}


def guard_tolerances(query_class: str | None = None) -> dict[str, int]:
    """Return the per-guard soft-failure budget for a query class.

    Args:
        query_class: One of ``factual``, ``computational``, ``exploratory``,
            ``comparison``, ``trend``, or None/unknown for the global
            defaults.

    Returns:
        ``{"numeric": int, "entity": int, "completeness": int}`` — the number
        of failures each guard tolerates before the finding is material.
    """
    tolerances = {
        "numeric": int(getattr(settings, "GUARD_TOLERANCE_NUMERIC_UNGROUNDED", 0)),
        "entity": int(getattr(settings, "GUARD_TOLERANCE_ENTITY_UNRESOLVED", 0)),
        "completeness": int(
            getattr(settings, "GUARD_TOLERANCE_COMPLETENESS_UNCITED", 0)
        ),
    }

    # NOTE BEFORE CHANGING THIS, and before "fixing" the fact that
    # validate_node does not pass query_class at all.
    #
    # The combination is `max`, so an override can only ever LOOSEN. Against
    # the shipped globals (GUARD_TOLERANCE_* = 2/2/2, config.py) the table
    # above resolves to:
    #
    #   None           numeric 2  entity 2  completeness 2
    #   factual        numeric 2  entity 2  completeness 2   <- identical
    #   computational  numeric 3  entity 2  completeness 2   <- looser
    #   exploratory    numeric 2  entity 2  completeness 3   <- looser
    #   comparison     numeric 2  entity 2  completeness 2   <- identical
    #   trend          numeric 2  entity 2  completeness 2   <- identical
    #
    # Every 0 in the table is dominated. So threading query_class through
    # from validate_node -- which reads like an obvious one-line omission,
    # and was reported to me as "restores the intended factual strictness"
    # -- would tighten nothing and loosen two guards. CLAUDE.md rule 5 is
    # explicit that weakening the four is not welcome, and this is the
    # shape of change that does it while looking like the opposite.
    #
    # The table's intent (factual tolerates ZERO uncited sentences) needs
    # `override` to win outright rather than `max`, which is a real change
    # to refusal behaviour and wants a corpus to measure against before it
    # ships. Left as-is deliberately; not left as-is silently.
    override = _PER_CLASS_TOLERANCE_OVERRIDES.get(query_class or "")
    if override is not None:
        tolerances = {k: max(tolerances[k], override[k]) for k in tolerances}
        logger.info(
            "guard_tolerances: per-class tolerances active (class=%s, "
            "numeric=%d entity=%d completeness=%d)",
            query_class,
            tolerances["numeric"],
            tolerances["entity"],
            tolerances["completeness"],
        )

    return tolerances


# ---------------------------------------------------------------------------
# Unified validation runner
# ---------------------------------------------------------------------------

#: Every prefix the Layer 3 guards emit.
#:
#: Two guards, two shapes: the numeric guard writes "Layer 3: Ungrounded
#: number ..." and the unit-pair guard writes "Layer 3 tuple: value 5.2
#: reported as 'ppm' ..." — a space where the other has a colon.
#:
#: Declared here, next to the code that builds the strings, because two
#: separate readers bucket them: the severity classifier below (retry and
#: flooring) and confidence_computer._is_layer3_warning (demotion). Both
#: matched "Layer 3:" and so ignored every unit-pair warning, which is how
#: the 2026-08-14 shadow→warn promotion came to have no effect on either.
#:
#: A tuple rather than `startswith("Layer 3")`: the loose form also matches
#: a "Layer 30:" that nobody has written yet, and a guard against
#: fabricated numbers should not itself be approximately right. It also
#: matches the advisory "Layer 3 advisory: number ..." finding, which must
#: NOT trigger a retry or the demotion (LAYER3_CITED_ELSEWHERE_PREFIX).
LAYER3_WARNING_PREFIXES: tuple[str, ...] = ("Layer 3:", "Layer 3 tuple:")


def _severity_buckets(
    all_warnings: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """``(critical, high, advisory)`` for a run's warnings.

    Shared by :func:`run_post_assembly_validation` (which sets
    ``should_retry`` from them) and :func:`retry_trigger_warnings` (which
    tells the caller WHICH warning did, so the answer's banner names the real
    reason rather than whichever advisory happened to come first).
    """
    _layer4 = [w for w in all_warnings if w.startswith("Layer 4:")]
    critical = [w for w in _layer4 if w.startswith("Layer 4: Drill-hole ID")]
    _layer4_advisory = [
        w for w in _layer4 if not w.startswith("Layer 4: Drill-hole ID")
    ]
    _LAYER4_ADVISORY_CRITICAL_THRESHOLD = 3
    if len(_layer4_advisory) >= _LAYER4_ADVISORY_CRITICAL_THRESHOLD:
        critical = critical + _layer4_advisory
        logger.warning(
            "post_assembly_validation: %d advisory Layer 4 warning(s) "
            "(threshold=%d) — the density signals fabrication, escalating "
            "to critical.",
            len(_layer4_advisory), _LAYER4_ADVISORY_CRITICAL_THRESHOLD,
        )
    high = [w for w in all_warnings if w.startswith("Layer 6:")]
    # Both Layer 3 prefixes, from the shared tuple. The numeric guard emits
    # "Layer 3: ..." and the unit-pair guard emits "Layer 3 tuple: ..." —
    # a space, not a colon. Matching on "Layer 3:" excluded every tuple
    # warning from this bucket, so the 2026-08-14 shadow->warn promotion
    # had no effect at all: unit-pair mismatches never counted toward
    # the count threshold that then gated retries, never set should_retry, and
    # were not even included in the advisory=%d figure logged below.
    # The per-sentence "cited elsewhere" finding is not in this bucket by
    # construction: its prefix ("Layer 3 advisory:") is not in
    # LAYER3_WARNING_PREFIXES.
    advisory = [w for w in all_warnings if w.startswith(LAYER3_WARNING_PREFIXES)]

    return critical, high, advisory


def retry_trigger_warnings(all_warnings: list[str]) -> list[str]:
    """The warnings that, on their own, force ``should_retry``.

    Critical (fabricated hole id, or Layer 4 density), high (Layer 6) and any
    Layer 3 finding except the per-sentence advisory. Excludes Layer 1's weak
    retrieval and the completeness guard, which are advisory by design.
    """
    critical, high, advisory = _severity_buckets(all_warnings)
    return [*critical, *high, *advisory]


async def run_post_assembly_validation(
    response: GeoRAGResponse,
    tool_results: list[tuple[str, Any]],
    deps: AgentDeps,
    *,
    query_class: str | None = None,
) -> tuple[GeoRAGResponse, list[str], bool]:
    """Run all orchestrator-compatible validators on an assembled response
    (Layer 1 advisory, Layer 3, Layer 4, Layer 6, plus the completeness
    guard).

    Args:
        response: The assembled response to validate (never mutated).
        tool_results: Tool results from the orchestrator fan-out.
        deps: Agent dependencies (project id, pg pool, neo4j driver).
        query_class: Optional query class (``factual``, ``computational``,
            ``exploratory``, ``comparison``, ``trend``) used to select the
            per-class guard tolerances.  None means the global defaults.

    Returns:
        (response, warnings, should_retry) — response is unchanged,
        warnings is a list of human-readable strings, should_retry is True
        if critical/high-severity issues were found (fabricated entities,
        geological constraint violations) warranting an LLM retry.
    """
    all_warnings: list[str] = []
    tolerances = guard_tolerances(query_class)

    # Security fix (2026-08-15): the proactive-insights boundary is read
    # from the structured field the assembler recorded, not re-derived by
    # searching response.text for the header string — see
    # anomaly_detector.strip_proactive_insights for why a text search is
    # unsafe (the LLM's own output could reproduce the header and hide
    # fabricated content from every guard below).
    _insights_offset = response.proactive_insights_offset

    # Layer 1 — retrieval quality gate (advisory half; the hard-refuse half
    # already ran in assemble_node before this response was ever built).
    all_warnings.extend(verify_retrieval_quality(tool_results))

    # Layer 3 — numerical grounding
    all_warnings.extend(
        verify_numbers(
            response.text,
            tool_results,
            proactive_insights_offset=_insights_offset,
        )
    )

    # Layer 3, per-sentence advisory half (audit item 5b): a number must be
    # in the evidence of the id its sentence cites, not merely in SOME
    # retrieved chunk. Never raises -- a failure here must not hide the
    # whole-answer check above.
    try:
        all_warnings.extend(
            verify_cited_number_support(
                response.text,
                tool_results,
                proactive_insights_offset=_insights_offset,
            )
        )
    except Exception:
        logger.warning("Layer 3 per-sentence number check failed", exc_info=True)

    # Layer 4 — entity resolution (async — needs database)
    # Pass tool_results so commodity-code grounding can verify against cited evidence.
    entity_warnings = await verify_entities(
        response.text,
        deps.project_id,
        deps.pg_pool,
        deps.neo4j_driver,
        tool_results=tool_results,
        proactive_insights_offset=_insights_offset,
    )
    all_warnings.extend(entity_warnings)

    # Layer 6 — geological constraints
    all_warnings.extend(
        verify_constraints(response.text, proactive_insights_offset=_insights_offset)
    )

    # Completeness — every declarative sentence must carry a citation marker.
    #
    # Advisory by construction: the "Completeness: " prefix matches none of
    # the severity buckets below, so these warnings surface in the returned
    # list and the logs but never set should_retry on their own. That is
    # deliberate for the first release of this guard on the live path — it
    # has never run against production answers, so its false-positive rate
    # on real corpora is unmeasured. Promoting it to a retry trigger is a
    # calibration decision, not a code change: add "Completeness:" to a
    # severity bucket once the warning rate has been observed.
    #
    # Since 2026-09-29 the ENFORCING half of this rule runs earlier, in
    # validate_node (layer2_typed_output.enforce_claim_citations): uncited
    # claim sentences are removed before this function sees the text, so on
    # the live path this check is a backstop. Findings inside the tolerance
    # are no longer thrown away — they still reach the warnings list, so the
    # answer is "flagged" rather than "clean" (RAG-7c); the tolerance only
    # governs the log line.
    completeness_warnings = verify_completeness(
        response.text, proactive_insights_offset=_insights_offset
    )
    if completeness_warnings and len(completeness_warnings) <= tolerances["completeness"]:
        logger.info(
            "post_assembly_validation: completeness guard within "
            "tolerance — %d uncited sentence(s) <= tolerance=%d (still "
            "reported; flags the answer)",
            len(completeness_warnings),
            tolerances["completeness"],
        )
    all_warnings.extend(completeness_warnings)

    # NOTE on the numeric/entity tolerances.
    #
    # ``tolerances["numeric"]`` and ``tolerances["entity"]`` are computed
    # above and deliberately NOT applied to the severity classification
    # below. They come from GUARD_TOLERANCE_NUMERIC_UNGROUNDED /
    # GUARD_TOLERANCE_ENTITY_UNRESOLVED, which default to 2, and applying
    # them here would loosen fabrication detection: ANY Layer 3 finding now
    # forces a retry (see should_retry below), so damping the count by 2
    # first would let one or two fabricated numbers ship unflagged. That is a
    # live safety-posture change and needs its own calibration run against
    # the golden set. The tolerances are surfaced here (and covered by
    # tests) so the model is available to whoever makes that call.

    # Classify warnings by severity — fabricated drill-hole IDs are
    # critical, constraints are high, and every Layer 3 numerical-grounding
    # finding is a retry trigger on its own (below). The other Layer 4
    # warnings (commodity / formation / entity grounding) come from heuristic
    # token-bag checks with a real false-positive rate, so they escalate to
    # critical only in bulk (_LAYER4_ADVISORY_CRITICAL_THRESHOLD). They
    # remain in all_warnings either way.
    critical, high, advisory = _severity_buckets(all_warnings)

    if all_warnings:
        logger.warning(
            "post_assembly_validation: %d warning(s) "
            "(critical=%d, high=%d, advisory=%d):\n  %s",
            len(all_warnings),
            len(critical),
            len(high),
            len(advisory),
            "\n  ".join(all_warnings),
        )

    # Mark whether a retry is recommended — the orchestrator checks this
    # flag to decide whether to re-call the LLM.
    #
    # Audit item 5a (2026-10-04): ANY Layer 3 finding sets it. An ungrounded
    # number used to cost only the x0.7 demotion unless several piled up (a
    # count threshold, removed 2026-10-04 as inert once this rule landed) --
    # no floor, no banner, the text untouched -- so one or two fabricated
    # grades shipped looking like a normal cited answer. Same treatment as a
    # Layer 4 / Layer 6 finding. The per-sentence advisory
    # (LAYER3_CITED_ELSEWHERE_PREFIX) stays out of this: it is not in
    # `advisory`.
    should_retry = len(critical) > 0 or len(high) > 0 or len(advisory) > 0

    return response, all_warnings, should_retry
