"""Shared citation-marker patterns for the hallucination-prevention layers.

Single source of truth for what a citation marker looks like. Every layer
that strips, extracts, or matches citation markers must import from here —
the 2026-06-27 audit (T3) found three divergent copies of the pattern, and
the divergence was itself a bug class (layers 4 and 6 missed the colon form
and the PGEO prefix that layers 3 and the orchestrator validators accepted).

Marker grammar
--------------
- Prefixes: DATA, NI43, PUB, PGEO (corpus-scoped numeric markers), plus the
  ``ev`` evidence-id form used by the citation-first generator.
- Separators: the prompts instruct the model to emit the colon form
  (``[DATA:1]``, canonical per Kyle 2026-04-22); the response assembler
  appends the dash form (``[DATA-1]``). Both must be accepted for the
  duration of the rollout window.
- Citation objects (``Citation.citation_id``) are always dash-form — use
  ``canonical_marker()`` to normalise a text marker before comparing
  against them.
"""

from __future__ import annotations

import re

# Corpus prefixes for numeric citation markers ([DATA-1], [PGEO:4], ...).
CITATION_PREFIXES: frozenset[str] = frozenset({"DATA", "NI43", "PUB", "PGEO"})

_PREFIX_ALT = "|".join(sorted(CITATION_PREFIXES))

# Numeric citation marker, colon or dash form: [DATA:1], [NI43-2], [PGEO:4].
CITATION_MARKER_RE = re.compile(rf"\[(?:{_PREFIX_ALT})[:-]\d+\]")

# Capture variant — groups: (prefix, separator, index).
CITATION_MARKER_CAPTURE_RE = re.compile(rf"\[({_PREFIX_ALT})([:-])(\d+)\]")

# Superset that also matches [ev:abc1def2] evidence ids (non-numeric tail).
ALL_MARKER_RE = re.compile(rf"\[(?:{_PREFIX_ALT}|ev)[:-][A-Za-z0-9-]+\]")

# Evidence-id marker alone — group: the id. These name a span the citation
# span resolver (CITATION_SPAN_RESOLVER_ENABLED) is meant to resolve; nothing
# resolves them while it is off, so they cite nothing then (layer 2).
EV_MARKER_CAPTURE_RE = re.compile(r"\[ev[:-]([A-Za-z0-9-]+)\]")


def canonical_marker(prefix: str, index: str) -> str:
    """Dash-form marker matching ``Citation.citation_id`` (e.g. ``[DATA-1]``)."""
    return f"[{prefix}-{index}]"


def canonical_ev_marker(evidence_id: str) -> str:
    """Colon-form evidence marker, whichever separator the text used."""
    return f"[ev:{evidence_id}]"


# Grouped markers: several citations in one pair of brackets. Prompts ask for
# one marker per bracket and models write "[NI43-1, NI43-2]", "[NI43-1; NI43-2]"
# and "[NI43-1, 2]" anyway (a bare number takes the prefix of the item before
# it). None matches the single-marker grammar above, so a fully cited sentence
# looked uncited to Layers 2 and 5 and was dropped -- and a figure in the
# bracket text ("[NI43-1, 12]") was read as a numerical claim by Layer 3
# (2026-10-10 audit, finding 5).
#
# Four more spellings of the same thing (2026-10-10 review, item 9): a range
# ("[NI43-1-3]", "[NI43-1–3]", "[NI43:1-3]"), a space ("[NI43-1 NI43-2]") and
# "and" / "&" ("[NI43-1 and NI43-2]", "[NI43-1 & 2]"). A space joins only FULL
# items -- "[NI43-1 12]" is a marker and a stray number, not two citations --
# and a range expands to at most `_MAX_RANGE` ids, ascending. Expanding invents
# no citation: every id is a separate marker that Layer 2 checks against the
# Citation list and Layer 5 against the retrieved chunks.
_DASHES = "-\u2013\u2014\u2212"
_FULL_ITEM = rf"(?:{_PREFIX_ALT})[:-]\d+(?:[{_DASHES}]\d+)?"
_ANY_ITEM = rf"(?:(?:{_PREFIX_ALT})[:-])?\d+(?:[{_DASHES}]\d+)?"
_GROUP_SEPARATOR = r"\s*[,;&]\s*|\s+and\s+"
_GROUPED_MARKER_RE = re.compile(
    rf"\[({_FULL_ITEM}(?:(?:{_GROUP_SEPARATOR}){_ANY_ITEM}|\s+{_FULL_ITEM})*)\]"
)
_GROUP_SPLIT_RE = re.compile(rf"{_GROUP_SEPARATOR}|\s+")
_GROUP_ITEM_RE = re.compile(rf"(?:({_PREFIX_ALT})([:-]))?(\d+)(?:[{_DASHES}](\d+))?")
_MAX_RANGE = 6


def normalize_grouped_markers(text: str) -> str:
    """Rewrite grouped markers as adjacent single markers.

    ``[NI43-1, NI43-2]``, ``[NI43-1; NI43-2]``, ``[NI43-1 NI43-2]`` and
    ``[NI43-1 and NI43-2]`` become ``[NI43-1][NI43-2]``; ``[NI43-1, 2]``
    becomes ``[NI43-1][NI43-2]`` (and ``[DATA-1, NI43-2, 5]`` gives DATA-1,
    NI43-2, NI43-5); ``[NI43-1-3]`` becomes ``[NI43-1][NI43-2][NI43-3]``. The
    separator of each full item is kept. No space is put between the markers,
    so removing the last one (an invented id) leaves "[NI43-1]." and not
    "[NI43-1] .".

    Pure rewriting, no judgement: it invents nothing a later layer would
    accept. Every id it produces is a separate marker that Layer 2 checks
    against the Citation list and Layer 5 against the retrieved chunks, so
    ``[NI43-1, 99]`` costs the invented ``[NI43-99]`` and keeps ``[NI43-1]``.
    A descending or wider-than-`_MAX_RANGE` range is left as written.
    """
    if "[" not in text:
        return text

    def _expand(match: re.Match[str]) -> str:
        prefix, separator = "", "-"
        singles: list[str] = []
        for item in _GROUP_SPLIT_RE.split(match.group(1)):
            parsed = _GROUP_ITEM_RE.fullmatch(item)
            if parsed is None:  # the pattern above admits nothing else
                return match.group(0)
            if parsed.group(1):
                prefix, separator = parsed.group(1), parsed.group(2)
            first = int(parsed.group(3))
            last = int(parsed.group(4)) if parsed.group(4) else first
            if last < first or last - first >= _MAX_RANGE:
                return match.group(0)
            singles.extend(f"[{prefix}{separator}{n}]" for n in range(first, last + 1))
        return "".join(singles)

    return _GROUPED_MARKER_RE.sub(_expand, text)


def ungroup_response_markers[R](response: R) -> R:
    """``response`` with grouped markers in its answer rewritten as singles.

    The same object when there are none. Only the LLM-written head is
    rewritten -- the deterministic proactive-insights block after
    ``proactive_insights_offset`` is system output -- and the offset moves
    with the head.
    """
    text: str = response.text  # type: ignore[attr-defined]
    offset: int | None = response.proactive_insights_offset  # type: ignore[attr-defined]
    if offset is not None and 0 <= offset <= len(text):
        head, tail = text[:offset], text[offset:]
    else:
        head, tail, offset = text, "", None
    new_head = normalize_grouped_markers(head)
    if new_head == head:
        return response
    update: dict[str, object] = {"text": new_head + tail}
    if offset is not None:
        update["proactive_insights_offset"] = len(new_head)
    return response.model_copy(update=update)  # type: ignore[attr-defined,no-any-return]
