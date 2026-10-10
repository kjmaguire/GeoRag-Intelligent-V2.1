"""Layer 2 — Typed Output Validation, and CLAUDE.md rule 4 enforcement.

Architecture reference: Section 04i, Layer 2; CLAUDE.md hard rule 4.

Purpose
-------
Validate that the assembled GeoRAGResponse is internally consistent:

  1. Every citation marker in the LLM text (e.g. ``[DATA-1]``, ``[NI43-2]``)
     has a corresponding Citation object in the ``citations`` list.
  2. No Citation has an empty or placeholder ``source_chunk_id``.
  3. The ``text`` field is not empty or a pure refusal with no grounding data.
  4. ``confidence`` is within [0.0, 1.0].

and — :func:`enforce_claim_citations` — that every substantive claim the
model made carries a citation at all (rule 4: "every claim ... must include
a source_chunk_id or be rejected").

This layer runs AFTER the response_assembler has built the GeoRAGResponse but
BEFORE the response is streamed to the client. The LLM cannot be re-invoked
in the agentic graph, so failures are repaired rather than retried.

Repairs applied
---------------
- Grouped markers are rewritten as single ones first ("[NI43-1, NI43-2]" ->
  "[NI43-1] [NI43-2]", see ``citation_markers.normalize_grouped_markers``):
  the checks below only know one marker per bracket, so a fully cited
  sentence looked uncited and was dropped (2026-10-10 audit, finding 5).
  Each id is still checked on its own, so an invented one is not let through.
- An invented citation marker (in the text, but with no Citation behind it)
  takes the sentence it supported with it. It used to be stripped on its own
  and the claim kept, which shipped the claim uncited — exactly what rule 4
  forbids (audit 2026-09-29, RAG-7). A sentence that ALSO carries a real
  marker keeps that marker and loses only the invented one. The sentence
  rule is Layer 5's, shared through
  ``layer5_provenance.scrub_rejected_markers``.
- An evidence-id marker ("[ev:abc123]") is an invented marker too unless the
  citation span resolver is on (CITATION_SPAN_RESOLVER_ENABLED, off by
  default): nothing else resolves it, and it used to count as a citation
  without being checked against anything (2026-10-10 audit, finding 6).
- A substantive sentence with no citation marker is removed by
  :func:`enforce_claim_citations`; if no cited claim is left the answer
  becomes a typed refusal.
- Placeholder source_chunk_ids ("no-tool-call") are left alone and logged —
  the placeholder is honest about the gap.
- Confidence is clamped to [0.0, 1.0] if somehow out of range.

Findings from both repairs are returned so ``validate_node`` can force
``should_retry`` (confidence floor + banner), the same as a Layer 5
rejection.

Usage
-----
    from app.agent.hallucination.layer2_typed_output import (
        enforce_claim_citations,
        validate_and_repair_with_findings,
    )
    response, findings = validate_and_repair_with_findings(response)
    response, rule4 = enforce_claim_citations(response)
"""

from __future__ import annotations

import logging
import re

from app.agent.hallucination.citation_markers import (
    ALL_MARKER_RE,
    CITATION_MARKER_CAPTURE_RE,
    EV_MARKER_CAPTURE_RE,
    canonical_ev_marker,
    canonical_marker,
    ungroup_response_markers,
)
from app.agent.hallucination.claim_sentences import (
    Unit,
    drop_units,
    is_non_claim,
    is_table_header,
    split_units,
)
from app.agent.hallucination.refusals import (
    MODEL_NO_OUTPUT_TEXT,
    PROVENANCE_REFUSAL_TEXT,
    UNSUPPORTED_BY_SOURCES_MESSAGE,
    make_refusal_payload,
)
from app.config import settings
from app.models.rag import Citation, GeoRAGResponse

logger = logging.getLogger(__name__)


def _ev_markers_cite() -> bool:
    """Whether an ``[ev:...]`` marker can stand for a citation at all.

    Only the citation span resolver resolves an evidence-id marker to a span
    of a retrieved chunk. While it is off (the default) such a marker names
    nothing, so it is treated exactly like a numeric marker with no Citation
    behind it: an orphan.
    """
    return bool(getattr(settings, "CITATION_SPAN_RESOLVER_ENABLED", False))


def _unsupported_refusal_payload() -> dict[str, object]:
    """Refusal payload for an answer withheld because its claims cited nothing
    real. Plain message: it renders verbatim in the RefusalPanel."""
    return make_refusal_payload(
        "unsupported_by_sources", UNSUPPORTED_BY_SOURCES_MESSAGE
    )


def validate_and_repair(response: GeoRAGResponse) -> GeoRAGResponse:
    """Validate and repair a GeoRAGResponse for internal consistency.

    This is hallucination prevention Layer 2. Kept for callers that only
    want the repaired response; ``validate_node`` uses
    :func:`validate_and_repair_with_findings` so an invented marker also
    forces ``should_retry``.

    Returns:
        The (possibly repaired) GeoRAGResponse. Never raises — all issues
        are logged as warnings and fixed in-place.
    """
    return validate_and_repair_with_findings(response)[0]


def validate_and_repair_with_findings(
    response: GeoRAGResponse,
) -> tuple[GeoRAGResponse, list[str]]:
    """:func:`validate_and_repair`, plus the guard findings it produced.

    Returns ``(response, findings)``; ``findings`` holds one ``"Layer 2: ..."``
    string when invented markers were found (the only repair here that
    changes what the answer claims). The response is the same object when
    nothing needed repairing.
    """
    issues: list[str] = []
    findings: list[str] = []

    # One marker per bracket from here on ("[NI43-1, NI43-2]" is two).
    response = ungroup_response_markers(response)

    # ── Check 1: citation marker ↔ citation list consistency ──────────────
    known_ids = set(c.citation_id for c in response.citations)
    orphan_ids: set[str] = set()
    for m in CITATION_MARKER_CAPTURE_RE.finditer(response.text):
        # citation_ids are always dash-form; the text may use the colon form.
        cid = canonical_marker(m.group(1), m.group(3))
        if cid not in known_ids:
            orphan_ids.add(cid)
    # An evidence-id marker has no Citation either, and nothing resolves it
    # unless the span resolver is on.
    if not _ev_markers_cite():
        orphan_ids.update(
            canonical_ev_marker(m.group(1))
            for m in EV_MARKER_CAPTURE_RE.finditer(response.text)
        )

    if orphan_ids:
        from app.agent.hallucination.layer5_provenance import (  # noqa: PLC0415
            scrub_rejected_markers,
        )

        orphan_list = ", ".join(sorted(orphan_ids))
        text, offset, dropped = scrub_rejected_markers(
            response.text,
            orphan_ids,
            known_ids,
            proactive_insights_offset=response.proactive_insights_offset,
        )
        issues.append(
            f"Orphan citation marker(s) in text with no matching Citation "
            f"object: {orphan_list} ({dropped} sentence(s) removed)"
        )
        findings.append(
            f"Layer 2: citation marker(s) {orphan_list} match no retrieved "
            f"source — {dropped} sentence(s) resting only on them were "
            f"removed (an invented citation cannot back a claim)"
        )
        response = response.model_copy(
            update=dict(text=text, proactive_insights_offset=offset)
        )

    # ── Check 2: source_chunk_id validity ─────────────────────────────────
    for citation in response.citations:
        if not citation.source_chunk_id or citation.source_chunk_id == "no-tool-call":
            issues.append(
                f"Citation {citation.citation_id} has placeholder "
                f"source_chunk_id='{citation.source_chunk_id}'"
            )
            # Don't repair — the assembler's fallback is the best we can do
            # when no tool was called. The placeholder is honest.

    # ── Check 3: text not empty ───────────────────────────────────────────
    if not response.text or not response.text.strip():
        issues.append("Response text is empty")
        # Emptied by the invented-marker repair above: every claim rested on
        # a citation that does not exist, so say that rather than "unable
        # to generate".
        replacement = (
            CITATION_REFUSAL_TEXT if findings else "I was unable to generate a response."
        )
        _update: dict[str, object] = dict(
            text=replacement, proactive_insights_offset=None
        )
        if findings:
            _update["refusal_payload"] = _unsupported_refusal_payload()
        response = response.model_copy(update=_update)

    # ── Check 4: confidence clamped ───────────────────────────────────────
    if response.confidence < 0.0 or response.confidence > 1.0:
        issues.append(
            f"Confidence {response.confidence} out of [0.0, 1.0] range"
        )
        response = response.model_copy(
            update=dict(confidence=max(0.0, min(1.0, response.confidence)))
        )

    # ── Check 5: at least one source_used ─────────────────────────────────
    if not response.sources_used:
        issues.append("sources_used list is empty — no grounding data")

    # ── Log results ───────────────────────────────────────────────────────
    if issues:
        logger.warning(
            "layer2_typed_output: %d issue(s) found and repaired:\n  %s",
            len(issues),
            "\n  ".join(issues),
        )
    else:
        logger.debug("layer2_typed_output: response passed all checks")

    return response, findings


# ---------------------------------------------------------------------------
# CLAUDE.md hard rule 4 — every claim carries a citation, or it does not ship
# ---------------------------------------------------------------------------

#: The answer when the model made claims and cited none of them. Opens with
#: "I don't have" so ``response_assembler._is_refusal`` reads it as a refusal,
#: like every other refusal path.
CITATION_REFUSAL_TEXT = (
    "I don't have an answer I can support with citations for this question. "
    "The draft answer made claims that could not be tied to any retrieved "
    "source, so it was withheld rather than shown uncited. Try rephrasing "
    "the question, or narrowing it to a specific hole, report or area."
)

#: source_chunk_id of the placeholder citation a withheld answer carries —
#: GeoRAGResponse.citations requires at least one. Same pattern as Layer 5's
#: "provenance-rejected".
CITATION_REJECTED_SOURCE_ID = "citation-rejected"

#: How far into the NEXT sentence a marker may sit and still cite this one —
#: "Claim. [NI43-1] Next claim." attributes [NI43-1] to "Claim.". The same
#: allowance the advisory completeness guard has always made.
_NEXT_SENTENCE_MARKER_WINDOW = 40

#: OIUR sections whose lines are not claims by contract: Interpretations rest
#: on the cited Observations they name ("supports: O1, O2"), Uncertainty and
#: Recommended actions are the model's judgement. Only applied when the
#: answer is in OIUR shape (has "## Observations").
_OIUR_UNCITED_SECTIONS: tuple[str, ...] = (
    "interpretations", "uncertainty", "recommended actions",
)
_OIUR_RE = re.compile(r"(?mi)^##\s+observations\b")
_H2_RE = re.compile(r"^\s*##\s+(?P<title>[^#].*?)\s*$")


def _is_system_text(text: str) -> bool:
    """Canned text written by the system, never by the model."""
    from app.agent.hallucination.layer1_retrieval import (  # noqa: PLC0415
        build_refusal_text,
    )
    from app.agent.llm_common import BUDGET_EXHAUSTED_FALLBACK  # noqa: PLC0415

    stripped = text.strip()
    return stripped in (
        build_refusal_text(),
        CITATION_REFUSAL_TEXT,
        BUDGET_EXHAUSTED_FALLBACK,
        MODEL_NO_OUTPUT_TEXT,
        PROVENANCE_REFUSAL_TEXT,
        "I was unable to generate a response.",
    )


def _real_citation_ids(citations: list[Citation]) -> set[str]:
    """Citation ids that stand for evidence, not a placeholder sentinel."""
    from app.agent.response_assembler import is_empty_source_id  # noqa: PLC0415

    placeholders = ("provenance-rejected", CITATION_REJECTED_SOURCE_ID)
    return set(
        c.citation_id
        for c in citations
        if c.source_chunk_id
        and c.source_chunk_id not in placeholders
        and not is_empty_source_id(c.source_chunk_id)
    )


def _cites(piece: str, valid_ids: set[str]) -> bool:
    if _ev_markers_cite() and EV_MARKER_CAPTURE_RE.search(piece):
        return True
    return any(
        canonical_marker(m.group(1), m.group(3)) in valid_ids
        for m in CITATION_MARKER_CAPTURE_RE.finditer(piece)
    )


def _oiur_exempt_flags(units: list[Unit]) -> list[bool]:
    flags: list[bool] = []
    exempt = False
    for unit in units:
        heading = _H2_RE.match(unit.text)
        if heading:
            exempt = heading.group("title").strip("*_ ").lower() in _OIUR_UNCITED_SECTIONS
        flags.append(exempt)
    return flags


def enforce_claim_citations(
    response: GeoRAGResponse,
) -> tuple[GeoRAGResponse, list[str]]:
    """CLAUDE.md hard rule 4: drop every substantive claim that cites nothing.

    Nothing enforced this before. The completeness guard found uncited
    sentences, but it was advisory and discarded up to two findings per
    answer, so "The deposit is unconformity-related and hosted in graphitic
    pelite. The alteration halo is chlorite-dominant." — no marker anywhere
    — validated "clean" (audit 2026-09-29, RAG-7).

    A sentence ships if it carries a marker naming a real (non-placeholder)
    citation, or if the next sentence ON THE SAME LINE opens with one within
    its first ``_NEXT_SENTENCE_MARKER_WINDOW`` characters (the completeness
    guard's long-standing allowance, kept for "Claim. [NI43-1] More."). Sentences that make no claim are exempt —
    headings, labels, table separators and header rows, questions,
    refusals and hedges, pointers ("See Table 14-3"), number-free
    connectives and number-free statements about the search ("I found
    passages about QA/QC but nothing on metallurgy"), and in an OIUR-shaped
    answer the Interpretations / Uncertainty / Recommended-actions sections,
    which the OIUR contract leaves uncited. See
    :func:`app.agent.hallucination.claim_sentences.is_non_claim`.

    Everything else is removed, keeping the surrounding markdown intact. If
    no cited claim is left, the answer becomes :data:`CITATION_REFUSAL_TEXT`
    — rule 4 says an uncited claim is rejected, and an answer made only of
    rejected claims is not an answer.

    The proactive-insights block (deterministic system output, see
    ``anomaly_detector``) and canned system texts are never touched.

    Returns ``(response, findings)``. ``findings`` is empty and the response
    is the SAME object when nothing was removed (a response whose grouped
    markers were rewritten as singles comes back as a copy carrying them).
    """
    response = ungroup_response_markers(response)
    text = response.text or ""
    if not text.strip() or _is_system_text(text):
        return response, []

    valid_ids = _real_citation_ids(list(response.citations))
    offset = response.proactive_insights_offset
    head = text[:offset] if offset is not None and 0 <= offset <= len(text) else text
    oiur = bool(_OIUR_RE.search(head))
    cache: dict[int, list[bool]] = dict()

    def _drop(i: int, units: list[Unit]) -> bool:
        unit = units[i].text
        if is_non_claim(unit) or is_table_header(units, i):
            return False
        if oiur:
            flags = cache.setdefault(id(units), _oiur_exempt_flags(units))
            if flags[i]:
                return False
        if _cites(unit, valid_ids):
            return False
        # The next-sentence allowance is for "Claim. [NI43-1] More." on one
        # line. A bullet, table row or paragraph on the next line is a claim
        # of its own; its marker does not cite this one.
        if i + 1 < len(units) and "\n" not in units[i].sep:
            nxt = units[i + 1].text.strip()
            if ALL_MARKER_RE.match(nxt) and _cites(nxt, valid_ids):
                return False
            if _cites(nxt[:_NEXT_SENTENCE_MARKER_WINDOW], valid_ids):
                return False
        return True

    new_text, new_offset, dropped = drop_units(
        text, _drop, proactive_insights_offset=offset,
    )
    if not dropped:
        return response, []

    new_head = new_text[:new_offset] if new_offset is not None else new_text
    survivors = split_units(new_head)
    any_cited_claim = any(
        _cites(u.text, valid_ids) and not is_non_claim(u.text) for u in survivors
    )

    findings = [
        f"Layer 2: {dropped} uncited claim sentence(s) removed — every claim "
        f"must carry a citation to a retrieved source (CLAUDE.md rule 4)"
    ]
    update: dict[str, object] = dict(text=new_text, proactive_insights_offset=new_offset)
    if not any_cited_claim:
        findings.append(
            "Layer 2: no cited claim remained after removing uncited ones — "
            "the answer was withheld"
        )
        update = dict(
            text=CITATION_REFUSAL_TEXT,
            proactive_insights_offset=None,
            # Machine-readable refusal so the chat renders RefusalPanel
            # instead of an ordinary-looking answer bubble (audit item 8).
            refusal_payload=_unsupported_refusal_payload(),
            citations=[
                Citation(
                    citation_id="[DATA-1]",
                    citation_type="DATA",
                    source_chunk_id=CITATION_REJECTED_SOURCE_ID,
                    document_title="No supporting source",
                    section=None,
                    page=None,
                    relevance_score=0.0,
                )
            ],
            sources_used=[CITATION_REJECTED_SOURCE_ID],
        )

    logger.warning(
        "layer2_typed_output: rule-4 enforcement removed %d uncited claim "
        "sentence(s)%s",
        dropped,
        "" if any_cited_claim else " and withheld the answer",
    )
    return response.model_copy(update=update), findings
