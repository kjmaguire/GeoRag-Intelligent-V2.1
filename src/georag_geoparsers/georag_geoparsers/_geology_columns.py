"""Alteration and mineralization columns of a geology log, read by token.

## Why this is not alias matching

The other drill parsers map a header to a field through
``_header_match.normalize_header``, which folds case, separators and a
trailing unit token (``pct``, ``percent``, ``m``...). That is right for
``Depth_m`` and wrong here, because a geology log names a mineral and its
percentage with the SAME stem::

    Mineral1     Mineral1_Pct     Min1     Min1_%     Sulphide     Sulphide%

``normalize_header`` reduces ``Min1_%`` and ``Mineral1_Pct`` to the skeletons
``min1`` and ``mineral1`` - the same skeletons as the NAME columns beside them.
An alias table would then pair the name column with whichever of the two the
file happened to list first, and swap a mineral with its percentage in a file
that lists them the other way round. Nothing would raise.

So this module reads the header's tokens (with ``%`` kept as a token), and a
column's meaning is ``(family, role, slot)``:

    family   alteration | mineralization
    role     what the column holds (type, intensity, pct, style ...)
    slot     1..4 for ``Alt2`` / ``Mineral3_Pct``; 0 when the header carries
             no number (``Alteration``, ``Min_Style``)

## What counts as evidence

Only STRONG headers can make a table an alteration or mineralization table (the
classifier reads :func:`has_family_evidence`). A strong header names the family
itself - ``Alteration``, ``Alt_Type``, ``Mineral1``, ``Mineralization``,
``Sulphide%``. WEAK headers (``Minerals``, ``Intensity``, ``Style``, ``Comments``)
are what half the drill vocabulary calls its own attributes; they are read only
after a table is already known to hold the family, never as the reason it does.

## What is deliberately not recognised

* bare ``Min`` / ``Alt``-as-altitude ambiguity: ``Min`` alone is a minimum, so a
  mineral column needs ``Mineral`` or a slot number (``Min1``). ``Alt`` alone IS
  read as alteration - it is the standard short header in logging templates -
  and only alongside hole and depth columns (the classifier requires both).
* ``Alteration_Weathering`` is the lithology parser's weathering alias and is
  not touched here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from georag_geoparsers._header_match import _BOUNDARY

#: Numbered columns beyond this are not slots (``Alt_2023`` is not slot 2023).
MAX_SLOTS = 4

FAMILY_ALTERATION = "alteration"
FAMILY_MINERALIZATION = "mineralization"

# Roles ---------------------------------------------------------------------
ROLE_NAME = "name"            # alteration type / mineral name
ROLE_INTENSITY = "intensity"
ROLE_MINERALS = "minerals"    # alteration minerals (text[])
ROLE_NOTES = "notes"
ROLE_STYLE = "style"          # alteration style: no column, kept in notes
ROLE_PCT = "pct"              # mineral abundance %
ROLE_FORM = "form"            # mineral form / habit / style
ROLE_GRAIN = "grain"          # mineral grain size
ROLE_GROUP_PCT = "group_pct"  # "Sulphide%": a percentage of a mineral GROUP

#: Roles whose value belongs to ONE alteration / mineral of the interval. An
#: unnumbered one beside several numbered names cannot be assigned to a slot,
#: and is kept (in notes, with a warning) rather than given to slot 1 by guess.
SLOT_LEVEL_ROLES: frozenset[str] = frozenset({ROLE_INTENSITY, ROLE_PCT})

_ALT_PREFIXES = frozenset({"alteration", "alt"})
_MIN_STRONG_PREFIXES = frozenset({"mineral", "mineralization", "mineralisation"})
_GROUP_PREFIXES = frozenset({"sulphide", "sulfide", "sulphides", "sulfides"})

_NAME_WORDS = frozenset({"type", "code", "name", "species"})
_INTENSITY_WORDS = frozenset({"intensity", "int", "intens"})
_NOTES_WORDS = frozenset({
    "comments", "comment", "notes", "note", "remarks", "description", "desc",
})
_ALT_MINERAL_WORDS = frozenset({"mineral", "minerals", "mineralogy", "assemblage"})
_FORM_WORDS = frozenset({"style", "form", "habit"})
_PCT_FORMS: frozenset[tuple[str, ...]] = frozenset({
    ("pct",), ("percent",), ("percentage",), ("perc",), ("abundance",),
    ("abundance", "pct"), ("abundance", "percent"),
})
_GRAIN_FORMS: frozenset[tuple[str, ...]] = frozenset({
    ("grain",), ("grainsize",), ("grain", "size"), ("size",),
})

#: Spellings that carry only the FAMILY of the table, used once the table is
#: known to hold it. Joined-token form (``normalize_header``-like, no units).
WEAK_ALTERATION: dict[str, frozenset[str]] = {
    ROLE_INTENSITY: frozenset({"intensity", "int", "strength"}),
    ROLE_MINERALS: frozenset({"minerals", "mineralogy", "mineral", "assemblage"}),
    ROLE_NOTES: frozenset({
        "comments", "comment", "notes", "remarks", "description", "desc",
    }),
    ROLE_STYLE: frozenset({"style"}),
}
WEAK_MINERALIZATION: dict[str, frozenset[str]] = {
    ROLE_NAME: frozenset({"minerals", "mineralogy", "mineralname", "mineraltype"}),
    ROLE_PCT: frozenset({"pct", "percent", "percentage", "abundance"}),
    ROLE_FORM: frozenset({"style", "form", "habit"}),
    ROLE_GRAIN: frozenset({"grain", "grainsize"}),
    ROLE_INTENSITY: frozenset({"intensity", "int"}),
    ROLE_NOTES: frozenset({
        "comments", "comment", "notes", "remarks", "description", "desc",
    }),
}

#: A cell that states there is nothing to record. Not data: the row is
#: skipped, not written as a mineral called "Nil".
NONE_TOKENS_ALTERATION = frozenset({
    "nil", "none", "no", "n/a", "na", "nd", "n.d.", "-", "--", "not observed",
    "no alteration", "unaltered",
})
NONE_TOKENS_MINERALIZATION = frozenset({
    "nil", "none", "no", "n/a", "na", "nd", "n.d.", "-", "--", "not observed",
    "no mineralization", "no mineralisation", "no visible mineralization",
})


@dataclass(frozen=True)
class FamilyColumn:
    """What one header means to a family."""

    family: str
    role: str
    slot: int = 0
    strong: bool = True
    #: The header names the family, not a mineral (``Mineralization``): its
    #: cell holds free text, which is stored verbatim and reported.
    free_text: bool = False
    #: For ROLE_GROUP_PCT: the group the percentage is of, as the header spells it.
    group: str | None = None


def header_tokens(header: str | None) -> list[str]:
    """Lower-case tokens of a header, with ``%`` kept as the token ``pct``."""
    if not header:
        return []
    text = str(header).replace("%", " pct ")
    tokens: list[str] = []
    for part in _BOUNDARY.split(text.strip()):
        if part:
            tokens.extend(re.findall(r"[a-z]+|\d+", part.lower()))
    return tokens


def _split_slot(rest: list[str]) -> tuple[int, list[str]] | None:
    """(slot, remaining tokens), or None when a number is not a slot."""
    digits = [t for t in rest if t.isdigit()]
    if len(digits) > 1:
        return None
    if not digits:
        return 0, list(rest)
    slot = int(digits[0])
    if not 1 <= slot <= MAX_SLOTS or len(digits[0]) > 1:
        return None
    return slot, [t for t in rest if not t.isdigit()]


def _alteration_column(tokens: list[str]) -> FamilyColumn | None:
    if not tokens or tokens[0] not in _ALT_PREFIXES:
        return None
    split = _split_slot(tokens[1:])
    if split is None:
        return None
    slot, rest = split
    fam = FAMILY_ALTERATION
    if not rest or (len(rest) == 1 and rest[0] in _NAME_WORDS):
        return FamilyColumn(fam, ROLE_NAME, slot)
    if len(rest) == 1:
        word = rest[0]
        if word in _INTENSITY_WORDS:
            return FamilyColumn(fam, ROLE_INTENSITY, slot)
        if word in _ALT_MINERAL_WORDS:
            return FamilyColumn(fam, ROLE_MINERALS, slot)
        if word in _NOTES_WORDS:
            return FamilyColumn(fam, ROLE_NOTES, slot)
        if word == "style":
            return FamilyColumn(fam, ROLE_STYLE, slot)
    return None


def _mineralization_column(tokens: list[str]) -> FamilyColumn | None:
    if not tokens:
        return None
    fam = FAMILY_MINERALIZATION
    head = tokens[0]

    # "Sulphide%" / "Total_Sulphide_Pct": a percentage of a GROUP. Bare
    # "Sulphide" is not read: it may hold text or a number.
    body = tokens[1:] if head == "total" else tokens
    if body and body[0] in _GROUP_PREFIXES and tuple(body[1:]) in _PCT_FORMS:
        return FamilyColumn(fam, ROLE_GROUP_PCT, 0, group=body[0])

    if head in _MIN_STRONG_PREFIXES:
        strong = True
    elif head == "min":
        strong = True  # only reached with a slot or a role word, see below
    else:
        return None

    split = _split_slot(tokens[1:])
    if split is None:
        return None
    slot, rest = split
    free = head in {"mineralization", "mineralisation"}

    # An UNNUMBERED "Min_..." is easy to misread: Min_Pct, Min_Int and Min_Size
    # are as likely to be a minimum as a mineral's percentage, intensity or
    # size. Those need a slot number (Min1_Pct); only the words that have no
    # such second reading are accepted bare.
    bare_min = head == "min" and slot == 0

    if not rest:
        if bare_min:
            return None  # a bare "Min" is a minimum
        return FamilyColumn(fam, ROLE_NAME, slot, strong, free_text=free)
    if len(rest) == 1 and rest[0] in _NAME_WORDS:
        return FamilyColumn(fam, ROLE_NAME, slot, strong, free_text=False)
    if tuple(rest) in _PCT_FORMS and not bare_min:
        return FamilyColumn(fam, ROLE_PCT, slot, strong)
    if len(rest) == 1 and rest[0] in _FORM_WORDS:
        return FamilyColumn(fam, ROLE_FORM, slot, strong)
    if tuple(rest) in _GRAIN_FORMS and not (bare_min and rest == ["size"]):
        return FamilyColumn(fam, ROLE_GRAIN, slot, strong)
    if len(rest) == 1 and rest[0] in _INTENSITY_WORDS and not bare_min:
        return FamilyColumn(fam, ROLE_INTENSITY, slot, strong)
    if len(rest) == 1 and rest[0] in _NOTES_WORDS:
        return FamilyColumn(fam, ROLE_NOTES, slot, strong)
    return None


def classify_header(header: str | None, family: str) -> FamilyColumn | None:
    """The STRONG meaning of *header* for *family*, or ``None``."""
    tokens = header_tokens(header)
    if family == FAMILY_ALTERATION:
        return _alteration_column(tokens)
    if family == FAMILY_MINERALIZATION:
        return _mineralization_column(tokens)
    raise ValueError(f"unknown geology family {family!r}")


def weak_column(header: str | None, family: str) -> FamilyColumn | None:
    """The WEAK meaning of *header* (family attributes only), or ``None``."""
    joined = "".join(header_tokens(header))
    table = WEAK_ALTERATION if family == FAMILY_ALTERATION else WEAK_MINERALIZATION
    for role, words in table.items():
        if joined in words:
            return FamilyColumn(family, role, 0, strong=False)
    return None


def has_family_evidence(headers: list[str], family: str) -> bool:
    """True when *headers* name the family explicitly.

    Alteration needs a type column (``Alteration``, ``Alt_Type``, ``Alt1``...).
    Mineralization needs a mineral column (``Mineral``, ``Mineral1``, ``Min1``,
    ``Mineralization``) or a group percentage (``Sulphide%``). An intensity or
    a percentage on its own is not evidence: it does not say of what.
    """
    for header in headers:
        col = classify_header(header, family)
        if col is None:
            continue
        if col.role == ROLE_NAME or col.role == ROLE_GROUP_PCT:
            return True
    return False


def is_none_token(value: str, family: str) -> bool:
    """Whether a cell says there is nothing to record."""
    tokens = (
        NONE_TOKENS_ALTERATION if family == FAMILY_ALTERATION
        else NONE_TOKENS_MINERALIZATION
    )
    return " ".join(value.split()).casefold() in tokens


__all__ = [
    "FAMILY_ALTERATION",
    "FAMILY_MINERALIZATION",
    "MAX_SLOTS",
    "ROLE_FORM",
    "ROLE_GRAIN",
    "ROLE_GROUP_PCT",
    "ROLE_INTENSITY",
    "ROLE_MINERALS",
    "ROLE_NAME",
    "ROLE_NOTES",
    "ROLE_PCT",
    "ROLE_STYLE",
    "SLOT_LEVEL_ROLES",
    "FamilyColumn",
    "classify_header",
    "has_family_evidence",
    "header_tokens",
    "is_none_token",
    "weak_column",
]
