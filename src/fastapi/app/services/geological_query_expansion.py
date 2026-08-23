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

    # ── Uranium (added 2026-08-21) ───────────────────────────────
    # The flagship corpus is Cameco / Athabasca uranium material and the
    # table had no uranium entry at all — while identifier_boost._COMMODITY_
    # CODES lists both U and U3O8. "What U3O8 grades did the Triple R zone
    # return?" matched nothing and went to the embedder unexpanded, with the
    # passages phrasing the same content as "uranium assays" and "eU3O8
    # equivalent". Highest-frequency vocabulary gap in the module.
    #
    # Bare "U" stays out: it is ambiguous with the pronoun and with unit
    # symbols. The compound forms are unambiguous.
    "U3O8":  "uranium oxide",
    "eU3O8": "equivalent uranium oxide",
    "U₃O₈": "uranium oxide",
}

# ───────────────────────────────────────────────────────────────────
# Synonym groups — full words geologists vary on, added 2026-08-21.
#
# The table above is abbreviations only, and expansion therefore did nothing
# for the terms people actually phrase differently. A user asks about
# "grades"; the passage says "assays". A user asks about an "intercept"; the
# report calls it an "intersection". Neither the dense nor the sparse branch
# had any help bridging that, and sparse in particular had no shared token
# to match on at all.
#
# Deliberately small. Each group is a set of terms that mean the same thing
# in an assay/intercept context, so appending the alternatives adds recall
# without changing what the query is asking.
# ───────────────────────────────────────────────────────────────────
_SYNONYMS: dict[str, str] = {
    "grade":        "assay tenor",
    "grades":       "assays tenor",
    "assay":        "grade",
    "assays":       "grades",
    "intercept":    "intersection",
    "intercepts":   "intersections",
    "intersection": "intercept",
    "intersections": "intercepts",
    "true width":   "downhole width",
    "downhole width": "true width",
    "collar":       "drillhole location",
    "cut-off":      "cutoff grade",
    "cutoff":       "cut-off grade",
}

# Word-boundary pattern compiled once. Sorted longest-first so
# multi-character abbreviations match before shorter substrings.
_ABBREVIATIONS_BY_LENGTH = sorted(
    _ABBREVIATIONS.items(), key=lambda kv: -len(kv[0])
)

# One alternation, longest-first so "true width" matches before "width" would
# and "intersections" before "intersection". Compiled once.
_SYNONYM_PATTERN = re.compile(
    r"\b(?:"
    + "|".join(
        re.escape(term)
        for term in sorted(_SYNONYMS, key=len, reverse=True)
    )
    + r")\b",
    re.IGNORECASE,
) if _SYNONYMS else None


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

    # Synonym groups run after the abbreviations, against whatever budget is
    # left.
    #
    # ONE pass over the string, not one pass per term. The groups are mutual
    # — "grade" offers "assay", "assay" offers "grade" — so a per-term loop
    # re-scans text it has just inserted and nests: "grades" became
    # "grades (assays (grades) tenor)", and "cut-off grade" degenerated into
    # four levels of parentheses. A single alternation, longest-first, with a
    # callback that expands each term at most once, cannot see its own
    # output.
    remaining = max(0, max_expansions - expansions_added)
    if remaining and _SYNONYM_PATTERN is not None:
        budget = {"left": remaining}

        def _annotate(match: re.Match[str]) -> str:
            found = match.group(0)
            key = found.lower()
            if budget["left"] <= 0 or key in used:
                return found
            used.add(key)
            budget["left"] -= 1
            return f"{found} ({_SYNONYMS[key]})"

        expanded = _SYNONYM_PATTERN.sub(_annotate, expanded)

    return expanded


__all__ = ["expand_query"]
