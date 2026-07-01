"""Keyword-driven query classification + lightweight text helpers.

Extracted from ``app.agent.orchestrator`` in Phase F.6 (see
``docs/master_plan_orchestrator_refactor.md``). All symbols here used to
live in ``orchestrator.py``; the orchestrator now re-exports them so any
caller that imported `from app.agent.orchestrator import ...` keeps
working unchanged.

Module contents (all pure, synchronous, no I/O):

* Keyword sets used by the deterministic classifier
  (``_SPATIAL_KEYWORDS`` … ``_GRAPH_KEYWORDS``)
* Jurisdiction / canonical-type / commodity hint tables for the public
  geoscience tool
* The classifier itself (``_classify_query``) and the small extractors
  it dispatches against (``_extract_public_geoscience_hints``,
  ``_extract_graph_entities``, ``_extract_label_from_query``,
  ``_detect_assay_element``)
* Temperature selection (``_select_temperature``)
* Query expansion (``_expand_query``) and sanitization
  (``_sanitize_query``)

This module is intentionally underscore-prefixed for every public symbol
— that matches the orchestrator's existing convention. The
``__all__`` list below is what the orchestrator re-exports.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.agent.viz_builder import extract_hole_ids

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Keyword sets
# ---------------------------------------------------------------------------

# Keywords that indicate a spatial/drill-hole question.
_SPATIAL_KEYWORDS = {
    "drill", "hole", "collar", "depth", "azimuth", "dip",
    "easting", "northing", "coordinate", "location",
    "diamond", "rc", "rotary", "percussion",
    "active", "completed", "abandoned",
    "count", "how many", "list",
}

# Keywords that indicate a document / technical report question.
# These trigger search_documents against the Qdrant georag_reports collection.
# We also include general geological-context keywords so broad "tell me about"
# style queries route to document retrieval when drill-hole data isn't the focus.
_DOCUMENT_KEYWORDS = {
    # Document type markers
    "report", "ni 43-101", "ni43", "43-101", "technical report",
    "pdf", "document", "filed", "filing",
    "publication", "paper", "journal", "study",
    # Resource / estimation terms
    "resource estimate", "indicated", "inferred", "measured",
    "mt at", "mt @", "lb u3o8", "mlb", "tonnage", "grade",
    # QP and compliance
    "qualified person", "qualified persons", "qp", "jorc",
    # NI 43-101 section keywords
    "summary", "introduction", "history", "conclusion", "recommendation", "recommendations",
    "property description", "deposit type", "exploration program",
    "adjacent propert", "sample preparation", "data verification",
    "accessibility", "climate", "infrastructure", "physiography",
    "reliance", "interpretation", "conclusions",
    # Broad geological context terms (only trigger when specifically
    # geological, not generic "what is" / "describe" which match everything)
    "geological", "geology", "setting", "mineralization", "mineralisation",
    "formation", "lithology", "alteration", "stratigraphy",
    "unconformity", "basement", "sandstone", "gneiss",
    "deposit", "prospect", "target", "anomaly",
}


_DOWNHOLE_KEYWORDS = {
    "lithology", "litho", "log", "strip", "strip log",
    "interval", "intersection", "intercept",
    "downhole", "unit", "rock type",
    "core", "rqd", "recovery",
    "weathering", "grain", "hardness",
}

# Keywords that route to search_public_geoscience (plan §09b).
# We keep these broad rather than brittle — the tool itself is cheap and
# failing open on an unrelated query is strictly better than missing a real
# public-geoscience question. Jurisdiction + canonical-type terms dominate;
# commodity tokens are shared with other classifiers so they don't appear
# here to avoid double-routing.
_PUBLIC_GEOSCIENCE_KEYWORDS = {
    # Surface name
    "public geoscience",
    # SMDI / mineral-occurrence vocabulary
    "smdi", "mineral occurrence", "mineral occurrences",
    "showing", "showings", "prospect", "prospects",
    "past producer", "past-producer", "occurrence",
    # Jurisdiction / authority tokens
    "saskatchewan", "ca-sk", "sgs", "sask geological",
    "government record", "government-published", "geological survey",
    "geohub", "sask.ca",
    # Resource-potential vocabulary
    "resource potential", "potential rank", "resource map",
    # Cross-corpus cues
    "cross-corpus", "both corpora", "corpus",
}

# Known Canadian jurisdiction codes we can recognize when mentioned by name
# or code in the query. Extend as more jurisdictions ship.
#
# Includes the bare 2-letter province codes ("sk", "bc", etc.). Substring
# matching would dangerously match these inside common English words
# ("task", "ask", "background"), so `_extract_public_geoscience_hints`
# uses word-boundary regex against `lower` instead of plain substring
# `in` membership.
_JURISDICTION_ALIASES: dict[str, str] = {
    "saskatchewan": "CA-SK",
    "ca-sk":        "CA-SK",
    "ca sk":        "CA-SK",
    "sk":           "CA-SK",
    # BC / ON / QC / … will light up only once their coming-soon status
    # flips, but recognizing the alias early is harmless.
    "british columbia": "CA-BC",
    "ca-bc":            "CA-BC",
    "bc":               "CA-BC",
    "ontario":          "CA-ON",
    "ca-on":            "CA-ON",
    # Note: "on" is deliberately omitted — \bon\b matches the English
    # preposition in essentially every query. Use "ontario" or "ca-on".
    "québec":           "CA-QC",
    "quebec":           "CA-QC",
    "ca-qc":            "CA-QC",
    "qc":               "CA-QC",
    "alberta":          "CA-AB",
    "ca-ab":            "CA-AB",
    # Note: "ab" omitted — matches "ab initio" and similar Latinisms.
    "manitoba":         "CA-MB",
    "ca-mb":            "CA-MB",
    "mb":               "CA-MB",
}

# Canonical-type hints the orchestrator passes through to the tool. When
# the user says "show me drillholes" we scope to drillhole_collar; when
# they say "occurrences" we scope to mineral_occurrence; otherwise we leave
# it unset and the tool queries all four.
_CANONICAL_TYPE_HINTS: list[tuple[str, str]] = [
    ("drillhole", "drillhole_collar"),
    ("drill hole", "drillhole_collar"),
    ("drill-hole", "drillhole_collar"),
    ("collar",    "drillhole_collar"),
    ("occurrence", "mineral_occurrence"),
    ("occurrences", "mineral_occurrence"),
    ("showing", "mineral_occurrence"),
    ("showings", "mineral_occurrence"),
    ("smdi", "mineral_occurrence"),
    ("resource potential", "resource_potential_zone"),
    ("potential zone", "resource_potential_zone"),
    (" mine ", "mine"),
    ("mines",  "mine"),
]

# Maps commodity-mention tokens in the query to canonical codes used by
# the Public Geoscience commodity_aliases table. Kept small + shared with
# the commodity filter UI; unknown tokens fall through (the tool will
# still return the grouping-level hits via semantic search).
_COMMODITY_TOKENS_TO_CODE: dict[str, str] = {
    "gold": "Au", "au": "Au",
    "silver": "Ag", "ag": "Ag",
    "copper": "Cu", "cu": "Cu",
    "nickel": "Ni", "ni": "Ni",
    "uranium": "U", "u3o8": "U",
    "lithium": "Li", "li2o": "Li",
    "zinc": "Zn", "zn": "Zn",
    "lead": "Pb", "pb": "Pb",
    "potash": "K", "k2o": "K",
    "rare earth": "REE", "ree": "REE",
    "rare earth elements": "REE",
    "cobalt": "Co",
    # Note: bare "co" deliberately omitted as a commodity alias.
    # It collides with Colorado's 2-letter state code ("CO") when US
    # jurisdictions are onboarded. Use "cobalt" (full name) instead.
    # Same rationale as omitting "on" / "ab" from jurisdiction aliases.
    "molybdenum": "Mo", "mo": "Mo",
}

# Keywords that trigger the assay data tool (query_assay_data).
_ASSAY_KEYWORDS = {
    "assay", "assays", "grade", "grades",
    "distribution", "histogram", "scatter",
    "geochemistry", "geochem",
    "u3o8", "uranium", "gold", "copper", "au", "cu",
    "sample", "samples",
    "ppm", "ppb", "pct", "percent",
    "highest grade", "lowest grade", "average grade",
    "max grade", "min grade",
}

# Phase F.9 — project-overview keywords.
#
# These trigger query_project_overview which returns silver.projects
# metadata (company, commodity, region) plus the distinct log-curve
# names available in silver.well_log_curves. Without this tool the
# deterministic classifier routes "what company / what county / what
# measurements" questions to spatial+documents — both come back empty
# on the project-overview surface, and the model refuses with
# "evidence does not include information about ...".
#
# Matching uses the same word-boundary contract as the other sets.
_PROJECT_OVERVIEW_KEYWORDS = {
    # Operator / company questions
    "company", "companies", "operator", "operators",
    "drilled by", "drilled the", "who drilled", "what company",
    # Geographic context — these are project-level metadata, not
    # PostGIS spatial filters
    "county", "counties", "state", "province",
    "located in", "where is", "where are",
    # Dataset-capability questions
    "what data", "what measurements", "what curves", "what logs",
    "data collected", "data available", "data included",
    "geophysical measurement", "geophysical measurements",
    "log curve", "log curves",
    # Coverage / inclusion questions
    "does the dataset", "does the data", "does the project",
    "does the corpus", "is there data", "include data",
    "include measurements", "include grade",
    # Phase F.9 broaden — the SME's Shirley Basin questions interleave
    # the project name between "does the" and "dataset" so the strict
    # phrase doesn't match. Add the substantive nouns + verbs separately
    # so any of these phrasings still trips the route.
    "dataset include", "dataset contain", "data include",
    "dataset cover", "dataset have", "project include",
    "grade measurement", "grade measurements",
    "uranium grade", "uranium measurement", "uranium measurements",
    # NOTE: "uranium production" intentionally NOT added — Q9 asks about
    # production rates which the project_overview tool cannot answer.
    # Letting that question fall through to refusal is the correct
    # behaviour for the eval.
    # Doc-count / inventory phrasings — wired alongside the silver.reports
    # rollup in ProjectOverviewResult so these answer cleanly instead of
    # refusing. The tool returns report_count + parser_breakdown so the
    # model can quote both the total and the file-type split.
    "how many reports", "how many documents", "how many pdfs",
    "report count", "document count", "pdf count",
    "indexed reports", "reports indexed", "documents indexed",
    "file types", "file type", "what types of files",
    "scanned logs", "scanned reports", "ocr",
    "what reports", "what documents", "what files",
}


# Keywords that indicate a knowledge-graph traversal question.
# These trigger traverse_knowledge_graph against Neo4j.
_GRAPH_KEYWORDS = {
    # Entity types (include plurals — word-boundary matching is exact)
    "deposit", "deposits", "triple r", "formation", "formations", "athabasca",
    "basement", "unconformity",
    "qualified person", "qp", "author", "authors",
    "mineral occurrence", "mineralization", "mineralisation",
    # Relationship queries
    "related to", "associated with", "connected to",
    "relationship", "linked",
    "who authored", "who wrote",
    "which holes target", "which holes intersect",
    "hosted by", "hosts",
    "part of",
    # Graph-specific terms
    "knowledge graph", "graph",
    "entity", "entities",
}


# Map query keywords to Neo4j node labels for label-based fallback queries.
# Neo4j review — canonical drillhole label is `Drillhole` (lowercase h)
# per the V1.2 schema migration in `index_neo4j.py`. Other entity labels
# stay PascalCase per Neo4j convention.
_LABEL_KEYWORDS: dict[str, str] = {
    "formation": "Formation",
    "formations": "Formation",
    "deposit": "Deposit",
    "deposits": "Deposit",
    "mineral": "MineralOccurrence",
    "mineralization": "MineralOccurrence",
    "mineralisation": "MineralOccurrence",
    "qualified person": "QualifiedPerson",
    "qp": "QualifiedPerson",
    "author": "QualifiedPerson",
    "report": "Report",
    "reports": "Report",
    "drill hole": "Drillhole",
    "drill holes": "Drillhole",
    "drillhole": "Drillhole",
    "drillholes": "Drillhole",
}


# Map query keywords to assay element JSONB key names. The tool's
# auto-substitute path (tools.query_assay_data) will swap these for their
# derived-composite siblings (`*_pct_e` / `*_ppb_e`) when only the
# composites are populated, so the orchestrator can keep using the
# canonical "raw assay" key here.
_ELEMENT_KEYWORDS: dict[str, str] = {
    "u3o8": "U3O8_ppm",
    "uranium": "U3O8_ppm",
    "gold": "Au_ppb",
    "au": "Au_ppb",
    "copper": "Cu_pct",
    "cu": "Cu_pct",
    # Derived-composite phrasings ("effective grade", "composite grade",
    # "weighted average grade") route directly to the *_e family.
    "effective grade": "U3O8_pct_e",
    "composite grade": "U3O8_pct_e",
    "weighted grade": "U3O8_pct_e",
}


# Geological synonym map for query expansion — the embedding model may
# not bridge abbreviations to full terms, so we expand common ones.
_GEO_SYNONYMS: dict[str, list[str]] = {
    # Measurement abbreviations
    "TD": ["total depth"],
    "AZ": ["azimuth"],
    "DH": ["drill hole"],
    "RC": ["reverse circulation"],
    "DD": ["diamond drill"],
    "QP": ["qualified person"],
    "GT": ["grade thickness", "grade-thickness product"],
    "RQD": ["rock quality designation"],
    "ppm": ["parts per million"],
    "ppb": ["parts per billion"],
    # Commodity abbreviations
    "U3O8": ["uranium oxide"],
    "Au": ["gold"],
    "Cu": ["copper"],
    "Ag": ["silver"],
    "Ni": ["nickel"],
    "Zn": ["zinc"],
    "Pb": ["lead"],
    "Mo": ["molybdenum"],
    "Li": ["lithium"],
    "Co": ["cobalt"],
    "PGE": ["platinum group elements"],
    "REE": ["rare earth elements"],
    # Deposit types
    "VMS": ["volcanogenic massive sulfide"],
    "IOCG": ["iron oxide copper gold"],
    "MVT": ["Mississippi Valley type"],
    "BIF": ["banded iron formation"],
    # Report/regulatory
    "NI 43-101": ["National Instrument 43-101", "technical report"],
    "NI43": ["National Instrument 43-101", "technical report"],
    "JORC": ["Joint Ore Reserves Committee"],
    "CIM": ["Canadian Institute of Mining"],
    # Alternative terms (bidirectional context)
    "borehole": ["drill hole"],
    "drillhole": ["drill hole"],
    "assays": ["geochemical analysis"],
    "intercept": ["intersection"],
}


# ---------------------------------------------------------------------------
# Classifier + extractors
# ---------------------------------------------------------------------------

def _classify_query(query: str) -> dict[str, Any]:
    """Return a dict indicating which tool categories are relevant.

    A query can match more than one category — e.g. "How many drill holes are
    listed in the 2024 PLS technical report?" is both spatial and documents.
    Both tools will be called and their results merged in context.

    Uses word-boundary matching to avoid false positives like "rc" matching
    inside "resource" or "how many" matching inside "anywho manymore".

    NOTE: This function produces internal routing buckets
    (spatial, documents, assay, downhole, graph, targeting, public_geo).
    It is NOT the same as the spec-class classifier in
    app.services.query_classifier.classify_query(), which maps to the five
    spec classes (factual, spatial, document, computation, viz, unknown).
    The spec-class classifier is called separately by run_deterministic_rag()
    for the answer_runs.query_class column and cache-key inclusion.

    # DEPRECATED routing bucket names — internal only. External callers wanting
    # the spec class should use classify_query() from query_classifier.py.
    # The routing dict produced here remains the dispatch authority for tool
    # selection and must not be replaced without a full orchestrator refactor.
    """
    lower = query.lower()
    # Extract words and multi-word phrases for robust matching.
    words = set(re.findall(r"\b[\w-]+\b", lower))

    def _matches(keywords: set[str]) -> bool:
        for kw in keywords:
            if " " in kw or "-" in kw:
                # Multi-word phrase — substring match with surrounding word boundaries.
                if re.search(r"\b" + re.escape(kw) + r"\b", lower):
                    return True
            else:
                if kw in words:
                    return True
        return False

    spatial = _matches(_SPATIAL_KEYWORDS)
    documents = _matches(_DOCUMENT_KEYWORDS)
    graph = _matches(_GRAPH_KEYWORDS)
    assay = _matches(_ASSAY_KEYWORDS)
    public_geo = _matches(_PUBLIC_GEOSCIENCE_KEYWORDS)
    # Phase F.9 — project-overview routing
    project_overview = _matches(_PROJECT_OVERVIEW_KEYWORDS)

    # Phase H — PLSS Township-Range-Section detection. The US Public Land
    # Survey System encodes geographic locations as e.g. "T28N R79W" or
    # "section 28N 79W" or "Section 36 T28N R79W". Qwen3 doesn't reliably
    # recognise these tokens as geographic when no other spatial keyword
    # appears in the query, so the classifier silently routed Q1
    # ("What company drilled the holes in section 28N 79W of Shirley Basin?")
    # to project_overview only. With no collar data in the context, the
    # model then refused.
    #
    # Three accepted shapes (all permissive):
    #   "<n>N <n>[EW]"            — bare township-range pair
    #   "T<n>N R<n>[EW]"          — full PLSS abbreviation
    #   "section <n> T<n>N R<n>[EW]" — section + T-R
    # Match without anchors so they fire inside any sentence. When detected
    # we force spatial=True (need collar coordinates) AND graph=True (need
    # to surface the operator company entity from the project subgraph).
    if not spatial:
        _plss_patterns = (
            r"\b\d+\s*[ns]\b\s*\d+\s*[ew]\b",           # "28N 79W"
            r"\bt\s*\d+\s*[ns]\b\s*r\s*\d+\s*[ew]\b",   # "T28N R79W"
            r"\bsection\s+\d+",                         # "section 36"
            r"\btownship\s+\d+",                        # "township 28"
        )
        if any(re.search(p, lower) for p in _plss_patterns):
            spatial = True
            graph = True
            logger.info(
                "_classify_query: PLSS Township-Range-Section syntax "
                "detected → forcing spatial+graph"
            )

    # Downhole log route: fires when the query names at least one specific
    # hole ID. The previous gate also required a lithology/log/interval
    # keyword, which meant "tell me about hole 36-1085" never triggered
    # query_downhole_logs — the LLM only got generic spatial collars and
    # refused with "I don't have data on that". Naming a specific hole is
    # itself enough intent to fetch that hole's details; visualisation
    # decisions can still be gated by the keyword set downstream.
    hole_ids = extract_hole_ids(query)
    downhole = bool(hole_ids)
    if downhole:
        spatial = True  # ensure collar data for map_payload + viz_payload

    # Drill target recommendation
    _TARGET_KEYWORDS = {"target", "targets", "recommend", "recommendation", "next hole",
                        "where to drill", "where should", "optimal", "proposed"}
    targeting = any(kw in lower for kw in _TARGET_KEYWORDS)
    if targeting:
        spatial = True   # need collar positions
        assay = True     # need grade data for IDW

    # Fallback: if no category matched, default to spatial + documents
    # so the user always gets SOME data context rather than an empty refusal.
    # The `classifier_fallback` flag is propagated so downstream code can
    # emit an escalation signal if the tools also return empty — that is
    # precisely the case where an agentic tool-calling path would help.
    classifier_fallback = False
    if not any([spatial, documents, downhole, graph, assay, targeting, public_geo, project_overview]):
        logger.info("_classify_query: no category matched, falling back to spatial+documents")
        spatial = True
        documents = True
        classifier_fallback = True

    # Extract public-geoscience filter hints once so the orchestrator can
    # pass them straight into search_public_geoscience without re-parsing.
    pg_jurisdictions, pg_canonical_types, pg_commodities = _extract_public_geoscience_hints(query)

    # Even when public_geo keywords didn't match directly, a query
    # that mentions Saskatchewan + a geological noun should route there too
    # (plan §09b: "queries both corpora when ambiguous"). This is the
    # "ambiguous → query both" branch.
    if not public_geo and pg_jurisdictions and (documents or graph):
        public_geo = True

    return {
        "spatial": spatial,
        "documents": documents,
        "downhole": downhole,
        "downhole_hole_ids": hole_ids if downhole else [],
        "graph": graph,
        "assay": assay,
        "targeting": targeting,
        "public_geo": public_geo,
        "project_overview": project_overview,
        "pg_jurisdictions": pg_jurisdictions,
        "pg_canonical_types": pg_canonical_types,
        "pg_commodities": pg_commodities,
        "classifier_fallback": classifier_fallback,
    }


def _extract_public_geoscience_hints(
    query: str,
) -> tuple[list[str], list[str], list[str]]:
    """Pull jurisdiction / canonical_type / commodity hints from the query.

    Hints are deliberately permissive — we'd rather over-include than
    narrow the search to zero hits, since the Qdrant filter expression
    is `MatchAny` (OR-within-array).

    Jurisdiction matching uses word-boundary regex so the 2-letter
    province codes (`sk`, `bc`, `qc`, `mb`) don't false-match inside
    English words ("ask", "back", "qcing", "mb" of memory). Canonical
    type and commodity matching keeps the legacy substring contract
    because their tokens are longer and less English-collision-prone.
    """
    lower = query.lower()

    jurisdictions: list[str] = []
    for alias, code in _JURISDICTION_ALIASES.items():
        # \b on alias boundaries — "sk" matches "SK" alone, not "task".
        # Multi-word phrases ("british columbia", "ca sk") still match
        # naturally; \b sits at every transition between word and non-
        # word characters.
        pattern = r"\b" + re.escape(alias) + r"\b"
        if re.search(pattern, lower) and code not in jurisdictions:
            jurisdictions.append(code)

    canonical_types: list[str] = []
    for token, ctype in _CANONICAL_TYPE_HINTS:
        if token in lower and ctype not in canonical_types:
            canonical_types.append(ctype)

    commodities: list[str] = []
    for token, code in _COMMODITY_TOKENS_TO_CODE.items():
        if token in lower and code not in commodities:
            commodities.append(code)

    return jurisdictions, canonical_types, commodities


def _extract_graph_entities(query: str, known_entities: list[str]) -> list[str]:
    """Extract known graph entity names from the user query.

    Returns a list of matched entity names preserving the order of
    ``known_entities`` (which comes pre-sorted by in-degree + name length,
    so longer/more-specific names are tried before generic ones).

    Pure function — the Neo4j fetch lives in fetch_project_graph_entities
    so the extraction itself can still be unit-tested without a live graph.
    """
    matches: list[str] = []
    lower = query.lower()

    for entity in known_entities:
        if entity.lower() in lower:
            matches.append(entity)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for m in matches:
        if m.lower() not in seen:
            seen.add(m.lower())
            unique.append(m)

    return unique


def _extract_label_from_query(query: str) -> str | None:
    """Return a Neo4j label if the query mentions a category keyword."""
    lower = query.lower()
    for kw, label in _LABEL_KEYWORDS.items():
        if kw in lower:
            return label
    return None


def _detect_assay_element(query: str) -> str | None:
    """Return the JSONB key for the element mentioned in the query, or None."""
    lower = query.lower()
    words = set(re.findall(r"\b[\w]+\b", lower))
    for kw, elem in _ELEMENT_KEYWORDS.items():
        if kw in words or kw in lower:
            return elem
    return None  # auto-detect in query_assay_data


def _select_temperature(query: str, categories: dict) -> float:
    """Select LLM temperature based on query type.

    Factual/numerical queries → low temperature (deterministic).
    Narrative/explanatory queries → slightly higher (more natural).
    """
    lower = query.lower()

    # Hard numerical queries need deterministic output
    if any(kw in lower for kw in ("how many", "count", "average", "deepest",
                                   "shallowest", "maximum", "minimum", "total")):
        return 0.05

    # Assay/grade queries are numerical
    if categories.get("assay"):
        return 0.05

    # Targeting is analytical
    if categories.get("targeting"):
        return 0.1

    # Document/narrative queries benefit from slightly more creativity
    if categories.get("documents") and not categories.get("spatial"):
        return 0.3

    # Default
    return 0.1


def _expand_query(query: str) -> str:
    """Expand geological abbreviations in the query for better retrieval."""
    expanded = query
    for abbrev, synonyms in _GEO_SYNONYMS.items():
        # Only expand if the abbreviation is a standalone word
        pattern = r'\b' + re.escape(abbrev) + r'\b'
        if re.search(pattern, expanded, re.IGNORECASE):
            # Append the first synonym in parentheses
            expanded = re.sub(
                pattern,
                lambda m: f"{m.group(0)} ({synonyms[0]})",
                expanded,
                count=1,
                flags=re.IGNORECASE,
            )
    return expanded


_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|rules?|prompts?)",
    r"(system|admin|root)\s*:\s*",
    r"you\s+are\s+now\s+",
    r"forget\s+(everything|all)",
    r"override\s+(mode|safety|rules?)",
    r"<\s*/?\s*system\s*>",
    r"\[\s*INST\s*\]",
    r"```\s*(system|prompt)",
    # Eval 13 R3 additions — Markdown image / link exfiltration and
    # role-redirection attempts that the previous list missed.
    r"!\[[^\]]*\]\([^)]*\)",
    r"jailbreak",
    r"DAN\s+mode",
    r"pretend\s+(you|to\s+be)",
    r"act\s+as\s+(a\s+)?(system|developer|admin)",
]


def _sanitize_query(query: str) -> str:
    """Sanitize user query to mitigate prompt injection attacks.

    Strips known injection patterns while preserving legitimate geological
    questions. This is defense-in-depth — the system prompt also instructs
    the LLM to ignore override attempts.

    Eval 13 R3 — also fires a Prometheus counter when ANY pattern
    matches so the OPS dashboard surfaces the attempt rate.
    """
    cleaned = query
    matches = 0
    for pattern in _INJECTION_PATTERNS:
        new_cleaned, n = re.subn(pattern, "", cleaned, flags=re.IGNORECASE)
        if n > 0:
            matches += n
        cleaned = new_cleaned

    # Cap query length (geological questions rarely exceed 500 chars)
    cleaned = cleaned[:1000].strip()

    if matches:
        try:
            from prometheus_client import Counter  # noqa: PLC0415

            global _PROMPT_INJECTION_ATTEMPTS
            try:
                _PROMPT_INJECTION_ATTEMPTS  # type: ignore[name-defined]  # noqa: B018
            except NameError:
                _PROMPT_INJECTION_ATTEMPTS = Counter(  # type: ignore[assignment]
                    "georag_prompt_injection_attempts_total",
                    "Count of queries where the sanitiser stripped at "
                    "least one known prompt-injection pattern. High "
                    "rate from one workspace = either user education "
                    "issue or active probing.",
                    labelnames=("count_bucket",),
                )
            # Bucket so cardinality is bounded: 1, 2-4, 5+
            bucket = "1" if matches == 1 else "2-4" if matches < 5 else "5+"
            _PROMPT_INJECTION_ATTEMPTS.labels(count_bucket=bucket).inc()
        except ImportError:
            pass
        logger.warning(
            "_sanitize_query: stripped %d injection pattern(s) from "
            "inbound query (length=%d)",
            matches, len(query),
        )

    return cleaned if cleaned else query[:500]


_PROMPT_INJECTION_ATTEMPTS = None  # type: ignore[assignment]


__all__ = [
    # Keyword sets
    "_SPATIAL_KEYWORDS",
    "_DOCUMENT_KEYWORDS",
    "_DOWNHOLE_KEYWORDS",
    "_PUBLIC_GEOSCIENCE_KEYWORDS",
    "_JURISDICTION_ALIASES",
    "_CANONICAL_TYPE_HINTS",
    "_COMMODITY_TOKENS_TO_CODE",
    "_ASSAY_KEYWORDS",
    "_GRAPH_KEYWORDS",
    "_PROJECT_OVERVIEW_KEYWORDS",
    "_LABEL_KEYWORDS",
    "_ELEMENT_KEYWORDS",
    "_GEO_SYNONYMS",
    # Classifier + extractors
    "_classify_query",
    "_extract_public_geoscience_hints",
    "_extract_graph_entities",
    "_extract_label_from_query",
    "_detect_assay_element",
    "_select_temperature",
    "_expand_query",
    "_sanitize_query",
]
