"""Orchestrator-compatible hallucination validators.

This module IS the live post-assembly validation path. It holds Layers 3
(numerical grounding), 4 (entity resolution), 6 (geological constraints)
and the completeness guard, all working against the deterministic
orchestrator's tool_results list rather than Pydantic AI's ctx.messages.

History (2026-08-21): the Pydantic-AI-shaped originals — layer3_numerical.py,
layer4_entity.py, layer1_retrieval.py and layer_completeness.py — were
deleted. They had accumulated no production callers: the orchestrator never
adopted the output_validator decorator pattern they were built for, so they
sat alongside this module looking like controls that were in force while
only this one ever executed. The completeness guard and the guard-tolerance
model were the only logic unique to them and were ported here; the rest was
a second, unreachable implementation of what verify_numbers and
verify_entities already do. Layers 2, 5 and 6 remain in their own modules
and are wired into agentic_retrieval/nodes.py.

Usage in orchestrator:
    from app.agent.hallucination.orchestrator_validators import run_post_assembly_validation
    response, warnings, should_retry = await run_post_assembly_validation(
        response, tool_results, deps
    )
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from typing import Any

from app.agent.deps import AgentDeps
from app.agent.hallucination.citation_markers import (
    ALL_MARKER_RE,
    CITATION_MARKER_RE,
    CITATION_PREFIXES,
)
from app.agent.hole_id_patterns import (
    HOLE_CONTEXT_RE,
    HOLE_ID_RE,
    NUMERIC_HOLE_ID_RE,
)
from app.config import settings
from app.models.rag import GeoRAGResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module 6 Chunk 3.5 — Formation name cache (per-process, TTL-based)
#
# The entity guard's Neo4j Formation-name lookup is the single most expensive
# guard operation (~200-500 ms on a warm Neo4j, dominating guard runtime when
# sequential).  Since Formation nodes change only on Dagster ingestion runs
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

_NUMBER_RE = re.compile(r"[-+]?\d+\.?\d*")

#: Drill-hole and sample identifiers, removed before any number extraction.
#:
#: `_NUMBER_RE` has no idea what a hole ID is, so "PLS-22-08" yielded the two
#: numbers **-22.0 and -8.0** — the hyphens read as minus signs. Both sides of
#: Layer 3 did this: the response text, and `_collect_grounded_numbers`, which
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
    r"([-+]?\d+\.?\d*)\s*"
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
    clean = _CITATION_MARKER_RE.sub("", text)
    out: list[tuple[float, str]] = []
    for match in _NUMBER_WITH_UNIT_RE.finditer(clean):
        try:
            val = float(match.group(1))
            unit = match.group(2).lower()
            if val not in _SMALL_NUMBERS:
                out.append((val, unit))
        except ValueError:
            continue
    return out


def _collect_grounded_tuples(
    tool_results: list[tuple[str, Any]],
) -> list[tuple[float, str]]:
    """Same shape as _extract_number_unit_tuples but over tool_results JSON."""
    out: list[tuple[float, str]] = []
    for _tool_name, result in tool_results:
        try:
            if hasattr(result, "model_dump"):
                text = json.dumps(result.model_dump(), default=str)
            elif hasattr(result, "__dict__"):
                text = json.dumps(result.__dict__, default=str)
            else:
                text = str(result)
            out.extend(_extract_number_unit_tuples(text))
        except Exception:
            continue
    return out


#: How far from a grounded value a derived statistic may sit and still be
#: treated as derived. Half to double covers a mean, a median, any
#: percentile and a rounded restatement; it does not cover a factor-of-ten
#: transcription error, which is the mistake worth catching.
_DERIVATION_SCALE_LOW = 0.5
_DERIVATION_SCALE_HIGH = 2.0


def _is_same_order_as_any(num: float, grounded: list[float]) -> bool:
    """Is ``num`` the scale of at least one grounded value?

    Compares magnitudes, so a negative dip of -55 is judged against the 60
    in the evidence rather than against the whole numeric span of the
    payload. Zero is only ever derived from zero.
    """
    target = abs(num)

    if target == 0.0:
        return any(g == 0.0 for g in grounded)

    return any(
        _DERIVATION_SCALE_LOW * abs(g) <= target <= _DERIVATION_SCALE_HIGH * abs(g)
        for g in grounded
        if g != 0.0
    )


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
        candidates = [
            (g, unit_g) for (g, unit_g) in grounded_tuples
            if abs(g - v) < 0.1
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


def _extract_numbers_from_text(text: str) -> list[float]:
    """Extract all numbers from response text.

    Citation markers and drill-hole identifiers are removed first — neither
    is a numerical claim, and both parse as one. See `_IDENTIFIER_TOKEN_RE`.
    """
    clean = _IDENTIFIER_TOKEN_RE.sub(" ", _CITATION_MARKER_RE.sub("", text))
    numbers = []
    for match in _NUMBER_RE.finditer(clean):
        try:
            val = float(match.group())
            if val not in _SMALL_NUMBERS:
                numbers.append(val)
        except ValueError:
            continue
    return numbers


def _collect_grounded_numbers(tool_results: list[tuple[str, Any]]) -> set[float]:
    """Collect all numbers from tool results for grounding verification.

    Hole IDs are stripped here too, and this is the half that mattered: the
    response mentions a handful of holes, the serialised evidence carries one
    `hole_id` per collar row. See `_IDENTIFIER_TOKEN_RE` for what those false
    numbers did to the derivation tolerance.
    """
    grounded: set[float] = set()

    for _tool_name, result in tool_results:
        # Serialize the result to JSON and extract all numbers
        try:
            if hasattr(result, '__dict__'):
                text = json.dumps(result.__dict__, default=str)
            elif hasattr(result, 'model_dump'):
                text = json.dumps(result.model_dump(), default=str)
            else:
                text = str(result)

            text = _IDENTIFIER_TOKEN_RE.sub(" ", text)
            for match in _NUMBER_RE.finditer(text):
                try:
                    grounded.add(float(match.group()))
                except ValueError:
                    continue
        except Exception:
            continue

    return grounded


def _expand_grounded_with_conversions(grounded: set[float]) -> set[float]:
    """Expand the grounded set with all valid unit-conversion derivatives.

    V1 conversions in scope (per Module 6 spec B2 scope gate):
      ppm  ↔ %         divide/multiply by 10 000
      g/t  ↔ oz/t      divide/multiply by 31.1035
      m    ↔ ft        divide/multiply by 3.28084

    For each grounded value we add both directions of every conversion.
    This lets the guard accept "1.2 oz/t" when the tool returned "37.3 g/t"
    (37.3 / 31.1035 ≈ 1.20).  The tolerance in _is_grounded_strict() handles
    floating-point rounding.
    """
    expanded: set[float] = set(grounded)
    for g in grounded:
        if abs(g) < 1e9:  # skip sentinel values
            # ppm ↔ %
            expanded.add(g / 10_000.0)
            expanded.add(g * 10_000.0)
            # g/t ↔ oz/t
            expanded.add(g / 31.1035)
            expanded.add(g * 31.1035)
            # m ↔ ft
            expanded.add(g / 3.28084)
            expanded.add(g * 3.28084)
    # Also add integer and one/two-decimal-place variants of all originals.
    extras = set()
    for v in expanded:
        if abs(v) < 1e9:
            extras.add(round(v, 1))
            extras.add(round(v, 2))
            with contextlib.suppress(OverflowError, ValueError):
                extras.add(float(int(v)))
    expanded |= extras
    return expanded


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

    raw_grounded = _collect_grounded_numbers(tool_results)
    # Expand with unit-conversion derivatives (V1 in-scope conversions).
    grounded = _expand_grounded_with_conversions(raw_grounded)

    # Phase 5 follow-up (2026-05-19) — derivation tolerance.
    # Bare "is X literally grounded?" check misclassifies legitimate
    # computed values (averages, medians, counts, range bounds) as
    # fabrications. The Qwen3-14B smoke matrix rejected an answer of
    # the form "average depth is 375.3 m" because 375.3 was the mean of
    # 66 in-evidence collar depths — not literally in the tool_results
    # but trivially derivable from them.
    #
    # Policy: an "ungrounded" number is allowed if it is plausibly
    # DERIVED from the grounded set — either it matches the count, or
    # it falls inside the [min, max] of grounded values at a comparable
    # scale. Numbers OUTSIDE the evidence range remain flagged
    # (that's the real fabrication failure mode).
    # Audit 2026-06-27: the range/count DERIVATION tolerance below must be based
    # on the RAW grounded evidence values, NOT the unit-conversion-expanded set.
    # A single grounded value (e.g. count=10) expands to ~[0, 100000] via
    # conversions (10% -> 100000 ppm, 10 m -> 10000 mm, …), so using the expanded
    # set as [min,max] made the "inside grounded range" tolerance swallow
    # clearly-fabricated numbers (5000 vs count=10) — effectively disabling
    # Layer 3 whenever any evidence number existed. The literal is_grounded check
    # above still uses the expanded set, so genuine unit conversions still pass.
    grounded_finite = sorted(g for g in raw_grounded if abs(g) < 1e6)
    g_count = len(grounded_finite)

    warnings = []
    for num in response_numbers:
        # Check if number (or close approximation) exists in grounded set.
        # Tolerance 0.1 covers floating-point rounding in unit conversions.
        is_grounded = (
            num in grounded
            or any(abs(num - g) < 0.1 for g in grounded if abs(g) < 1e6)
        )
        if is_grounded:
            continue

        # Derivation tolerance — value plausibly computed from evidence.
        if g_count and abs(num - float(g_count)) < 0.5:
            logger.debug(
                "Layer 3 derivation tolerance: %s ~ count(grounded)=%d",
                num,
                g_count,
            )
            continue
        # An average or median sits between two grounded values of the SAME
        # kind. It does not sit anywhere at all inside [min, max] of every
        # number that appeared in the serialised evidence.
        #
        # That was the previous rule, and _collect_grounded_numbers regexes
        # digit runs straight out of the JSON blob — ISO timestamps, UTM
        # eastings, UUID fragments. One realistic collar row
        # ({"ingested_at": "2026-08-20T14:03:11+00:00", "easting": 512345.7,
        # "relevance_score": 0.82, "page": 12}) yields a grounded range of
        # roughly [-20, 512345.7], so every plausible geological value on
        # earth fell inside it and was accepted as "likely average/median".
        # The guard only ever fired on numbers larger than the biggest
        # coordinate in the payload — that is to say, essentially never for
        # the grades, depths, widths and tonnages it exists to protect.
        #
        # Same order of magnitude as some individual grounded value is the
        # property a derived statistic actually has. A mean of values around
        # 400 is around 400; it is not 4, and it is not 512345.
        if _is_same_order_as_any(num, grounded_finite):
            logger.debug(
                "Layer 3 derivation tolerance: %s is the scale of a grounded "
                "value — likely average/median/percentile",
                num,
            )
            continue

        warnings.append(
            f"Layer 3: Ungrounded number {num} in response — "
            f"not found in any tool result (direct or via unit conversion)"
        )

    # C3: silent-skip threshold REMOVED. Report every ungrounded number.
    if warnings:
        logger.warning(
            "orchestrator_validators: %d ungrounded number(s) detected "
            "(threshold removed per Module 6 Chunk 3 tightening; "
            "derivation tolerance applied — only values outside the "
            "grounded range remain flagged)",
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
    """
    out: list[str] = []
    if isinstance(obj, dict):
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
    candidates = list(_HOLE_ID_RE.findall(clean))
    # Bare numeric IDs (36-1085, the Cameco Shirley Basin shape) only when
    # the answer is actually talking about holes — the same gate viz_builder
    # puts on this pattern, so a depth interval like "20-30 m" is not read
    # as a hole name.
    if _HOLE_CONTEXT_RE.search(clean):
        candidates.extend(_NUMERIC_HOLE_ID_RE.findall(clean))

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

    # --- Hole ID resolution via PostGIS ---
    if hole_ids:
        try:
            async with pg_pool.acquire() as conn:
                rows = await asyncio.wait_for(
                    conn.fetch(
                        "SELECT hole_id FROM silver.collars "
                        "WHERE hole_id = ANY($1) AND project_id = $2::uuid",
                        hole_ids,
                        project_id,
                    ),
                    timeout=settings.TIMEOUT_POSTGIS_S,
                )
            found = {r["hole_id"] for r in rows}
            missing = [hid for hid in hole_ids if hid not in found]
            for hid in missing:
                # RAG-quality audit 2026-08-14 (finding 3, the "ZRY" case):
                # a hole named verbatim in retrieved document chunks but
                # absent from silver.collars is NOT a fabrication — the
                # structured drill database simply doesn't cover it. Check
                # the same tool-result token bag the commodity check uses
                # before escalating. Only a hole absent from BOTH the DB
                # and the retrieved evidence stays critical (the prefix
                # "Layer 4: Drill-hole ID" is what
                # run_post_assembly_validation classifies as critical /
                # confidence-floor-worthy — the advisory prefix below is
                # deliberately different so it never trips that bucket).
                if hid.lower() in grounded_tokens:
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
            logger.debug(
                "orchestrator_validators: hole-ID entity resolution failed (fail-open)"
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

    answer_text = strip_proactive_insights(answer_text, proactive_insights_offset)

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
#: fabricated numbers should not itself be approximately right.
LAYER3_WARNING_PREFIXES: tuple[str, ...] = ("Layer 3:", "Layer 3 tuple:")


async def run_post_assembly_validation(
    response: GeoRAGResponse,
    tool_results: list[tuple[str, Any]],
    deps: AgentDeps,
    *,
    query_class: str | None = None,
) -> tuple[GeoRAGResponse, list[str], bool]:
    """Run all 4 orchestrator-compatible validators on an assembled response.

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

    # Layer 3 — numerical grounding
    all_warnings.extend(
        verify_numbers(
            response.text,
            tool_results,
            proactive_insights_offset=_insights_offset,
        )
    )

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
    completeness_warnings = verify_completeness(
        response.text, proactive_insights_offset=_insights_offset
    )
    if len(completeness_warnings) <= tolerances["completeness"]:
        if completeness_warnings:
            logger.info(
                "post_assembly_validation: completeness guard within "
                "tolerance — %d uncited sentence(s) <= tolerance=%d",
                len(completeness_warnings),
                tolerances["completeness"],
            )
        completeness_warnings = []
    all_warnings.extend(completeness_warnings)

    # NOTE on the numeric/entity tolerances.
    #
    # ``tolerances["numeric"]`` and ``tolerances["entity"]`` are computed
    # above and deliberately NOT applied to the severity classification
    # below. They come from GUARD_TOLERANCE_NUMERIC_UNGROUNDED /
    # GUARD_TOLERANCE_ENTITY_UNRESOLVED, which default to 2, and applying
    # them here would loosen fabrication detection rather than preserve
    # existing behaviour: the Layer 3 escalation already fires at
    # NUMERIC_RETRY_THRESHOLD (3) ungrounded numbers, so damping the count
    # by 2 first would push the effective bar to 5. That is a live
    # safety-posture change and needs its own calibration run against the
    # golden set — it is not a side effect of deleting dead code. The
    # tolerances are surfaced here (and covered by tests) so the model is
    # available to whoever makes that call.

    # Classify warnings by severity — fabricated drill-hole IDs are
    # critical, constraints are high, numerical grounding is advisory
    # UNLESS it crosses a threshold or co-locates with a constraint
    # violation. The other Layer 4 warnings (commodity / formation /
    # entity grounding) come from heuristic token-bag checks with a real
    # false-positive rate, so they escalate to critical only in bulk —
    # mirroring the Layer 3 NUMERIC_RETRY_THRESHOLD policy below. They
    # remain in all_warnings either way.
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
    # NUMERIC_RETRY_THRESHOLD, never set should_retry, and were not even
    # included in the advisory=%d figure logged below.
    advisory = [
        w for w in all_warnings if w.startswith(LAYER3_WARNING_PREFIXES)
    ]

    # Phase H — Layer 3 escalation policy. Per the overnight app review,
    # Layer 3 (numeric_claims) was historically log-only — even when the
    # model emitted 8+ ungrounded numbers in one answer, the run still
    # shipped. The new policy:
    #
    # (a) ≥ NUMERIC_RETRY_THRESHOLD (default 3) ungrounded numbers in
    #     one answer escalates Layer 3 from "advisory" to "high" — the
    #     density signals the model is fabricating, not just rounding.
    # (b) Any Layer 3 number whose value ALSO appears in a Layer 6
    #     constraint violation is critical — the number is BOTH
    #     ungrounded AND violates a physical constraint, which is the
    #     "fabricated impossible value" failure mode that the §04i
    #     contract exists to prevent.
    #
    # Both rules are tunable via settings; safe defaults preserve the
    # current pass rates while raising the retry-on-fabrication bar.
    _numeric_threshold = int(getattr(settings, "NUMERIC_RETRY_THRESHOLD", 3))
    _layer3_escalated_high = False
    if len(advisory) >= _numeric_threshold:
        _layer3_escalated_high = True
        logger.warning(
            "post_assembly_validation: Layer 3 escalated to HIGH — "
            "%d ungrounded number(s) in one answer (threshold=%d). "
            "Triggering retry.",
            len(advisory), _numeric_threshold,
        )

    # Rule (b): co-location with a Layer 6 constraint violation.
    # Both layers carry numeric values in their warning strings; we
    # extract them and check for intersection. Any match elevates the
    # Layer 3 warning to critical (matches "fabricated impossible value"
    # severity).
    _layer3_escalated_critical = False
    if advisory and high:
        import re as _re  # noqa: PLC0415
        _num_re = _re.compile(r"-?\d+(?:\.\d+)?")
        _layer3_nums = set()
        for w in advisory:
            for m in _num_re.findall(w):
                with contextlib.suppress(ValueError):
                    _layer3_nums.add(float(m))
        _layer6_nums = set()
        for w in high:
            for m in _num_re.findall(w):
                with contextlib.suppress(ValueError):
                    _layer6_nums.add(float(m))
        if _layer3_nums & _layer6_nums:
            _layer3_escalated_critical = True
            logger.error(
                "post_assembly_validation: Layer 3 + Layer 6 colocate "
                "on values %s — fabricated impossible value detected. "
                "Triggering retry with critical severity.",
                sorted(_layer3_nums & _layer6_nums),
            )

    if all_warnings:
        logger.warning(
            "post_assembly_validation: %d warning(s) "
            "(critical=%d, high=%d, advisory=%d, "
            "L3_escalated_high=%s, L3_escalated_critical=%s):\n  %s",
            len(all_warnings),
            len(critical),
            len(high),
            len(advisory),
            _layer3_escalated_high,
            _layer3_escalated_critical,
            "\n  ".join(all_warnings),
        )

    # Mark whether a retry is recommended — the orchestrator checks this
    # flag to decide whether to re-call the LLM.
    should_retry = (
        len(critical) > 0
        or len(high) > 0
        or _layer3_escalated_high
        or _layer3_escalated_critical
    )

    return response, all_warnings, should_retry
