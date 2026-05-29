"""Hole ID canonicalization and fuzzy matching helpers.

Provides:
  - canonicalize(hole_id) — strip separators, uppercase, return None for blank.
  - fuzzy_match(target, candidates, threshold) — best rapidfuzz match above threshold.
  - suggest_collisions(ids) — pairs in a single file that share a canonical form
    but differ in raw form; feeds the warnings list.

Library choice: rapidfuzz (MIT) — already installed in the dagster container.
Do NOT substitute fuzzywuzzy (GPL).
"""

from __future__ import annotations

import re

# Separator characters to remove when producing the canonical form.
_SEP_RE = re.compile(r"[ \-_./]+")


def canonicalize(hole_id: str | None) -> str | None:
    """Produce the canonical join-key form of a hole ID.

    Rules (applied in order):
      - None, empty string, or whitespace-only input  → None
      - Strip leading/trailing whitespace
      - Remove separator characters: space, hyphen, underscore, dot, forward-slash
      - Uppercase

    Examples
    --------
    >>> canonicalize('LEB-23-001')
    'LEB23001'
    >>> canonicalize('leb_23_001')
    'LEB23001'
    >>> canonicalize('  LEB 23/001')
    'LEB23001'
    >>> canonicalize('')
    >>> canonicalize(None)
    """
    if hole_id is None:
        return None
    stripped = str(hole_id).strip()
    if not stripped:
        return None
    no_seps = _SEP_RE.sub("", stripped)
    if not no_seps:
        return None
    return no_seps.upper()


def fuzzy_match(
    target: str,
    candidates: list[str],
    threshold: float = 85.0,
) -> str | None:
    """Return the best-matching candidate (canonical form) from the list.

    Both *target* and each element of *candidates* are expected to be already
    in canonical form (i.e. already passed through :func:`canonicalize`).
    Running on canonical inputs means trivial formatting differences don't
    pollute the similarity score.

    Uses ``rapidfuzz.fuzz.ratio`` for the similarity score.  Returns the first
    candidate that scores >= *threshold*; ties are broken by list order (first
    match wins).  Returns None if no candidate meets the threshold.

    Parameters
    ----------
    target:
        Canonical form of the query hole ID.
    candidates:
        Canonical forms of the known hole IDs to match against.
    threshold:
        Minimum score (0–100) to accept a match.  Default 85.0 gives a
        comfortable margin above noise while tolerating a single character
        transposition.
    """
    if not candidates:
        return None

    from rapidfuzz import fuzz  # noqa: PLC0415 — deferred, not always needed

    best_score = -1.0
    best_candidate: str | None = None

    for candidate in candidates:
        score = fuzz.ratio(target, candidate)
        if score >= threshold and score > best_score:
            best_score = score
            best_candidate = candidate

    return best_candidate


def suggest_collisions(ids: list[str]) -> list[dict]:
    """Identify raw hole IDs in *ids* that canonicalize to the same form.

    Returns a list of collision dicts for pairs of DIFFERENT raw forms that
    share a canonical form.  Intended to populate the ``warnings`` list so a
    human reviewer can decide whether the two raw forms represent the same hole.

    Each dict has the shape::

        {
            "a":         str,    # first raw form
            "b":         str,    # second raw form
            "canonical": str,    # the shared canonical form
            "score":     float,  # rapidfuzz ratio(a, b) — informational
        }

    Only unique (a, b) pairs are returned (a < b alphabetically to avoid
    duplicates).  If two or more raw forms map to the same canonical, all
    C(n,2) pairs are reported.

    Parameters
    ----------
    ids:
        Raw hole ID strings from a single file (may include duplicates).
    """
    from rapidfuzz import fuzz  # noqa: PLC0415

    # Build canonical → set-of-raw-forms index
    canonical_map: dict[str, set[str]] = {}
    for raw in ids:
        c = canonicalize(raw)
        if c is None:
            continue
        canonical_map.setdefault(c, set()).add(raw)

    collisions: list[dict] = []
    for canonical, raw_set in canonical_map.items():
        if len(raw_set) < 2:
            continue
        # Sort for deterministic output and to satisfy a < b constraint
        sorted_raws = sorted(raw_set)
        for i, a in enumerate(sorted_raws):
            for b in sorted_raws[i + 1 :]:
                score = fuzz.ratio(a, b)
                collisions.append(
                    {
                        "a": a,
                        "b": b,
                        "canonical": canonical,
                        "score": round(score, 2),
                    }
                )

    return collisions
