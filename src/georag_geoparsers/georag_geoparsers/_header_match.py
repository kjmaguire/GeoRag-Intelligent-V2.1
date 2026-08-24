"""One header-matching rule, shared by every drill parser and the classifier.

## Why this exists

Header matching used to be ``if alias in set(csv_columns)`` — an exact,
case-sensitive string test against roughly seven hand-written aliases per
field. Measured against a real delivery on 2026-08-24, that rejected every
row of a collar table whose only sin was calling its key column ``Hole ID``
instead of ``Hole_ID``.

Worse, the two halves of the pipeline disagreed. ``_sheet_classifier`` had
always lower-cased before comparing; the parsers had not. A sheet could
therefore be correctly identified as collar data and then have all of its
rows thrown out by the writer that was handed it — which is exactly what
the "uploaded to the collar category ... accepted none of its rows" report
was describing. Both halves now call the same function, so a header the
classifier recognises is a header the parser can map.

## The rule

A header is reduced to its alphanumeric skeleton, so the spellings people
actually type all converge:

    Hole ID   HoleID   hole_id   HOLE-ID   Hole.Id      ->  holeid
    TotalDepth  Total_Depth  total depth                ->  totaldepth

Tokenisation splits on non-alphanumerics AND on camel-case boundaries,
which is what lets ``HoleID`` and ``hole_id`` meet in the middle. The
boundary rule has two halves: lower-to-upper (``holeID`` -> ``hole|ID``)
and an acronym running into a word (``IDNumber`` -> ``ID|Number``).
Without the second, ``DDHNumber`` would tokenise as one word.

A trailing unit token is then dropped, because unit-suffixed headers are
the norm in exported drill tables:

    Depth_m   Depth (m)   depth_metres   Depth_ft       ->  depth

Stripping happens on TOKENS, never on the joined string. Trimming a
trailing "m" off the characters of ``datum`` would leave ``datu``, and a
rule that mangles real field names to rescue unit suffixes is worse than
no rule. The last token is only dropped when it is a unit AND something
survives it, so a column genuinely named ``M`` is left alone.

Deliberately NOT normalised away: digits. ``from1``/``from2`` are distinct
columns in interval tables, and collapsing them would silently map two
columns onto one field.
"""

from __future__ import annotations

import re

#: Split points: a non-alphanumeric run, a lower/digit-to-upper transition,
#: or an acronym meeting a capitalised word.
_BOUNDARY = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

#: Unit tokens dropped from the END of a header.
#:
#: Length, angle and percentage only. Concentration units (ppm, ppb, oz/t)
#: are deliberately absent: in an assay table ``Au_ppm`` and ``Au_ppb`` are
#: different columns holding different numbers, and folding them together
#: would merge two grades into one.
_UNIT_TOKENS: frozenset[str] = frozenset({
    "m", "mm", "cm", "km",
    "metre", "metres", "meter", "meters",
    "ft", "feet", "foot",
    "deg", "degree", "degrees",
    "pct", "percent",
})


def normalize_header(header: str | None) -> str:
    """Reduce a column header to the skeleton used for alias comparison.

    Returns ``""`` for a header that is empty, ``None``, or made entirely
    of punctuation — callers treat that as "no header", never as a name
    that might match something.
    """
    if not header:
        return ""

    tokens = [t.lower() for t in _BOUNDARY.split(str(header).strip()) if t]
    if not tokens:
        return ""

    # Drop trailing units while a name survives them: "depth_m" -> "depth",
    # but "m" on its own stays "m".
    while len(tokens) > 1 and tokens[-1] in _UNIT_TOKENS:
        tokens.pop()

    return "".join(tokens)


def alias_skeletons(canonical: str, alias_list: list[str]) -> set[str]:
    """Every normalised spelling that means *canonical*.

    The canonical name is always one of them. Callers must go through this
    rather than normalising an alias list themselves: the classifier did
    the latter, so when ``_drill_schema`` stopped repeating each canonical
    inside its own alias list — redundant for ``build_column_map``, which
    adds it — a sheet whose header was spelled exactly ``lithology_code``
    silently stopped counting toward lithology's coverage.
    """
    return {
        skeleton
        for skeleton in (normalize_header(a) for a in (*alias_list, canonical))
        if skeleton
    }


def build_column_map(
    columns: list[str],
    aliases: dict[str, list[str]],
) -> tuple[dict[str, str], list[str]]:
    """Match source columns to canonical field names.

    Parameters
    ----------
    columns:
        The source file's header row, exactly as written.
    aliases:
        ``{canonical_name: [accepted spellings]}``. Order within each list
        is a preference: when a file carries two columns that both map to
        one field, the earlier alias wins.

    Returns
    -------
    (column_map, unmapped)
        ``column_map`` is ``{canonical: original_column_name}`` — the
        original spelling, not the normalised form, because the caller
        renames the dataframe with it.
        ``unmapped`` lists source columns no field claimed, in file order.

    A source column is claimed at most ONCE. Two canonical fields sharing
    an alias — ``Type`` means ``hole_type`` in a collar table and
    ``sample_type`` in a sample table — would otherwise both map to it,
    and the caller's ``{v: k for k, v in column_map.items()}`` rename
    would silently drop one of them. Iteration order of `aliases` decides
    who gets it, which is why these are dicts and not sets.
    """
    normalized = [(original, normalize_header(original)) for original in columns]

    column_map: dict[str, str] = {}
    claimed: set[str] = set()

    for canonical, alias_list in aliases.items():
        # Preference order is the alias list's order, with the canonical
        # name last: an explicit alias should win a tie against it.
        seen: set[str] = set()
        wanted = [
            skeleton
            for skeleton in (normalize_header(a) for a in (*alias_list, canonical))
            if skeleton and not (skeleton in seen or seen.add(skeleton))
        ]

        for want in wanted:
            match = next(
                (orig for orig, norm in normalized if norm == want and orig not in claimed),
                None,
            )
            if match is not None:
                column_map[canonical] = match
                claimed.add(match)
                break

    unmapped = [original for original in columns if original not in claimed]
    return column_map, unmapped
