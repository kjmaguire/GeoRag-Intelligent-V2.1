"""Orchestrator-compatible hallucination validators.

These are adaptations of Layers 3, 4, and 6 that work with the deterministic
orchestrator's tool_results list instead of Pydantic AI's ctx.messages.

The original validators in layer3_numerical.py, layer4_entity.py, and
layer6_constraints.py are designed for Pydantic AI's output_validator
decorator pattern (they access ctx.messages). This module provides
equivalent validation using the orchestrator's direct tool_results.

Usage in orchestrator:
    from app.agent.hallucination.orchestrator_validators import run_post_assembly_validation
    response, warnings = await run_post_assembly_validation(response, tool_results, deps)
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from app.agent.deps import AgentDeps
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
# Audit 2026-06-27 (T3): this is the marker regex the AGENTIC path's
# verify_numbers uses. Accept both the colon form ([DATA:1]) the prompts
# instruct the model to emit (canonical, Kyle 2026-04-22) and the dash form
# the assembler appends, plus the PGEO prefix — otherwise colon citation
# indices (e.g. [PUB:23]) leak through as ungrounded "numbers" / false retries.
_CITATION_MARKER_RE = re.compile(r"\[(?:DATA|NI43|PUB|PGEO)[:-]\d+\]")
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

_NUMBER_WITH_UNIT_RE = re.compile(
    r"([-+]?\d+\.?\d*)\s*"
    r"(g/t|oz/t|ppm|ppb|wt%|%|m|ft|km|kt|Mt|mt|tonnes?|lbs?|kg)"
    r"\b",
    re.IGNORECASE,
)

# Unit families — values within a family are convertible to each other
# via the existing _expand_grounded_with_conversions() table. A response
# tuple whose value matches a grounded value BUT whose unit lives in a
# different family is a unit-pair fabrication (the value happens to
# coincide; the unit is wrong).
_UNIT_FAMILIES: dict[str, str] = {
    # mass concentration
    "g/t": "mass_conc",
    "oz/t": "mass_conc",
    "ppm": "mass_conc",
    "ppb": "mass_conc",
    "wt%": "mass_conc",
    "%": "mass_conc",
    # length
    "m": "length",
    "ft": "length",
    "km": "length",
    # tonnage
    "kt": "tonnage",
    "mt": "tonnage",
    "tonne": "tonnage",
    "tonnes": "tonnage",
    "lb": "tonnage",
    "lbs": "tonnage",
    "kg": "tonnage",
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
        # All candidates live in different families — unit fabrication.
        observed = sorted({u for (_, u) in candidates})
        warnings.append(
            f"Layer 3 tuple: value {v} reported as '{unit_r}' "
            f"but evidence carries it as {observed} (different unit family)"
        )
    return warnings


def _extract_numbers_from_text(text: str) -> list[float]:
    """Extract all numbers from response text, excluding citation markers."""
    clean = _CITATION_MARKER_RE.sub("", text)
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
    """Collect all numbers from tool results for grounding verification."""
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
            try:
                extras.add(float(int(v)))
            except (OverflowError, ValueError):
                pass
    expanded |= extras
    return expanded


def verify_numbers(text: str, tool_results: list[tuple[str, Any]]) -> list[str]:
    """Layer 3: Check that every number in the response is grounded in tool results.

    C3 tightening (Module 6 Chunk 3): removed the silent-skip for ≤ 3
    ungrounded numbers.  Every numeric token must be derivable from cited
    evidence or a valid unit conversion of a cited value.

    Phase F.5: strip the proactive-insights block before extracting numbers.
    Those numbers (mean depth, σ multiples) are deterministically computed
    by ``anomaly_detector`` from raw tool_results rows and don't appear
    verbatim in the cited tool results — they're grounded by construction,
    not by retrieval.

    Returns a list of warning strings for ungrounded numbers.
    """
    if not settings.NUMERICAL_VERIFICATION_ENABLED:
        return []

    from app.agent.anomaly_detector import strip_proactive_insights  # noqa: PLC0415
    text = strip_proactive_insights(text)

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
    g_min = grounded_finite[0] if grounded_finite else None
    g_max = grounded_finite[-1] if grounded_finite else None
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
        if g_min is not None and g_max is not None and g_min <= num <= g_max:
            logger.debug(
                "Layer 3 derivation tolerance: %s inside grounded range "
                "[%.3f, %.3f] — likely average/median/percentile",
                num,
                g_min,
                g_max,
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

_HOLE_ID_RE = re.compile(r"\b([A-Z]{1,8}-\d{1,6}-\d{1,6})\b")
_CITATION_PREFIX_SET = frozenset({"DATA", "NI43", "PUB", "PGEO"})

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
_ALL_MARKER_RE = re.compile(r"\[(?:DATA|NI43|PUB|PGEO|ev)[:-][A-Za-z0-9-]+\]")


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
        except Exception:
            continue
    return entity_tokens


async def verify_entities(
    text: str,
    project_id: str,
    pg_pool: Any,
    neo4j_driver: Any,
    tool_results: list[tuple[str, Any]] | None = None,
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
    from app.agent.anomaly_detector import strip_proactive_insights  # noqa: PLC0415
    text = strip_proactive_insights(text)

    # Strip all citation markers before extraction.
    clean = _ALL_MARKER_RE.sub("", text)

    # --- Hole IDs (original check) ---
    candidates = _HOLE_ID_RE.findall(clean)
    hole_ids = [
        hid for hid in dict.fromkeys(candidates)
        if hid.split("-", 1)[0] not in _CITATION_PREFIX_SET
    ]

    warnings: list[str] = []

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
        grounded_tokens = _extract_entities_from_tool_results(tool_results)
        # Find bare commodity tokens in the answer text.
        commodity_pattern = re.compile(
            r"\b(" + "|".join(re.escape(c) for c in sorted(_COMMODITY_CODES, key=len, reverse=True)) + r")\b"
        )
        cited_commodities = [m.group(1) for m in commodity_pattern.finditer(clean)]
        cited_commodities = list(dict.fromkeys(cited_commodities))
        for commodity in cited_commodities:
            if commodity.lower() not in grounded_tokens:
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
        # Build the tool-results token bag once (already computed above for
        # the commodity-grounding check; recompute if that branch was skipped).
        if tool_results:
            tool_tokens = grounded_tokens  # noqa: F821  (set in the commodity block)
        else:
            tool_tokens = set()

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
        # If known_formations is empty, graph not yet populated — fail-open (no warnings).

    return warnings


# ---------------------------------------------------------------------------
# Layer 6 — Geological Constraints (orchestrator version)
# Delegates to the existing constraint checker which only needs the text.
# ---------------------------------------------------------------------------

def verify_constraints(text: str) -> list[str]:
    """Layer 6: Check geological plausibility of numerical claims.

    Phase F.5: strip the proactive-insights block before constraint checking.
    Anomaly insights are by definition statistical outliers (e.g. "445 m TD
    — 2.2σ deeper than project average of 374 m") and tripping the depth /
    grade ceilings on those numbers is exactly the noise the strip avoids.

    Returns a list of warning strings for constraint violations.
    """
    if not settings.GEOLOGICAL_CONSTRAINTS_ENABLED:
        return []

    from app.agent.anomaly_detector import strip_proactive_insights  # noqa: PLC0415
    text = strip_proactive_insights(text)

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
# Unified validation runner
# ---------------------------------------------------------------------------

async def run_post_assembly_validation(
    response: GeoRAGResponse,
    tool_results: list[tuple[str, Any]],
    deps: AgentDeps,
) -> tuple[GeoRAGResponse, list[str], bool]:
    """Run all 3 orchestrator-compatible validators on an assembled response.

    Returns:
        (response, warnings, should_retry) — response is unchanged,
        warnings is a list of human-readable strings, should_retry is True
        if critical/high-severity issues were found (fabricated entities,
        geological constraint violations) warranting an LLM retry.
    """
    all_warnings: list[str] = []

    # Layer 3 — numerical grounding
    all_warnings.extend(verify_numbers(response.text, tool_results))

    # Layer 4 — entity resolution (async — needs database)
    # Pass tool_results so commodity-code grounding can verify against cited evidence.
    entity_warnings = await verify_entities(
        response.text,
        deps.project_id,
        deps.pg_pool,
        deps.neo4j_driver,
        tool_results=tool_results,
    )
    all_warnings.extend(entity_warnings)

    # Layer 6 — geological constraints
    all_warnings.extend(verify_constraints(response.text))

    # Classify warnings by severity — entity failures are critical
    # (fabricated hole IDs), constraints are high, numerical grounding
    # is advisory UNLESS it crosses a threshold or co-locates with a
    # constraint violation.
    critical = [w for w in all_warnings if w.startswith("Layer 4:")]
    high = [w for w in all_warnings if w.startswith("Layer 6:")]
    advisory = [w for w in all_warnings if w.startswith("Layer 3:")]

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
                try:
                    _layer3_nums.add(float(m))
                except ValueError:
                    pass
        _layer6_nums = set()
        for w in high:
            for m in _num_re.findall(w):
                try:
                    _layer6_nums.add(float(m))
                except ValueError:
                    pass
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
