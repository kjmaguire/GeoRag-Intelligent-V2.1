"""Spec-aligned query-class classifier for the GeoRAG deterministic orchestrator.

This module codifies the five spec query classes defined in Module 4 spec B13
and addendum §04h:

    factual     — general geological questions answered from documents or graph
    spatial     — geographic / drill-hole location questions (PostGIS queries)
    document    — report-section, NI 43-101, publication questions (Qdrant)
    computation — grade, tonnage, resource estimate, statistics (structured + docs)
    viz         — map, chart, plot, stereonet rendering requests
    unknown     — fallback when no class matches after all rules

Rule precedence (strict, descending priority):
    viz > spatial > computation > document > factual > unknown

VIZ is first because explicit rendering intents ("plot", "render", "draw") are
the clearest user signal and should override all other classes.  Spatial is
second because drill-hole location queries are the most common concrete
retrieval task and need direct PostGIS dispatch.

Design rationale
----------------
- Simple rule-based classification is fast, deterministic, and debuggable.
- The orchestrator's existing `_classify_query()` dispatches to internal routing
  buckets (spatial, documents, assay, downhole, graph, targeting,
  public_geo).  These remain the routing authority — `classify_query()`
  adds a *spec-class label* on top of them for audit, cache-key inclusion, and
  module test harness alignment.  It does NOT replace the routing buckets.
- Keyword sets are geological-domain-specific.  Generic English words (what,
  how, list, section) are EXCLUDED to prevent false positives on off-topic
  queries.  The FACTUAL class deliberately uses a small, conservative token
  set — most factual geological questions route to DOCUMENT via the geological
  context keywords in that set.
- UNKNOWN is the correct output for clearly off-topic queries.

Usage
-----
    from app.services.query_classifier import classify_query, QueryClassLiteral

    spec_class: QueryClassLiteral = classify_query("How many drill holes near X?")
    # → "spatial"

Relationship to orchestrator._classify_query()
----------------------------------------------
`_classify_query()` in orchestrator.py produces the internal routing dict
{spatial: bool, documents: bool, assay: bool, ...}.  That function is NOT
replaced here.  The orchestrator calls `_classify_query()` first for routing,
then `classify_query()` for the spec-class label that goes into answer_runs and
the cache key.

Module 4 spec B13 — retrieval_strategy_version
----------------------------------------------
The constant below must be incremented when any behavioral retrieval change
lands (B4: hybrid Qdrant + RRF).
"""

from __future__ import annotations

import re
from typing import Literal

# ---------------------------------------------------------------------------
# QueryClassLiteral — re-exported here so callers can import from one place.
# ---------------------------------------------------------------------------
QueryClassLiteral = Literal[
    "factual",
    "spatial",
    "document",
    "computation",
    "viz",
    "unknown",
]

# ---------------------------------------------------------------------------
# Retrieval strategy version (Module 4 spec B13)
# Increment when any behavioral retrieval change lands.
# v1-hybrid-2026-04-21: hybrid Qdrant RRF introduced (Chunk 2)
# v2-retrieval-only-cache-2026-04-21: cache boundary moved from GeoRAGResponse
#   to CachedRetrievalContext (Phase B addendum). Old v4 keys (answer-level)
#   are unreachable; new v5 keys store retrieval context only.
# v2.1-citation-per-claim-2026-04-21: _SYSTEM_PROMPT_VERSION bumped to 6
#   (per-claim citation discipline tightened in DEFAULT and NUMERIC variants).
#   Sub-minor bump because retrieval behavior itself did not change — only
#   the prompt seen during synthesis changed. Cache bust ensures any cached
#   retrieval context from v2 is re-keyed and gets the new prompt version
#   on the next synthesis pass.
# v3-qwen3-moe-2026-04-21: _SYSTEM_PROMPT_VERSION bumped to 7 (model flip
#   qwen2.5:14b → qwen3:30b-a3b MoE). New model may produce different
#   synthesis behavior; version bump ensures all v2.x Redis cache keys miss
#   and rebuild against the new model. OLLAMA_NUM_CTX dropped from 24576 to
#   8192; MAX_CONTEXT_TOKENS reduced proportionally to 7500.
# v3.1-think-off-2026-04-21: _SYSTEM_PROMPT_VERSION bumped to 8 (TOOL-CALL-01
#   fix). Grounded synthesis now passes enable_thinking=False at all call sites
#   — saves 1000-2000 tokens per call, eliminates RC-C2 budget exhaustion.
#   Empty-content guard returns structured fallback. OLLAMA_NUM_CTX raised to
#   16384; MAX_CONTEXT_TOKENS raised to 15000 (2× per-class values).
#   Cache invalidation intentional — new ctx budget changes evidence slice.
# ---------------------------------------------------------------------------
RETRIEVAL_STRATEGY_VERSION = "v3.1-think-off-2026-04-21"

# ---------------------------------------------------------------------------
# Keyword token sets — geological domain specific only.
#
# RULES:
# - Single tokens are matched against the word-set (O(1) set intersection).
# - Multi-word phrases use word-boundary regex against the full lowercased query.
# - Generic English words (what, how, list, section, where, etc.) must NOT
#   appear in any set — they produce false positives for off-topic queries.
# - Precedence: viz > spatial > computation > document > factual > unknown.
# ---------------------------------------------------------------------------

# VIZ — explicit rendering / plotting / charting intents.
# These are deliberate user actions ("plot", "render", "draw", "visualize").
# VIZ is first in precedence because these verbs are unambiguous user signals.
_VIZ_TOKENS: set[str] = {
    "plot", "chart", "visualize", "visualise", "render",
    "stereonet", "rosette",
    "heatmap", "contour", "isopach",
    "draw", "generate",
    # Specific visualization types only (no generic "map" — too broad)
    "wireframe", "downhole-plot", "fence-diagram",
}

_VIZ_PHRASES: set[str] = {
    "show me a", "plot the", "chart the", "render the", "draw the",
    "rose diagram", "stereonet plot", "fence diagram",
    "heat map", "plan view map", "cross section view",
    "generate a map", "render a map",
}

# SPATIAL — drill-hole locations, coordinate queries, geographic proximity.
# Only include geology-domain tokens; remove generic English words.
_SPATIAL_TOKENS: set[str] = {
    # Drill-hole structure (geological jargon)
    "drill", "hole", "holes", "collar", "collars",
    "azimuth", "dip", "inclination",
    "easting", "northing", "coordinate", "coordinates",
    # Hole type codes (geological abbreviations)
    "ddh", "hq", "bq", "nq",
    # Spatial query operators (geographic meaning here)
    "near", "within", "radius", "bbox", "intersect",
    "polygon",
    # Coordinate system identifiers
    "utm", "epsg", "crs", "srid",
    # Geographic proximity (domain-specific phrasing)
    "collar-location", "drillhole-location",
}

_SPATIAL_PHRASES: set[str] = {
    "drill hole", "drill holes", "drillhole", "drillholes",
    "how many drill", "how many collar", "how many hole",
    "hole id", "hole ids", "collar location", "near point",
    "within radius", "within metres", "within meters",
    "within km", "within kilometres", "within kilometers",
    "cross section", "plan view",
    "diamond drill", "rc drill", "rotary drill",
    "active holes", "completed holes", "abandoned holes",
}

# COMPUTATION — grade, tonnage, resource estimates, statistical aggregations.
# Includes assay, geochemistry, and numerical mining calculations.
_COMPUTATION_TOKENS: set[str] = {
    # Mining resource vocabulary (geological jargon)
    "grade", "grades", "tonnage", "tonnes", "tons",
    "reserve", "reserves",
    "ppm", "ppb", "pct",
    "u3o8", "au", "cu",
    # Statistical operations (mathematical intent)
    "calculate", "compute", "average", "mean", "median",
    "maximum", "minimum", "aggregate",
    "distribution", "correlation",
    "weighted", "interpolate", "idw",
    # Assay / geochemistry
    "assay", "assays",
    "geochemistry", "geochem",
    # Grade-thickness
    "accumulation",
    # Economic cut-off
    "cutoff", "cut-off", "break-even",
    # Common mining numeric suffixes
    "mlb", "mlbs",
}

_COMPUTATION_PHRASES: set[str] = {
    "resource estimate", "grade distribution", "average grade",
    "highest grade", "lowest grade", "max grade", "min grade",
    "grade thickness", "grade-thickness", "grade thickness product",
    "cut off grade", "cutoff grade", "breakeven grade",
    "weighted average", "weighted average grade",
    "mt at", "mt @", "lb u3o8",
    "total tonnage", "indicated tonnage", "inferred tonnage",
    "measured tonnage", "indicated resource", "inferred resource",
    "measured resource",
    "uranium grade", "gold grade", "copper grade",
    "grade intercept", "highest intercept", "best intercept",
    "geochem sample", "assay result",
}

# DOCUMENT — NI 43-101 reports, publications, technical report sections.
# Only words that specifically indicate a document retrieval intent.
_DOCUMENT_TOKENS: set[str] = {
    # Document type markers (domain-specific)
    "report", "reports", "ni43", "43-101",
    "pdf", "filing",
    "publication", "publications", "journal",
    # NI 43-101 structure (section headings appear in reports)
    "appendix", "jorc",
    # Provenance markers
    "authored", "published",
    # QP markers
    "qp",
}

_DOCUMENT_PHRASES: set[str] = {
    "ni 43-101", "technical report", "technical reports",
    "qualified person", "qualified persons",
    "property description", "deposit type",
    "exploration program",
    "sample preparation", "data verification",
    "ni43-101", "43-101 report",
    "filed on sedar", "filed with sedar",
    "recommendations section", "conclusion section",
    "introduction section", "history section",
    "appendix of the", "section of the report",
    "what does the report", "what does the technical report",
    "according to the report",
    "geological setting",
    "geological study",
    # 2026-06-01 — summarisation verbs route to the narrative profile so
    # "summarise / summarize / summary of X" stops misclassifying as
    # computation and hitting the refusal-happy NUMERIC prompt.
    "summary of", "summarise", "summarize",
    "give me a summary", "give a summary",
    "describe the", "explain the", "tell me about",
    "what does", "what is in", "what's in",
    "section of", "article", "chapter",
}

# FACTUAL — knowledge-graph / entity questions not covered by DOCUMENT.
# Conservative token set — mainly graph-entity and relationship vocabulary.
# Most factual geological questions route to DOCUMENT via document phrases.
_FACTUAL_TOKENS: set[str] = {
    # Graph entity relationship vocabulary
    "entity", "entities",
    "relationship", "relationships",
    "pathfinder",
    # Geological entity type identifiers (non-document, non-computation)
    "vein", "fault", "fold", "intrusion",
    "mineralogy", "petrology",
}

_FACTUAL_PHRASES: set[str] = {
    "related to the", "connected to the", "associated with the",
    "hosted by", "part of the",
    "knowledge graph", "graph entity",
    "deposit style",
    "uranium deposit style", "gold deposit style", "copper deposit style",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _word_set(text: str) -> set[str]:
    """Extract word tokens from lowercased text."""
    return set(re.findall(r"\b[\w-]+\b", text))


def _matches_tokens(word_set: set[str], tokens: set[str]) -> bool:
    """Check if any single-token keyword exists in the word set."""
    return bool(word_set & tokens)


def _matches_phrases(lower_text: str, phrases: set[str]) -> bool:
    """Check if any multi-word phrase matches in the text with word boundaries."""
    for phrase in phrases:
        if re.search(r"\b" + re.escape(phrase) + r"\b", lower_text):
            return True
    return False


def _matches(lower_text: str, word_set: set[str], tokens: set[str], phrases: set[str]) -> bool:
    """Combined token + phrase match."""
    return _matches_tokens(word_set, tokens) or _matches_phrases(lower_text, phrases)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def classify_query(query: str) -> QueryClassLiteral:
    """Return the spec query class for a geological natural-language query.

    Precedence (strict, descending): viz > spatial > computation > document > factual > unknown.

    VIZ is first because explicit render/plot/draw verbs are the clearest
    unambiguous user intent signal.  Spatial is second because drill-hole
    location queries are the most frequent concrete retrieval task.

    Args:
        query: Raw natural-language query from the user.

    Returns:
        One of the six QueryClassLiteral values.

    Examples:
        >>> classify_query("How many drill holes are within 500m of the target zone?")
        'spatial'
        >>> classify_query("What is the average uranium grade in the 2024 resource estimate?")
        'computation'
        >>> classify_query("Plot a stereonet of the structural measurements from PLS-22.")
        'viz'
        >>> classify_query("What does the NI 43-101 report say about the deposit type?")
        'document'
        >>> classify_query("What is the geological setting of the Triple R deposit?")
        'factual'
    """
    lower = query.lower()
    words = _word_set(lower)

    if _matches(lower, words, _VIZ_TOKENS, _VIZ_PHRASES):
        return "viz"

    if _matches(lower, words, _SPATIAL_TOKENS, _SPATIAL_PHRASES):
        return "spatial"

    if _matches(lower, words, _COMPUTATION_TOKENS, _COMPUTATION_PHRASES):
        return "computation"

    if _matches(lower, words, _DOCUMENT_TOKENS, _DOCUMENT_PHRASES):
        return "document"

    if _matches(lower, words, _FACTUAL_TOKENS, _FACTUAL_PHRASES):
        return "factual"

    return "unknown"
