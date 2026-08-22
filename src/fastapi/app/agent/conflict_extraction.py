"""Turn the model's "### Conflicting evidence" section into structured rows.

``GeoRAGResponse.conflicting_evidence`` is declared in the schema, read in
four places, and — until 2026-08-21 — **written nowhere in production code**.
Its only writers were two test files. So ``conflicts_present`` in
``confidence_computer.apply_guard_demotion`` was permanently ``False`` and the
"conflicting sources force Low confidence" rule could never fire, no matter
what the answer said. ``RetrievalProfile.conflict_detection_enabled`` was set
True for the synthesis and uncertainty_quantification profiles and logged by
``route_node``; ``assemble_node`` never read it.

Meanwhile the ``synthesis_with_conflicts`` prompt fragment instructs the model
to emit a "### Conflicting evidence" sub-section. So the one place a conflict
was ever surfaced was inside the answer prose, where nothing could act on it.

This module closes that loop: it reads back what the model reported and puts
it in the structured field, which restores the demotion rule.

**What this is and is not.** It is not a conflict *detector*. The detector is
the model, reading the Evidence Set; this is a parser for its report. That
distinction matters for how much weight the output can carry: a populated row
means "the model said these two sources disagree", not "the system verified a
disagreement". Deciding what constitutes a geological conflict — whether a
498 m and a 510 m total depth for the same hole are a contradiction or a
collar-vs-EOH convention difference — is a domain judgement, and CLAUDE.md is
explicit that geological decisions are not to be inferred. A deterministic
detector needs those rules from an SME first.

Which is also why the rows are deliberately shallow. ``entity_key`` and
``property_name`` are filled only when the model's bullet uses the form the
prompt asks for, and are ``None`` otherwise rather than guessed. Every
consumer today reads this field for presence only (``bool(...)``), so an
honest partial row is strictly better than a confidently-wrong full one.
"""

from __future__ import annotations

import re
from typing import Any

from app.agent.hallucination.citation_markers import CITATION_MARKER_RE

__all__ = ["extract_conflicting_evidence"]

#: The header the prompt fragment orders verbatim ("Begin that sub-section
#: with the literal header '### Conflicting evidence'"). Matched at any depth
#: because models routinely shift heading levels.
_HEADER_RE = re.compile(r"^[ \t]*#{1,6}[ \t]*conflicting evidence[ \t]*:?[ \t]*$", re.I)

#: Any markdown header — where the section ends.
_ANY_HEADER_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+\S")

#: The model's way of saying it found none. Rule 21 of the fragment mandates a
#: line like this rather than omitting the header, so it is the common case
#: and must not parse as a conflict.
_NONE_MARKERS = (
    "none detected",
    "none found",
    "no conflicts",
    "no conflicting",
    "not applicable",
)

_BULLET_RE = re.compile(r"^[ \t]*(?:[-*+•]|\d+[.)])[ \t]+(.+)$")

#: "<entity> — <property>: ..." — the shape the prompt asks for ("naming the
#: entity, the property in conflict, and the citations on each side").
#:
#: The separator must be WHITESPACE-DELIMITED. An earlier version excluded
#: hyphens from the entity group, which cut "PLS-22-08" down to "PLS" — in a
#: geological corpus almost every entity is a hole ID full of hyphens
#: (DDH-22-041, 36-1085), so a bare-hyphen separator cannot be used.
_ENTITY_PROPERTY_RE = re.compile(
    r"^(?P<entity>[^:]{1,80}?)\s+[—–-]\s+(?P<prop>[^:]{1,80}?)\s*:",
)

#: "510 m [NI43-1] vs 498 m [NI43-3]" — split the two sides when the model
#: uses the connective. Not required; absent means values stays empty.
_VS_RE = re.compile(r"\s+(?:vs\.?|versus|against)\s+", re.I)


def _section_lines(text: str) -> list[str] | None:
    """The body lines under the first "Conflicting evidence" header.

    None when the header is absent — which is the normal case for every
    answer that did not run under the synthesis emphasis.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if _HEADER_RE.match(line):
            start = index + 1
            break
    if start is None:
        return None

    body: list[str] = []
    for line in lines[start:]:
        if _ANY_HEADER_RE.match(line):
            break
        body.append(line)
    return body


def _strip_markers(value: str) -> str:
    return CITATION_MARKER_RE.sub("", value).strip(" \t.,;·-—")


def extract_conflicting_evidence(text: str) -> list[dict[str, Any]] | None:
    """Structured rows for the conflicts the model reported, or None.

    Returns None both when the section is absent and when it is present but
    says none were found. None rather than ``[]`` on purpose: every consumer
    tests truthiness, and the schema's own default is None for "no conflicts
    are present", so an empty list would be a third state meaning the same
    thing.

    Args:
        text: the assembled answer body.

    Returns:
        A list of ``{entity_key, property_name, evidence_ids, values, claim}``
        dicts — ``entity_key``/``property_name`` may be None, ``claim`` always
        carries the model's own sentence so a reader can see what was actually
        said rather than only the parse of it. None when there is nothing to
        report.
    """
    if not text:
        return None

    body = _section_lines(text)
    if body is None:
        return None

    joined = " ".join(body).lower()
    if any(marker in joined for marker in _NONE_MARKERS):
        return None

    rows: list[dict[str, Any]] = []
    for line in body:
        match = _BULLET_RE.match(line)
        if not match:
            continue
        claim = match.group(1).strip()
        if not claim:
            continue

        evidence_ids = CITATION_MARKER_RE.findall(claim)
        # A conflict with no citation on either side is the model asserting a
        # disagreement it cannot point at. Kept — it is still what the answer
        # says, and dropping it would hide the weakest claims from the reader
        # while leaving the strongest — but the empty evidence_ids list is the
        # signal that it is unsupported.
        shape = _ENTITY_PROPERTY_RE.match(_strip_markers(claim))
        entity_key = shape.group("entity").strip() if shape else None
        property_name = shape.group("prop").strip() if shape else None

        sides = _VS_RE.split(claim)
        values = [_strip_markers(side) for side in sides] if len(sides) > 1 else []
        if values and shape:
            # Drop the "<entity> - <property>:" label from the first side so
            # `values` holds the two values rather than one value wearing the
            # row's own header.
            values[0] = values[0][shape.end():].strip(" \t.,;") or values[0]

        rows.append({
            "entity_key": entity_key,
            "property_name": property_name,
            "evidence_ids": evidence_ids,
            "values": [v for v in values if v],
            "claim": claim,
        })

    return rows or None
