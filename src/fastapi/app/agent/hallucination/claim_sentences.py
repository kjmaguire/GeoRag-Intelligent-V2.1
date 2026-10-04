"""Sentence units shared by the guards that remove claims (§04i Layers 2, 5).

Three guards decide claim-by-claim whether a sentence may ship:

* Layer 2 (``layer2_typed_output``) drops a sentence whose only citation
  marker was invented (a marker with no Citation behind it);
* the CLAUDE.md rule-4 enforcer in the same module drops a substantive
  sentence that carries no citation at all;
* Layer 5's gate (``layer5_provenance``) drops a sentence whose only
  citation was rejected on provenance.

They used to share one private regex, ``(?<=[.!?])\\s+``, duplicated in two
modules, and each rejoined the survivors with single spaces. That had two
costs (audit 2026-09-29, RISK-4):

* "approx. 12 m", "Fig. 3" and "e.g. the" were sentence ends, so a claim
  could be cut in half and each half judged separately -- over- or
  under-deleting depending on which half held the marker.
* Rejoining with " " flattened the answer's markdown: headings, bullets,
  tables and the "### Conflicting evidence" section that
  ``conflict_extraction`` parses back out all collapsed onto one line.

This module splits once, keeps every separator, and puts the text back
together with only the dropped units removed.

No NLP dependency, deliberately -- same trade-off the guards already made.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.agent.hallucination.citation_markers import (
    ALL_MARKER_RE,
    CITATION_MARKER_CAPTURE_RE,
    canonical_marker,
)

#: "." after one of these is not a sentence end. Lower-cased, without the
#: trailing dot. Chosen for geological report prose; words that routinely END
#: a sentence ("etc", "Inc", "Ltd", "Corp", "Mt") are deliberately absent --
#: missing a boundary merges two claims, which is the milder error.
_ABBREVIATIONS: frozenset[str] = frozenset((
    "approx", "ca", "cf", "e.g", "eg", "i.e", "ie", "viz", "vs", "al",
    "fig", "figs", "no", "nos", "vol", "vols", "p", "pp", "sec", "secs",
    "sect", "eq", "eqs", "ref", "refs", "incl", "est", "dr", "mr", "mrs",
    "ms", "prof", "resp", "tab",
))

# A candidate boundary: sentence punctuation (plus any closing brackets,
# quotes or emphasis) followed by whitespace, or a bare line break.
_BOUNDARY_RE = re.compile(
    r"([.!?][)\]\"'”’*_]*)([ \t]*\n\s*|[ \t]+)|(\n\s*)"
)
_WORD_BEFORE_RE = re.compile(r"([A-Za-z][A-Za-z.]*)$")

_LIST_PREFIX_RE = re.compile(r"^(\s*(?:[-*+•]|\d{1,3}[.)]|\([OI]\d+\))\s+)")
_LIST_ONLY_LINE_RE = re.compile(r"(?m)^[ \t]*(?:[-*+•]|\d{1,3}[.)])[ \t]*(?:\n|$)")


@dataclass
class Unit:
    """One sentence (or line) of an answer and the whitespace after it."""

    text: str
    sep: str


def split_units(text: str) -> list[Unit]:
    """Split ``text`` into sentence units, keeping every separator.

    ``"".join(u.text + u.sep for u in split_units(t)) == t`` always holds.

    A unit ends at ``.``/``!``/``?`` followed by whitespace -- unless the
    word before the ``.`` is a known abbreviation, or the next character is
    lower-case (neither ends a sentence) -- and at every line break, so
    headings, bullets and table rows are units of their own. A unit that is
    nothing but citation markers ("[NI43-1]" after "Claim.") is folded back
    onto the unit before it: it is that sentence's citation, not a sentence.
    """
    units: list[Unit] = []
    pos = 0
    for m in _BOUNDARY_RE.finditer(text):
        if m.group(3) is not None:  # bare line break
            end_text, sep = m.start(3), m.group(3)
        else:
            punct, ws = m.group(1), m.group(2)
            if "\n" not in ws:
                if punct.startswith("."):
                    before = _WORD_BEFORE_RE.search(text[pos:m.start(1)])
                    if before and before.group(1).lower().rstrip(".") in _ABBREVIATIONS:
                        continue
                if text[m.end():m.end() + 1].islower():
                    continue
            end_text, sep = m.start(1) + len(punct), ws
        units.append(Unit(text[pos:end_text], sep))
        pos = m.end()
    if pos < len(text) or not units:
        units.append(Unit(text[pos:], ""))

    folded: list[Unit] = []
    for unit in units:
        if folded and is_marker_only(unit.text):
            prev = folded[-1]
            prev.text = f"{prev.text}{prev.sep}{unit.text}"
            prev.sep = unit.sep
        else:
            folded.append(unit)
    return folded


def join_units(units: Iterable[Unit]) -> str:
    """Inverse of :func:`split_units`."""
    return "".join(u.text + u.sep for u in units)


def is_marker_only(piece: str) -> bool:
    """True when ``piece`` is nothing but citation marker(s) and whitespace."""
    stripped = piece.strip()
    return bool(stripped) and not ALL_MARKER_RE.sub("", stripped).strip()


def marker_ids(piece: str) -> list[str]:
    """Canonical (dash-form) ids of the numeric markers in ``piece``."""
    return [
        canonical_marker(m.group(1), m.group(3))
        for m in CITATION_MARKER_CAPTURE_RE.finditer(piece)
    ]


def strip_list_prefix(piece: str) -> str:
    """``piece`` without a leading bullet / list number / OIUR tag."""
    return _LIST_PREFIX_RE.sub("", piece, count=1)


def drop_units(
    text: str,
    should_drop: Callable[[int, list[Unit]], bool],
    *,
    rewrite: Callable[[int, list[Unit]], str] | None = None,
    proactive_insights_offset: int | None = None,
) -> tuple[str, int | None, int]:
    """Remove the units ``should_drop`` selects, preserving the rest verbatim.

    ``rewrite`` (optional) returns replacement text for a KEPT unit -- Layer 5
    uses it to cut a rejected marker out of a sentence that also carries a
    valid one.

    Only the LLM-written head of ``text`` is touched: the deterministic
    proactive-insights block after ``proactive_insights_offset`` is system
    output and passes through unchanged, and the returned offset is moved to
    the new boundary. (Removing text in front of the old offset without
    moving it left every later reader of the offset cutting in the wrong
    place.)

    Returns ``(new_text, new_offset, dropped_count)``. When nothing is
    dropped or rewritten the input text is returned unchanged.
    """
    offset = proactive_insights_offset
    if offset is not None and 0 <= offset <= len(text):
        head, tail = text[:offset], text[offset:]
    else:
        head, tail, offset = text, "", None

    units = split_units(head)
    kept: list[Unit] = []
    dropped = 0
    changed = False
    for i, unit in enumerate(units):
        if unit.text.strip() and should_drop(i, units):
            dropped += 1
            prefix = _LIST_PREFIX_RE.match(unit.text)
            if prefix and "\n" not in unit.sep and i + 1 < len(units):
                # The next unit shares this bullet's line: keep the bullet.
                kept.append(Unit(prefix.group(1), ""))
            elif kept and unit.sep.count("\n") > kept[-1].sep.count("\n"):
                kept[-1].sep = unit.sep
            continue
        if rewrite is not None:
            new_text = rewrite(i, units)
            if new_text != unit.text:
                changed = True
                unit = Unit(new_text, unit.sep)
        kept.append(unit)

    if not dropped and not changed:
        return text, proactive_insights_offset, 0

    new_head = _LIST_ONLY_LINE_RE.sub("", join_units(kept))
    new_head = re.sub(r"[ \t]{2,}", " ", new_head).strip()
    if offset is None:
        return new_head, None, dropped
    # Keep the head/insights separation the assembler wrote.
    gap = head[len(head.rstrip()):] if new_head else ""
    new_head = f"{new_head}{gap}"
    return new_head + tail, len(new_head), dropped


# ---------------------------------------------------------------------------
# Which units are claims
# ---------------------------------------------------------------------------

#: Phrases that make a sentence a refusal or a statement about the evidence
#: rather than a claim about the geology. Matched anywhere, lower-cased.
_NON_CLAIM_PHRASES: tuple[str, ...] = (
    # refusals (response_assembler._REFUSAL_PHRASES_ANYWHERE shapes)
    "i don't have", "i do not have", "don't have data", "do not have data",
    "i can only answer", "only geological questions", "not a possible value",
    "physically impossible", "i was unable to generate", "i cannot find",
    "i can't find", "i could not find", "i couldn't find",
    "the language model is currently unavailable", "please try again",
    "no data found", "no records found", "no results found",
    "insufficient information", "no information is available",
    # statements about the evidence set, which the prompt asks the model to
    # make when retrieval does not cover the question
    "provided evidence does not", "evidence provided does not",
    "retrieved evidence does not", "evidence set does not",
    "retrieved passages do not", "passages provided do not",
    "passages do not address", "passages do not cover",
    "not covered by the retrieved", "not covered in the retrieved",
    "does not support answering", "no disagreement found",
    "not applicable",
    # requests for clarification
    "could you clarify", "please clarify", "could you rephrase",
    "please rephrase", "try rephrasing", "let me know", "would you like",
)

#: Pointers to other material. Never claims, whatever they contain.
_POINTER_STARTERS: tuple[str, ...] = (
    "see table", "see figure", "see fig", "see section", "see appendix",
    "see page", "refer to", "for more detail", "for further",
)

#: Connectives that introduce a restatement. Exempt only when they carry no
#: number -- "In summary, the resource is 48.2 Mt" is a claim.
_CONNECTIVE_STARTERS: tuple[str, ...] = (
    "in summary", "in conclusion", "to summarize", "to summarise",
    "overall", "note that", "please note", "as noted", "as shown",
)

#: Sentences about the search or the answer itself ("I found passages about
#: QA/QC but nothing on metallurgy", "Here is what the reports say",
#: "I hope this helps") -- exempt when they carry no number. First person is
#: the model talking about its own work, not about the geology.
_META_STARTERS: tuple[str, ...] = (
    "i ", "i'", "the retrieved passages", "the retrieved documents",
    "the passages", "the evidence set", "the provided evidence",
    "the retrieved evidence", "here is", "here are", "here's", "below is",
    "below are", "the following", "the table below", "this answer",
    "this summary",
)

_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
_RULE_OR_TABLE_SEPARATOR_RE = re.compile(r"^[\s|:\-*_=+]+$")
_NUMERAL_RE = re.compile(r"\d")
_EMPHASIS_WRAPPED_RE = re.compile(r"^(\*\*|__|\*|_)(?P<body>.+?)\1:?$")


def _has_measurement(body: str) -> bool:
    """Whether ``body`` carries a numeral outside hole names and designations."""
    from app.agent.hole_id_patterns import (  # noqa: PLC0415
        DESIGNATION_RE,
        HOLE_ID_RE,
        find_numeric_hole_ids,
    )

    masked = DESIGNATION_RE.sub(" ", body)
    for cand in sorted(find_numeric_hole_ids(masked), key=lambda c: c.start, reverse=True):
        masked = masked[: cand.start] + " " + masked[cand.end:]
    masked = HOLE_ID_RE.sub(" ", masked)
    return bool(_NUMERAL_RE.search(masked))


def is_non_claim(unit_text: str) -> bool:
    """True when this unit makes no factual claim that needs a citation.

    Covers structure (headings, rules, table separators, "Label:" lines,
    short bold/italic labels), questions, refusals and hedges, pointers
    ("See Table 14-3"), number-free connectives and number-free statements
    about the search. Everything else is a claim.
    """
    text = ALL_MARKER_RE.sub("", unit_text).strip()
    if len(text) < 5:
        return True
    if _HEADING_RE.match(text) or _RULE_OR_TABLE_SEPARATOR_RE.match(text):
        return True
    body = strip_list_prefix(text).strip()
    if not body or body.endswith((":", "?")):
        return True
    has_number = bool(_NUMERAL_RE.search(body))
    wrapped = _EMPHASIS_WRAPPED_RE.match(body)
    if wrapped and not has_number and len(wrapped.group("body").split()) <= 8:
        return True
    lowered = body.lower().lstrip("*_ ")
    if len(lowered.split()) <= 2 and not has_number:
        return True
    # Audit item 6 (2026-10-04): the refusal / evidence-gap phrases used to
    # exempt a sentence wherever they appeared in it, so "The grade was 5.2
    # g/t Au over 3 m; passages do not cover the upper zone." shipped
    # uncited -- a claim with a hedge bolted on. The exemption now needs the
    # sentence to carry no measurement. Digits that are only part of a hole
    # name or a standard designation do not count ("I don't have data for
    # hole PLS-22-11" is still a refusal).
    if not _has_measurement(body) and any(p in lowered for p in _NON_CLAIM_PHRASES):
        return True
    if lowered.startswith(_POINTER_STARTERS):
        return True
    return not has_number and lowered.startswith(_CONNECTIVE_STARTERS + _META_STARTERS)


def is_table_header(units: list[Unit], i: int) -> bool:
    """A markdown table row directly followed by its |---| separator row."""
    row = units[i].text.strip()
    if not row.startswith("|") or i + 1 >= len(units):
        return False
    nxt = units[i + 1].text.strip()
    return nxt.startswith("|") and bool(_RULE_OR_TABLE_SEPARATOR_RE.match(nxt))
