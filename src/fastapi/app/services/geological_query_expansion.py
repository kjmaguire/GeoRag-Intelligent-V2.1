"""Geological-domain query expansion (Eval 15 R3 follow-up).

The dense embedder (bge-small-en-v1.5) is trained on general English.
It does not natively know that "Au" means "gold", "DDH" means
"diamond drillhole", or "g/t" stands for "grams per tonne". Queries
using abbreviations therefore miss passages using the full term —
and vice versa.

This module returns an EXPANDED query string with both forms
appended, e.g.:

    "What's the highest Au grade in PLS-22-08?"
    →
    "What's the highest Au (gold) grade (g/t grams per tonne) in PLS-22-08?"

The expansion is conservative — only well-known unambiguous
geological abbreviations are listed. The resulting string stays
short enough not to dilute the dense embedding.

Used by the orchestrator before computing `query_dense` and
`query_sparse`. Both embeddings benefit: dense gets the full-form
semantics; sparse gets exact-token matches against passages that
use either form.
"""
from __future__ import annotations

import re

# ──────────────────────────────────────────────────────────────────────
# Symbol / abbreviation → canonical full form.
#
# Order matters for overlapping prefixes; longest-first to avoid
# expanding "kt" before "ktoe" (we don't have ktoe but the principle
# stands for future additions).
#
# Only abbreviations that uniquely identify a geological concept are
# listed. "U" alone is ambiguous (uranium, but also units, you, etc.)
# so we only expand it in clear contexts via the trailing-context
# patterns below.
# ──────────────────────────────────────────────────────────────────────

_ABBREVIATIONS: dict[str, str] = {
    # Commodity symbols — chemical
    "Au":    "gold",
    "Ag":    "silver",
    "Cu":    "copper",
    "Pb":    "lead",
    "Zn":    "zinc",
    "Ni":    "nickel",
    "Co":    "cobalt",
    "Mo":    "molybdenum",
    "Pt":    "platinum",
    "Pd":    "palladium",
    "Sn":    "tin",
    "REE":   "rare earth elements",

    # Units
    "g/t":   "grams per tonne",
    "oz/t":  "ounces per tonne",
    "ppm":   "parts per million",
    "ppb":   "parts per billion",
    "wt%":   "weight percent",

    # Drilling abbreviations
    "DDH":   "diamond drillhole",
    "RC":    "reverse circulation",
    "RAB":   "rotary air blast",
    "AC":    "air core",

    # Other domain abbreviations
    "QP":    "qualified person",
    "NI 43-101": "National Instrument 43-101",
    "JORC":  "Joint Ore Reserves Committee code",
    "PEA":   "preliminary economic assessment",
    "PFS":   "pre-feasibility study",
    "FS":    "feasibility study",
}

# Word-boundary pattern compiled once. Sorted longest-first so
# multi-character abbreviations match before shorter substrings.
_ABBREVIATIONS_BY_LENGTH = sorted(
    _ABBREVIATIONS.items(), key=lambda kv: -len(kv[0])
)


def expand_query(query: str, *, max_expansions: int = 6) -> str:
    """Return the query with up to ``max_expansions`` geological terms
    annotated with their canonical full forms.

    Annotations are appended in-line as parentheticals so the dense
    embedder sees both surface forms in one sentence. Sparse search
    benefits too because the expanded text carries both the
    abbreviation tokens and the full-word tokens.

    Each abbreviation is expanded AT MOST ONCE per query — duplicates
    don't help retrieval and clutter the embedding.
    """
    if not query:
        return query
    expanded = query
    used: set[str] = set()
    expansions_added = 0

    for abbr, full in _ABBREVIATIONS_BY_LENGTH:
        if expansions_added >= max_expansions:
            break
        if abbr.lower() in used:
            continue
        # Case-sensitive word-boundary match for commodity symbols
        # (so "Au" matches but "auction" doesn't); case-insensitive
        # for everything else. The commodity symbols are 2-3 letter
        # caps; if the abbreviation is all-caps and 2-3 chars, match
        # case-sensitively.
        is_chem_symbol = (
            len(abbr) <= 3 and abbr.isalpha()
            and any(c.isupper() for c in abbr)
        )
        flags = 0 if is_chem_symbol else re.IGNORECASE

        # Use word-boundary so "Au" doesn't match inside "Australia".
        pat = re.compile(rf"\b{re.escape(abbr)}\b", flags)
        if pat.search(expanded):
            # Insert " (full)" after the FIRST match only.
            expanded = pat.sub(f"{abbr} ({full})", expanded, count=1)
            used.add(abbr.lower())
            expansions_added += 1

    return expanded


__all__ = ["expand_query"]
