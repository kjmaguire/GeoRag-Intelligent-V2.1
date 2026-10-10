"""Hole ID canonicalization and fuzzy matching helpers.

Provides:
  - canonicalize(hole_id) — strip separators, uppercase, return None for blank.
  - fuzzy_match(target, candidates, threshold) — best rapidfuzz match above threshold.
  - suggest_collisions(ids) — pairs in a single file that share a canonical form
    but differ in raw form; feeds the warnings list.
  - split_duplicate_holes(records) — keeps the first row of each canonical id and
    returns the repeats, so a hole listed twice is reported instead of the last
    row silently replacing the first (duplicate_hole_warning /
    duplicate_hole_skip_entry shape the report).

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

    from rapidfuzz import fuzz

    best_score = -1.0
    best_candidate: str | None = None

    for candidate in candidates:
        score = fuzz.ratio(target, candidate)
        if score >= threshold and score > best_score:
            best_score = score
            best_candidate = candidate

    return best_candidate


#: Fields compared to tell a harmless repeat from a conflicting one.
_DUPLICATE_COMPARE_FIELDS = (
    "easting", "northing", "elevation", "total_depth", "azimuth", "dip",
)
#: How many duplicate rows a ``duplicate_hole_id`` warning lists.
_DUPLICATES_LISTED = 20
_DUPLICATES_QUOTED = 5


def split_duplicate_holes(
    records: list[dict],
) -> tuple[list[dict], list[dict]]:
    """``(kept, duplicates)`` - the first row of each canonical hole id wins.

    A collar file that lists a hole twice (the same spelling or ``DH-1`` and
    ``dh_1``) used to reach the database as two upserts onto one collar, so
    the LAST row silently replaced the first one's coordinates and depth.
    Which row is right cannot be known from the file, so the first is kept
    deliberately and every later one is returned in ``duplicates`` for the
    caller to report (:func:`duplicate_hole_warning`), with both rows'
    positions and coordinates. Records with no canonical id are never
    duplicates of each other (they are rejected elsewhere).

    Each duplicate is ``{"row", "hole_id", "first_row", "first_hole_id",
    "easting", "northing", "first_easting", "first_northing", "identical"}``;
    ``identical`` is True when every compared field matches the kept row, so
    nothing was lost by dropping it.
    """
    kept: list[dict] = []
    first_by_canonical: dict[str, dict] = {}
    duplicates: list[dict] = []
    for rec in records:
        canonical = rec.get("hole_id_canonical") or canonicalize(rec.get("hole_id"))
        first = first_by_canonical.get(canonical) if canonical else None
        if first is None:
            kept.append(rec)
            if canonical:
                first_by_canonical[canonical] = rec
            continue
        duplicates.append(duplicate_of(rec, first))
    return kept, duplicates


def duplicate_of(rec: dict, first: dict) -> dict:
    """Describe *rec* as a repeat of the earlier collar row *first*."""
    return {
        "row": rec.get("_source_row"),
        "hole_id": rec.get("hole_id"),
        "first_row": first.get("_source_row"),
        "first_hole_id": first.get("hole_id"),
        "easting": rec.get("easting"),
        "northing": rec.get("northing"),
        "first_easting": first.get("easting"),
        "first_northing": first.get("northing"),
        "identical": all(
            rec.get(f) == first.get(f) for f in _DUPLICATE_COMPARE_FIELDS
        ),
    }


def duplicate_hole_skip_entry(dup: dict) -> dict:
    """A duplicate in the parsers' ``skipped_details`` shape."""
    return {
        "row": dup["row"],
        "code": "duplicate_hole_id",
        "reason": (
            f"row {dup['row']}: hole {dup['hole_id']!r} repeats row "
            f"{dup['first_row']} ({dup['first_hole_id']!r}); the first was kept"
        ),
        "raw": {
            "hole_id": dup["hole_id"],
            "easting": dup["easting"],
            "northing": dup["northing"],
        },
        "expected": "one row per hole",
        "actual": {"first_row": dup["first_row"]},
        "suggestion": (
            "Remove the repeated hole from the file, or give the two holes "
            "distinct ids, and upload it again."
        ),
    }


def _coords(easting: object, northing: object) -> str:
    def fmt(value: object) -> str:
        # Not ``:g`` - six significant digits would print a UTM northing of
        # 6000999 as 6.001e+06 and hide the very difference being reported.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{value:.6f}".rstrip("0").rstrip(".")
        return str(value)

    return f"E {fmt(easting)}, N {fmt(northing)}"


def duplicate_hole_warning(duplicates: list[dict], *, label: str | None = None) -> dict | None:
    """The one ``duplicate_hole_id`` warning for *duplicates*, or None.

    Names the rows and their coordinates, the kept row's too, so a person can
    see whether the repeat was harmless (identical) or two different positions
    for one hole id. ``severity: info`` only when every repeat was identical
    to the row that was kept.
    """
    if not duplicates:
        return None
    conflicting = [d for d in duplicates if not d["identical"]]
    quoted = "; ".join(
        f"row {d['row']} {d['hole_id']!r} ({_coords(d['easting'], d['northing'])}) "
        f"repeats row {d['first_row']} "
        f"({_coords(d['first_easting'], d['first_northing'])})"
        + ("" if d["identical"] else " and differs")
        for d in duplicates[:_DUPLICATES_QUOTED]
    )
    more = len(duplicates) - min(len(duplicates), _DUPLICATES_QUOTED)
    where = f" in {label}" if label else ""
    warning: dict = {
        "row": None,
        "code": "duplicate_hole_id",
        "message": (
            f"{len(duplicates)} collar row(s){where} repeat a hole id already "
            f"in the file; the first of each was kept"
        ),
        "detail": (
            f"{len(duplicates)} collar row(s){where} name a hole that an earlier "
            f"row already gave (the same id, or one that differs only in "
            f"separators or case): {quoted}"
            + (f" (and {more} more)" if more else "")
            + ". "
            + (
                f"{len(conflicting)} of them disagree with the row that was "
                f"kept - nothing here says which position is right, so the "
                f"FIRST row of each hole was kept and the later ones were not "
                f"stored. "
                if conflicting else
                "They match the row that was kept exactly, so nothing was lost. "
            )
            + "Remove the repeats, or give the holes distinct ids, and "
            "upload the file again if the kept row is not the right one."
        )[:900],
        "context": {
            "count": len(duplicates),
            "conflicting": len(conflicting),
            "rows": [
                {k: d[k] for k in ("row", "hole_id", "first_row", "easting", "northing", "identical")}
                for d in duplicates[:_DUPLICATES_LISTED]
            ],
        },
    }
    if not conflicting:
        warning["severity"] = "info"
    return warning


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
    from rapidfuzz import fuzz

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
