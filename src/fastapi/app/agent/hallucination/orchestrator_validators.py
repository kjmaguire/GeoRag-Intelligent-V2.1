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

import contextlib
import dataclasses
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
from app.agent.hole_id_patterns import (
    DESIGNATION_RE,
    HOLE_CONTEXT_RE,
    HOLE_ID_RE,
    NUMERIC_HOLE_ID_RE,
    canonical_hole_id,
    find_numeric_hole_ids,
    hole_id_key,
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
    # A collar's stated position uncertainty is metadata about the position,
    # not a value a claim may be grounded on (crs_confidence is already
    # excluded by _NON_CONTENT_KEY_RE). GIS audit 2026-10.
    "spatial_uncertainty_m",
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
#: literally say (after rounding and unit conversion). The 2x "derived
#: statistic" window is for structured rows -- a mean of collar depths is
#: near some collar depth -- and applying it to every number in 5,000
#: characters of report text is what let a fabricated grade pass whenever
#: the chunk happened to contain any value of the same magnitude.
_DOCUMENT_TOOL_NAMES: frozenset[str] = frozenset((
    "search_documents", "search_documents_adversarial", "search_public_geoscience",
))


@dataclasses.dataclass
class _Evidence:
    literal: set[float] = dataclasses.field(default_factory=set)
    derivable: list[float] = dataclasses.field(default_factory=list)
    identifiers: set[str] = dataclasses.field(default_factory=set)


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


def _numbers_in(text: str) -> list[float]:
    text = DESIGNATION_RE.sub(" ", text)
    text = _IDENTIFIER_TOKEN_RE.sub(" ", HOLE_ID_RE.sub(" ", text))
    out: list[float] = []
    for m in _NUMBER_RE.finditer(text):
        with contextlib.suppress(ValueError):
            out.append(_parse_number(m.group()))
    return out


def _walk_evidence(
    obj: Any,
    ev: _Evidence,
    *,
    structured: bool,
    key: str = "",
    sample_sizes: bool = True,
) -> None:
    """Collect content numbers from ``obj``, skipping non-content keys.

    ``sample_sizes=False`` stops the length of a list -- and a ``count``
    field -- from counting as a number the answer may state. It is set for a
    result that reports its own ``total_count`` (a LIMIT-capped sample): the
    sample size is not a fact about the project, and offering it as one is
    how "50 holes" got grounded on a 567-hole project (audit item 3).
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
        for f in dataclasses.fields(obj):
            if reports_total and f.name == "count":
                continue  # rows returned, not rows matched
            _walk_evidence(
                getattr(obj, f.name), ev, structured=structured, key=f.name,
                sample_sizes=sample_sizes and not reports_total,
            )
    elif isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            _walk_evidence(
                getattr(obj, name), ev, structured=structured, key=name,
                sample_sizes=sample_sizes,
            )
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _walk_evidence(
                v, ev, structured=structured, key=str(k), sample_sizes=sample_sizes
            )
    elif isinstance(obj, (list, tuple, set, frozenset)):
        if structured and sample_sizes:
            # A row count is a number the answer may state ("12 samples").
            ev.literal.add(float(len(obj)))
        for v in obj:
            _walk_evidence(
                v, ev, structured=structured, key=key, sample_sizes=sample_sizes
            )
    elif isinstance(obj, bool) or obj is None:
        return
    elif isinstance(obj, (int, float)):
        number = float(obj)
        ev.literal.add(number)
        if structured:
            ev.derivable.append(number)
    elif isinstance(obj, str):
        ev.literal.update(_numbers_in(obj))


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


def _strip_non_claims(text: str, identifiers: set[str] | frozenset[str] = frozenset()) -> str:
    """Remove everything in the answer whose digits are not a claim."""
    clean = _CITATION_MARKER_RE.sub(" ", text)
    clean = DESIGNATION_RE.sub(" ", clean)
    clean = _REFERENCE_RE.sub(" ", clean)
    for ident in sorted(identifiers, key=len, reverse=True):
        if any(ch.isdigit() for ch in ident) and len(ident) <= 40:
            pattern = r"(?<![\w-])" + re.escape(ident) + r"(?![\w-])"
            clean = re.sub(pattern, " ", clean, flags=re.IGNORECASE)
    return _IDENTIFIER_TOKEN_RE.sub(" ", HOLE_ID_RE.sub(" ", clean))


def _extract_number_tokens(
    text: str, identifiers: set[str] | frozenset[str] = frozenset()
) -> list[tuple[float, float, float]]:
    """(value, exact tolerance, rounded tolerance) for every numerical claim
    in ``text`` — see `_written_tolerance`."""
    out: list[tuple[float, float, float]] = []
    for match in _NUMBER_RE.finditer(_strip_non_claims(text, identifiers)):
        try:
            val = _parse_number(match.group())
        except ValueError:
            continue
        if val not in _SMALL_NUMBERS:
            exact, rounded = _written_tolerance(match.group())
            out.append((val, exact, rounded))
    return out


def _extract_numbers_from_text(text: str) -> list[float]:
    """Extract all numbers from response text.

    Citation markers, drill-hole identifiers, standard designations
    ("NI 43-101") and page/figure/section references are removed first --
    none is a numerical claim, and all parse as one. See `_NUMBER_RE` and
    `_IDENTIFIER_TOKEN_RE`.
    """
    return [value for value, _exact, _rounded in _extract_number_tokens(text)]


#: Conversion factors applied to every grounded value, both directions:
#: ppm and % (10 000), g/t and oz/t (31.1035), m and ft (3.28084), and
#: the x1000 steps (m and km, ppb and ppm, t and kt).
_CONVERSION_FACTORS: tuple[float, ...] = (10_000.0, 31.1035, 3.28084, 1_000.0)


def _expand_grounded_with_conversions(grounded: set[float]) -> set[float]:
    """Expand the grounded set with all valid unit-conversion derivatives.

    For each grounded value we add both directions of every conversion in
    `_CONVERSION_FACTORS`. This lets the guard accept "1.2 oz/t" when the
    tool returned "37.3 g/t" (37.3 / 31.1035 = 1.20). Rounding is handled
    on the ANSWER side by `_written_tolerance`, so the rounded / truncated
    copies of every expanded value this used to add (``round(v, 1)``,
    ``round(v, 2)``, ``int(v)``) are gone -- they multiplied the grounded set
    several-fold, and ``int()`` is not rounding.
    """
    expanded: set[float] = set(grounded)
    for g in grounded:
        if abs(g) < 1e9:  # skip sentinel values
            for factor in _CONVERSION_FACTORS:
                expanded.add(g / factor)
                expanded.add(g * factor)
    return expanded


def _matches_grounded(value: float, tolerance: float, grounded: list[float]) -> bool:
    """Is ``value`` (as written, give or take its rounding) a grounded value?

    Compared by magnitude: a dip written -60 in one place and 60 in another
    is the same measurement under two sign conventions.
    """
    target = abs(value)
    return any(abs(target - abs(g)) <= tolerance + 1e-9 * max(1.0, abs(g)) for g in grounded)


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

    # Grounding (reworked 2026-09-29, audit RAG-1).
    #
    # 1. Literal: the answer's number, give or take the rounding it was
    #    written with (`_written_tolerance`), equals a CONTENT number from
    #    the evidence or a unit conversion of one. Identifiers, scores,
    #    pages and section numbers are no longer content (`_NON_CONTENT_KEYS`).
    # 2. Derived: only against STRUCTURED rows. A mean / median / percentile
    #    of collar depths or assay values sits within 0.5x-2x of some row
    #    value, the Phase 5 follow-up (2026-05-19) case of "average depth is
    #    375.3 m" over 66 collars. Document prose gets no such allowance:
    #    with every number of a 5,000-character chunk in the window, almost
    #    any invented grade had a same-magnitude neighbour and passed.
    #
    #    The window is still over RAW values, never the conversion-expanded
    #    set (audit 2026-06-27: one count of 10 expands to roughly 0..100000).
    #
    # The old "equals the number of distinct grounded values" rule is gone:
    # that count is an artefact of serialisation, not of the data. Row
    # counts ARE grounded now; every list in a structured result adds its
    # length (`_walk_evidence`).
    evidence = _collect_evidence(tool_results)
    literal = [g for g in evidence.literal if abs(g) < 1e9]
    grounded = [
        g for g in _expand_grounded_with_conversions(evidence.literal) if abs(g) < 1e9
    ]
    derivable = sorted(g for g in evidence.derivable if abs(g) < 1e6)

    warnings = []
    for num, exact, rounded in _extract_number_tokens(text, evidence.identifiers):
        if _matches_grounded(num, rounded, literal) or _matches_grounded(num, exact, grounded):
            continue
        if _is_same_order_as_any(num, derivable):
            logger.debug(
                "Layer 3 derivation tolerance: %s is the scale of a structured "
                "value, likely average/median/percentile",
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


def _number_in_evidence(
    num: float, exact: float, rounded: float, ev: _Evidence
) -> bool:
    """The grounding test of :func:`verify_numbers`, against one evidence set."""
    literal = [g for g in ev.literal if abs(g) < 1e9]
    if _matches_grounded(num, rounded, literal):
        return True
    expanded = [g for g in _expand_grounded_with_conversions(ev.literal) if abs(g) < 1e9]
    if _matches_grounded(num, exact, expanded):
        return True
    return _is_same_order_as_any(num, sorted(g for g in ev.derivable if abs(g) < 1e6))


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
    from app.agent.hallucination.claim_sentences import split_units  # noqa: PLC0415

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
        for num, exact, rounded in _extract_number_tokens(unit.text, identifiers):
            if any(_number_in_evidence(num, exact, rounded, per_id[c]) for c in cited):
                continue
            elsewhere = [
                cid for cid, ev in per_id.items()
                if cid not in cited and _number_in_evidence(num, exact, rounded, ev)
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
    from app.agent.hallucination.claim_sentences import split_units  # noqa: PLC0415

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
    candidates = list(_HOLE_ID_RE.findall(clean))
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
            async with pg_pool.acquire() as conn:
                rows = await asyncio.wait_for(
                    conn.fetch(
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
                    ),
                    timeout=settings.TIMEOUT_POSTGIS_S,
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
