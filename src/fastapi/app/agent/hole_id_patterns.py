"""Drill-hole identifier patterns, shared by everything that must not read
the digits inside a hole name as measurements.

These lived in ``viz_builder`` (which needs them to route a query to a collar
lookup), but Layer 6 needs the same patterns to MASK hole IDs before it scans
an answer for numbers — and importing viz_builder would drag the whole tool
layer into a validator. They are leaf definitions with no dependencies, so
they belong in their own module and both sides import from here.

Formats seen in real projects:

  PLS-20-01, GH08-212, SRE09-12, IC-11, XLS-24-01, DH-2547
      letters, optional embedded digits, then dash-separated digit groups
  0070-4850, 370-4850, 36-1085
      Gas Hills / Cameco style — numeric only, no letter prefix
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

#: Lettered IDs. Safe to match anywhere: the letter prefix makes a false
#: positive on ordinary prose unlikely.
#:
#: TWO letters minimum, deliberately. A single-letter prefix would pull
#: in "Figure A-1", "Table B-2" and "Appendix C-3", which are on nearly
#: every page of an NI 43-101. Layer 4 treats an unmatched hole ID as a
#: fabricated one — the single warning it grades critical on its own — so
#: a false positive there floors the answer's confidence and prints a
#: fabrication banner over a correct answer.
#:
#: Six digits per group rather than five: Layer 4's previous private
#: pattern allowed six, and narrowing a check while widening it
#: elsewhere is how coverage gets lost quietly.
HOLE_ID_RE = re.compile(
    r"\b([A-Z]{2,6}\d{0,4}-\d{1,6}(?:-\d{1,6})?)\b",
    re.IGNORECASE,
)

#: Numeric-only IDs. Bare digit ranges (depth intervals "20-30", page
#: numbers, hole counts) look identical, so callers must gate this on
#: HOLE_CONTEXT_RE matching somewhere in the same text rather than using it
#: unconditionally.
NUMERIC_HOLE_ID_RE = re.compile(
    r"\b(\d{1,4}-\d{1,5}(?:-\d{1,5})?)\b",
)

#: The gate for NUMERIC_HOLE_ID_RE. Deliberately not a tight lookbehind:
#: "this hole please tell me about it, 36-1085" puts the context word 30
#: characters away, and a strict adjacency rule drops the match.
HOLE_CONTEXT_RE = re.compile(
    r"\b(?:hole(?:\s*id)?s?|drill\s*holes?|drillholes?|ddh|borehole)\b",
    re.IGNORECASE,
)


#: Standard and regulatory designations whose digits are a NAME, not a
#: measurement and not a hole: "NI 43-101", "43-101F1", "National Instrument
#: 43-101", "S-K 1300", "JORC Code (2012)" is left alone (the year is a real
#: number a report may state). Every NI 43-101 answer names its standard, and
#: without this mask the digits leaked into three guards at once: Layer 3 read
#: 43 and 101 as numerical claims, Layer 6 tested them against whatever
#: constraint keyword sat nearby, and Layer 4 read "43-101" as a numeric
#: drill-hole ID whenever the answer also said "hole" — the one Layer 4
#: finding graded critical on its own (audit 2026-09-29, RAG-3).
DESIGNATION_RE = re.compile(
    r"\b(?:NI|National\s+Instrument|Form)\s*43-101(?:\s*F1|F1)?\b"
    r"|\b43-101\s*F1\b"
    r"|\bS-?K\s*1300\b",
    re.IGNORECASE,
)

# A numeric candidate immediately followed by one of these is a measured
# interval or quantity ("120-126 m", "50-60 %"), never a hole name.
_UNIT_AFTER_RE = re.compile(
    r"\s*(?:m|metres?|meters?|ft|feet|foot|km|cm|mm|%|g/t|ppm|ppb|°|deg(?:rees?)?)"
    r"(?![A-Za-z0-9/])",
    re.IGNORECASE,
)

# ...or immediately preceded by one of these words: page, figure, table,
# section and item references, and the interval prepositions ("from 120-126",
# "between 20-30"). Deliberately NOT "in" — "mineralisation in 36-1085" is the
# commonest way an answer names a Cameco hole.
_NON_HOLE_PRECEDER_RE = re.compile(
    r"\b(?:ni|instrument|form|pages?|pp|p|figs?|figures?|tables?|sections?|"
    r"items?|appendix|appendices|chapters?|plates?|from|to|between|intervals?|"
    r"depths?|years?)\s*\.?\s*$",
    re.IGNORECASE,
)

# 2011-2014 / 2011-14. Kept only when a hole-context word sits RIGHT before
# it ("hole 2011-14" is a legitimate year-sequence hole name); "hole 36-1085
# was drilled during 2011-2014" puts "hole" inside the ordinary window of a
# plain year range, so year ranges get a much tighter one.
_YEAR_RANGE_RE = re.compile(r"^(?:19|20)\d{2}-(?:(?:19|20)\d{2}|\d{2})$")
_YEAR_RANGE_CONTEXT_WINDOW = 3

#: How far before a numeric candidate a hole-context word may end and still
#: govern it. "holes 36-1085, 36-1042 and 36-1090" puts the third ID ~27
#: characters after "holes".
NUMERIC_HOLE_CONTEXT_WINDOW = 48


@dataclass(frozen=True)
class NumericHoleIdCandidate:
    """One bare-numeric token that may be a drill-hole ID.

    ``near_context`` is True when a hole-context word ("hole", "DDH",
    "borehole", ...) ends within :data:`NUMERIC_HOLE_CONTEXT_WINDOW`
    characters before it. Only near-context candidates are strong enough
    for Layer 4 to call a miss a fabricated hole; a far one is advisory.
    """

    value: str
    start: int
    end: int
    near_context: bool


def find_numeric_hole_ids(text: str) -> list[NumericHoleIdCandidate]:
    """Bare-numeric hole-ID candidates in ``text``, with the obvious
    non-hole shapes removed.

    Gated on HOLE_CONTEXT_RE matching somewhere in the text, exactly as the
    bare NUMERIC_HOLE_ID_RE always was. On top of that gate, a candidate is
    dropped when it is:

    * inside a standard designation ("NI 43-101") — :data:`DESIGNATION_RE`;
    * followed by a length / grade unit ("120-126 m");
    * preceded by a page / figure / table / section reference or an
      interval preposition ("from 120-126", "pages 12-14");
    * a year range with no hole word governing it ("2011-2014").

    Those were all reported by Layer 4 as critical fabricated drill holes on
    correct answers (RAG-3). A real numeric hole — "hole 36-1085" — passes
    every rule and is still checked.
    """
    if not HOLE_CONTEXT_RE.search(text):
        return []

    masked = [(m.start(), m.end()) for m in DESIGNATION_RE.finditer(text)]
    contexts = [m.end() for m in HOLE_CONTEXT_RE.finditer(text)]

    out: list[NumericHoleIdCandidate] = []
    for m in NUMERIC_HOLE_ID_RE.finditer(text):
        start, end = m.start(1), m.end(1)
        if any(ms < end and start < me for ms, me in masked):
            continue
        # Part of a decimal or a longer token ("145.2-148.0", "12-3a").
        if start > 0 and text[start - 1] in ".,":
            continue
        if end < len(text) and text[end] in ".," and text[end + 1: end + 2].isdigit():
            continue
        if _UNIT_AFTER_RE.match(text, end):
            continue
        if _NON_HOLE_PRECEDER_RE.search(text[max(0, start - 20):start]):
            continue
        value = m.group(1)
        near = any(0 <= start - c <= NUMERIC_HOLE_CONTEXT_WINDOW for c in contexts)
        if _YEAR_RANGE_RE.match(value) and not any(
            0 <= start - c <= _YEAR_RANGE_CONTEXT_WINDOW for c in contexts
        ):
            continue
        out.append(NumericHoleIdCandidate(value, start, end, near))
    return out


# ---------------------------------------------------------------------------
# What the lettered pattern matches that is not a hole, and the holes it misses
# (2026-10-10 audit, findings 7-9)
# ---------------------------------------------------------------------------
#
# HOLE_ID_RE is "two or more letters, a dash, digits", and ordinary prose is
# full of that shape: "Pre-2010", "Post-2015", "mid-2019", "Zone-3", "Lens-2",
# "Oct-2011", "ISO-9001", "Pb-206". Layer 4 treats an unmatched hole ID as a
# fabricated one -- critical on its own, which floors the answer's confidence
# and forces a retry -- and the retrieval side turns one into an assay filter
# that matches no rows and a forced factual_lookup intent.
#
# The pattern itself stays as it is (Layers 3 and 6 use it to MASK digits, where
# over-matching is harmless). Everything that ACTS on a match goes through
# iter_hole_id_matches() below, so the exclusions live in one place.

#: Lettered prefixes that are English words, dates or labels as well as hole
#: series, in the four ways such a token is told from a hole (2026-10-10 review,
#: item 5: one flat list made "CO-12" invisible on both sides -- retrieval
#: dropped a real hole, Layer 4 stopped checking "CO-99"). Matched against the
#: letters in front of the first digit, case-insensitively.
#:
#: * affixes: a year behind them makes the token a date ("pre-2010",
#:   "post-2015", "mid-2019"); anything else is a hole series ("CO-12",
#:   "SUB-3", "MID-5", "PRE-9").
_AFFIX_PREFIXES: frozenset[str] = frozenset((
    "pre", "post", "mid", "sub", "non", "co", "semi", "multi", "inter", "intra",
    "ultra", "anti",
))
#: * months: a year behind one makes a date ("Oct-2011"); "MAR-12" can be either
#:   (March 2012, or hole 12 of series MAR), so it is a hole to retrieval and a
#:   question for Layer 4's pool (`iter_ambiguous_hole_id_matches`).
_MONTH_PREFIXES: frozenset[str] = frozenset((
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec", "june", "july", "march", "april", "august",
))
#: * places: "Zone-3" is a zone and "ZONE-3" may be a hole. Retrieval takes the
#:   capitalised form for a hole; Layer 4 asks the pool whether the project has
#:   any hole of that series at all.
_LOCATION_PREFIXES: frozenset[str] = frozenset((
    "zone", "lens", "area", "block", "vein", "target", "lode", "seam", "bench",
    "stope", "grid", "line", "level",
))
#: * never a hole series: calendar and report labels, references, standards and
#:   datums ("Yr-2", "Phase-2", "Figure-3", "ISO-9001", "WGS-84").
_WORD_PREFIXES: frozenset[str] = frozenset((
    "yr", "year", "day", "week", "wk",
    "unit", "phase", "stage", "type", "class", "layer", "cycle", "series", "group",
    "step", "case", "item", "fig", "figure", "table", "tab", "page",
    "iso", "astm", "jorc", "cim", "csa", "nad", "wgs", "utm", "epsg", "srid", "crs",
))

#: Fiscal-year prefixes always carry their digits ("FY2021-22"), so unlike the
#: words above they are not a hole prefix even with digits embedded.
_NOT_HOLE_EVEN_WITH_DIGITS: frozenset[str] = frozenset(("fy", "cy"))

#: Two-letter element symbols, for isotope notation: "Pb-206", "Sr-87", "Nd-143".
#: Matched CASE-SENSITIVELY ("Pb", not "PB"): a hole series is written in capitals
#: ("PB-206"), a symbol is not.
_ELEMENT_SYMBOLS: frozenset[str] = frozenset((
    "He", "Li", "Be", "Ne", "Na", "Mg", "Al", "Si", "Cl", "Ar", "Ca", "Sc", "Ti",
    "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn", "Ga", "Ge", "As", "Se", "Br", "Kr",
    "Rb", "Sr", "Zr", "Nb", "Mo", "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn", "Sb",
    "Te", "Xe", "Cs", "Ba", "La", "Ce", "Pr", "Nd", "Sm", "Eu", "Gd", "Tb", "Dy",
    "Ho", "Er", "Tm", "Yb", "Lu", "Hf", "Ta", "Re", "Os", "Ir", "Pt", "Au", "Hg",
    "Tl", "Pb", "Bi", "Ra", "Rn", "Th",
))
_ISOTOPE_RE = re.compile(r"^([A-Z][a-z])-[1-9]\d{0,2}$")
_LEADING_LETTERS_RE = re.compile(r"[A-Za-z]+")
_FIRST_NUMBER_RE = re.compile(r"-(\d+)")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

#: A hole word this close in front of a token names it a hole whatever it
#: looks like ("hole CO-12", "drill hole Sub-3"), the same allowance a year
#: range gets for "hole 2011-14".
_NAMED_AS_HOLE_WINDOW = 3

# What a lettered match is, to the extractors below.
_HOLE = "hole"        # an ordinary series: always a hole
_WORD = "word"        # never a hole (unless a hole word names it)
_AFFIX = "affix"      # a series, unless a year follows (handled: that is _WORD)
_MONTH = "month"      # a date or a series: Layer 4 asks the pool
_LOCATION = "location"  # a place or a series: Layer 4 asks the pool


def _lettered_kind(token: str) -> str:
    """What a lettered ``HOLE_ID_RE`` match is: ``_HOLE``, ``_WORD``, ``_AFFIX``
    (a word-like prefix that is a series unless a year follows), ``_MONTH`` or
    ``_LOCATION``."""
    letters = _LEADING_LETTERS_RE.match(token)
    if letters is None:
        return _HOLE
    prefix = letters.group().lower()
    if prefix in _NOT_HOLE_EVEN_WITH_DIGITS:
        return _WORD
    isotope = _ISOTOPE_RE.match(token)
    if isotope and isotope.group(1) in _ELEMENT_SYMBOLS:
        return _WORD
    # "GH08-212", "SRE09-12": digits glued to the letters make a series name.
    if token[letters.end():letters.end() + 1].isdigit():
        return _HOLE
    first_number = _FIRST_NUMBER_RE.search(token)
    is_year = bool(first_number and _YEAR_RE.match(first_number.group(1)))
    if prefix in _WORD_PREFIXES:
        return _WORD
    if prefix in _AFFIX_PREFIXES:
        return _WORD if is_year else _AFFIX
    if prefix in _MONTH_PREFIXES:
        return _WORD if is_year else _MONTH
    if prefix in _LOCATION_PREFIXES:
        return _LOCATION
    return _HOLE


def _named_as_hole(start: int, contexts: list[int]) -> bool:
    return any(0 <= start - c <= _NAMED_AS_HOLE_WINDOW for c in contexts)


def iter_hole_id_matches(text: str, *, certain_only: bool = False) -> Iterator[re.Match[str]]:
    """``HOLE_ID_RE`` matches in ``text`` that can be a drill hole.

    Drops the lettered shapes that are not holes -- "Pre-2010", "Post-2015",
    "mid-2019", "Oct-2011", "Yr-2", "Phase-2", "ISO-9001", "Pb-206" -- unless a
    hole word sits right in front of the token ("hole SUB-3"). Real series
    ("DDH-1234", "BH-21", "PLS-22-08", "GH08-212", "SRE09-12", "IC-11") pass, and
    so do the ones that are also words: "CO-12", "SUB-3", "MID-5", "MAR-12".
    A word-like prefix directly followed by a measurement unit ("sub-3 g/t") is
    a figure, not a hole.

    ``ZONE-4`` and ``Zone-4``: a place name or a series. Retrieval, which has no
    pool to ask, takes the capitalised form for a hole and the title-case form
    for a place. ``certain_only=True`` (Layer 4) takes neither -- the answer
    side asks the pool about them instead (`iter_ambiguous_hole_id_matches`) --
    and also leaves out the months that are not followed by a year.

    Group 1 is the ID, as for ``HOLE_ID_RE.finditer``.
    """
    contexts = [m.end() for m in HOLE_CONTEXT_RE.finditer(text)]
    for match in HOLE_ID_RE.finditer(text):
        token = match.group(1)
        kind = _lettered_kind(token)
        if kind == _HOLE or _named_as_hole(match.start(1), contexts):
            yield match
            continue
        if kind == _WORD or _UNIT_AFTER_RE.match(text, match.end(1)):
            continue
        # A word-like prefix: an affix is a series unless a year follows (that
        # was _WORD above); a month or a place is left to Layer 4's pool when
        # ``certain_only``, and otherwise to the spelling.
        takes_it = (
            kind == _AFFIX
            or (not certain_only and kind == _MONTH)
            or (not certain_only and kind == _LOCATION and token.isupper())
        )
        if takes_it:
            yield match


def iter_ambiguous_hole_id_matches(text: str) -> Iterator[tuple[re.Match[str], str]]:
    """The tokens that are a word, a date or a place AND may be a hole series:
    a month with no year ("MAR-12"), a place with a number ("Zone-3", "ZONE-3").

    Yields ``(match, PREFIX)``. Only Layer 4 can tell them apart, by asking
    whether the project has any hole of that series; retrieval reads
    :func:`iter_hole_id_matches` instead. A token a hole word names is not
    ambiguous (it is a hole) and neither is one followed by a unit.
    """
    contexts = [m.end() for m in HOLE_CONTEXT_RE.finditer(text)]
    for match in HOLE_ID_RE.finditer(text):
        token = match.group(1)
        if _lettered_kind(token) not in (_MONTH, _LOCATION):
            continue
        if _named_as_hole(match.start(1), contexts) or _UNIT_AFTER_RE.match(text, match.end(1)):
            continue
        letters = _LEADING_LETTERS_RE.match(token)
        yield match, (letters.group().upper() if letters else "")


def find_lettered_hole_ids(text: str, *, certain_only: bool = False) -> list[str]:
    """The IDs of :func:`iter_hole_id_matches`, in order of appearance."""
    return [m.group(1) for m in iter_hole_id_matches(text, certain_only=certain_only)]


def find_ambiguous_hole_ids(text: str) -> list[tuple[str, str]]:
    """``(token, PREFIX)`` of :func:`iter_ambiguous_hole_id_matches`."""
    return [(m.group(1), prefix) for m, prefix in iter_ambiguous_hole_id_matches(text)]


#: Compact IDs: letters then digits with no separator -- "BH21", "DDH0023",
#: "SRE0912". HOLE_ID_RE requires a dash, so these were never extracted on
#: either side (finding 9), although identifier_boost recognises the shape.
#: Two digits minimum ("CO2", "SO4", "NO3" are formulas), and not the head of a
#: dashed ID ("SRE09" in "SRE09-12", which HOLE_ID_RE owns).
HOLE_ID_COMPACT_RE = re.compile(
    r"(?<![\w.-])([A-Z]{2,5}\d{2,7})(?!\w|-\d)",
    re.IGNORECASE,
)

#: Drill-type abbreviations. A compact token that starts with one is a hole
#: name wherever it appears: diamond (DDH, DD), reverse circulation (RC, RCD),
#: rotary air blast (RAB), aircore (AC), borehole (BH), drill hole (DH).
DRILL_TYPE_PREFIXES: frozenset[str] = frozenset(
    ("DDH", "DD", "DH", "BH", "RC", "RCD", "RAB", "AC", "CDH")
)

#: Any other compact token counts only with a hole word this close in front of
#: it ("holes SRE0912, SRE0913 and SRE0914" puts the third ~28 characters on).
COMPACT_HOLE_CONTEXT_WINDOW = 32

#: Letters that begin a compact token which is never a hole: standards,
#: datums, coordinate systems and fiscal shorthand ("NI43", "WGS84", "EPSG4326",
#: "NAD83", "UTM13", "ISO9001", "FY2021", "JORC2012").
_NOT_COMPACT_HOLE_PREFIXES: frozenset[str] = frozenset((
    "NI", "ISO", "JORC", "SK", "NAD", "WGS", "EPSG", "SRID", "UTM", "ITRF", "NTS",
    "CRS", "ASTM", "CSA", "CIM", "SEC", "NSR", "FY", "CY", "PH",
    # vertical and horizontal datums: "CGVD28", "NAVD88", "GDA2020", "MGA94"
    "CGVD", "NAVD", "NGVD", "GDA", "MGA", "ETRS", "GRS", "NZGD", "DIN",
))

#: A drill-type prefix in front of a year -- "RC2012 program results", "the
#: DD2021 campaign" -- names a program far more often than hole 2012 of a
#: series, so it counts as a hole only when a hole word names it ("hole RC2012").
_PROGRAM_YEAR_RE = re.compile(r"[A-Za-z]+(?:19|20)\d{2}")


#: Glue that may sit between a hole word and an ID, or between two IDs of a
#: list: connectives ("and", "or"), the words that label an ID ("id", "no.",
#: "number"), quotes, brackets, separators and a lone dash.
_ID_LIST_GLUE_RE = re.compile(
    r"\b(?:and|or|id|ids|no|nos|number|numbers)\b\.?"
    r"|[&#:,;/()\[\]'\"‘’“”]"
    r"|(?<!\S)[-–—]+(?!\S)",
    re.IGNORECASE,
)


def _only_ids_between(gap: str) -> bool:
    """Whether ``gap`` -- the text between a hole word and a token -- is
    nothing but IDs and the glue between them ("PLS-22-08, ", " and BH-1 ").

    "holes SRE0912, SRE0913 and SRE0914" is a list of holes; "hole PLS-22-08
    (sample MS240301)" is a hole followed by a clause about something else, and
    the sample ID in it is not a hole. Every piece left once the glue is
    removed must carry a digit: a word ("sample", "returned") ends the list.
    """
    return all(any(ch.isdigit() for ch in piece) for piece in _ID_LIST_GLUE_RE.sub(" ", gap).split())


def iter_compact_hole_id_matches(text: str) -> Iterator[re.Match[str]]:
    """Compact hole IDs ("BH21", "DDH0023", "SRE0912") in ``text``.

    The shape alone matches too much ("NI43", "WGS84", "ISO9001", a sample ID
    like "MS240301"), so a match must also either start with a drill-type
    abbreviation (:data:`DRILL_TYPE_PREFIXES`) or be named as a hole: follow a
    hole word within :data:`COMPACT_HOLE_CONTEXT_WINDOW` characters with
    nothing but IDs in between (:func:`_only_ids_between`) -- and never start
    with a standards / datum prefix. A drill-type prefix in front of a YEAR
    ("RC2012 program results", "the DD2021 campaign") is a program, so only a
    hole word makes it a hole (:data:`_PROGRAM_YEAR_RE`). Group 1 is the ID.
    """
    contexts = [m.end() for m in HOLE_CONTEXT_RE.finditer(text)]
    for match in HOLE_ID_COMPACT_RE.finditer(text):
        token = match.group(1)
        letters = _LEADING_LETTERS_RE.match(token)
        prefix = letters.group().upper() if letters else ""
        if prefix in _NOT_COMPACT_HOLE_PREFIXES:
            continue
        start = match.start(1)
        near = [c for c in contexts if 0 <= start - c <= COMPACT_HOLE_CONTEXT_WINDOW]
        program_year = _PROGRAM_YEAR_RE.fullmatch(token) is not None
        if (prefix in DRILL_TYPE_PREFIXES and not program_year) or (
            near and _only_ids_between(text[max(near):start])
        ):
            yield match


_CANONICAL_SEPARATORS_RE = re.compile(r"[\s\-_./]+")
_RUN_RE = re.compile(r"[A-Z]+|[0-9]+")


def canonical_hole_id(hole_id: str) -> str:
    """Separator-free, upper-cased form of a hole ID ("BH-12" -> "BH12").

    The same rule the ingest side writes into
    ``silver.collars.hole_id_canonical`` (las_ingester._canonical_hole_id,
    csv_collar_ingester.canonicalize), so "BH-12", "bh12" and "BH 12" are one
    hole. Leading zeros are deliberately NOT stripped: "BH-1" and "BH-01"
    may be different holes, and a guard must not merge two real holes.

    It also merges "PLS-2-28" with "PLS-22-8" (every separator is deleted).
    That is the price of agreeing with the ingest-written column, so use
    :func:`hole_id_key` wherever two ids are compared for IDENTITY in Python.
    """
    return _CANONICAL_SEPARATORS_RE.sub("", (hole_id or "").strip()).upper()


def hole_id_key(hole_id: str) -> str:
    """Separator-position-aware comparison key for a hole ID (audit item 24).

    :func:`canonical_hole_id` deletes every separator, so "PLS-2-28" and
    "PLS-22-8" are the SAME hole to it -- a fabricated id merged with a real
    one. This key keeps the one separator that carries information, the
    boundary between two digit groups, as a single "-", and drops the
    boundary between letters and digits (which writers vary freely):

        "PLS-22-08", "pls 22 08", "PLS22-08"  -> "PLS22-08"
        "PLS-2-28"                            -> "PLS2-28"   (a different hole)
        "BH-12", "BH12", "bh 12"              -> "BH12"
        "36-1085", "36 1085"                  -> "36-1085"
        "GH08-212"                            -> "GH08-212"

    Leading zeros are kept, as in :func:`canonical_hole_id` ("BH-1" and
    "BH-01" may be different holes). It is NOT what ``silver.collars.
    hole_id_canonical`` holds -- that column is written by ingestion with the
    separator-free rule, so SQL candidates are still fetched by
    :func:`canonical_hole_id` and then confirmed with this key in Python.
    (``tools.normalize_hole_id`` is the lookup-side twin that also drops
    leading zeros; that is a convenience match over a unique candidate, not
    an identity, which is why the two are deliberately not merged.)
    """
    runs = _RUN_RE.findall((hole_id or "").upper())
    out: list[str] = []
    prev_was_digits = False
    for run in runs:
        is_digits = run.isdigit()
        if out and is_digits and prev_was_digits:
            out.append("-")
        out.append(run)
        prev_was_digits = is_digits
    return "".join(out)
