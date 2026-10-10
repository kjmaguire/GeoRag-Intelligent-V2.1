"""Layer 1 — Retrieval Quality Gate.

Architecture reference: Section 04i, Layer 1.

History (2026-08-21): a Pydantic-AI-shaped ``layer1_retrieval.py`` existed
before this file and was deleted as unreachable — the orchestrator never
adopted the ``output_validator`` pattern it was built for (see
``orchestrator_validators.py``'s module docstring). What survived instead
was a FLAT per-chunk floor applied inline in
``app.agent.tools.search_documents`` (``RERANKER_SCORE_THRESHOLD`` /
``RERANKER_SCORE_THRESHOLD_HOSTED``) — real, but not the gate CLAUDE.md
hard rule 5 describes. This module restores the rest of it: a genuine
retrieval-quality VERDICT computed over everything a query retrieved,
not just one tool's per-chunk score.

Two signals, two different consumers
-------------------------------------
``assess_retrieval_quality`` returns a :class:`RetrievalQualityVerdict`
with two independent findings:

  1. ``refuse`` — HARD. Nothing cleared the relevance floor from ANY
     store: no document chunk survived ``search_documents``'s own floor,
     and no structured tool (PostGIS, public geoscience, graph) returned
     any rows either. There is no point spending an LLM call synthesizing
     an answer from empty context, and no reliable way for a post-hoc
     guard to tell a careful refusal apart from a confident fabrication
     built on nothing. Consumed by ``assemble_node``, BEFORE the LLM is
     called — see ``build_refusal_text()``.

  2. ``weak`` — SOFT / advisory. Some document chunks cleared the floor,
     but only marginally (thin count, or no chunk reaches the stricter
     "confidently relevant" threshold). Surfaced as a ``"Layer 1: ..."``
     validation warning by
     ``orchestrator_validators.verify_retrieval_quality``, the same way
     the Layer 3/6 advisories are — it never sets ``should_retry`` on its
     own; this check has not been calibrated against a real corpus yet
     (same posture the completeness guard shipped with).

The cosine/RRF-fallback case
-----------------------------
``DocumentSearchResult.rerank_degraded=True`` means the deployment uses an
explicitly local/dev reranker backend (``cross_encoder`` / ``qwen3_causal``)
and none was loaded, so ``search_documents`` returned raw Qdrant RRF-fusion
order instead (see that dataclass's docstring). It is never set on a hosted
deployment: with ``RERANKER_BACKEND=bedrock`` a reranker that is missing or
fails twice is a typed ``reranker_unavailable`` retrieval failure and the
query fails with RETRIEVAL_UNAVAILABLE before this gate runs. RRF
scores (``float(point.score)``, roughly ``1/(k+rank)``) live on a
completely different, uncalibrated scale from a cross-encoder or Cohere
relevance score — an order of magnitude smaller. Comparing them against
``RETRIEVAL_GATE_CONFIDENT_SCORE`` would refuse or flag nearly every
degraded-reranker query, regardless of how relevant the results actually
were. That is exactly the ``RETRIEVAL_QUALITY_THRESHOLD`` incident
recorded in ``config.py`` (a rank-derived score compared against a
calibrated-scale gate made every document query return "insufficient
information" until 2026-08-21) — this restoration must not reintroduce
it under a new name. So on the fallback path this module applies ONLY a
count check, never a score comparison.

Usage
-----
    from app.agent.hallucination.layer1_retrieval import (
        assess_retrieval_quality,
        build_refusal_text,
    )
    verdict = assess_retrieval_quality(state.tool_results)
    if verdict.refuse:
        response = assemble_response(build_refusal_text(), state.tool_results)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievalQualityVerdict:
    """Layer 1 verdict for one query's retrieved evidence.

    ``refuse`` and ``weak`` are independent — ``refuse`` is only ever True
    when ``weak`` is False (a refusal supersedes an advisory warning).
    """

    refuse: bool
    weak: bool
    reason: str | None
    document_chunks_considered: int
    document_chunks_confident: int
    used_cosine_fallback: bool
    other_evidence_present: bool


#: Refusal text for the hard-gate case. Deliberately opens with "I don't
#: have" — the first phrase in
#: ``response_assembler._REFUSAL_PHRASES_ANYWHERE`` — so
#: ``response_assembler._is_refusal`` recognises this as a refusal exactly
#: like every other refusal path (IND-6 confidence flooring, the OIUR
#: parser skip, etc.) instead of needing a parallel special case.
_REFUSAL_TEXT = (
    "I don't have sufficient relevant information retrieved for this "
    "project to answer confidently. Document search found no passages "
    "that cleared the relevance threshold, and no structured data (drill "
    "holes, samples, spatial features, or public geoscience records) "
    "relevant to this question was retrieved either. Try rephrasing the question, "
    "narrowing it to a specific hole or area, or verify the project has "
    "ingested data covering this topic."
)


def build_refusal_text() -> str:
    """Typed refusal text for a hard Layer 1 gate failure."""
    return _REFUSAL_TEXT


def build_refusal_payload() -> dict[str, Any]:
    """``GeoRAGResponse.refusal_payload`` for a hard Layer 1 gate failure.

    The same shape ``_build_terminal_refusal_payload`` in
    agentic_retrieval/nodes.py produces, so the chat's RefusalPanel and
    persist's ``rejection_reason`` read it without a special case.
    ``reason_code`` is ``insufficient_evidence``, the existing
    ``RefusalReasonCode`` (app/models/answer_run.py) for exactly this
    outcome; ``strategy`` is None because no repair strategy ran — the
    gate refused before the LLM was called.
    """
    return {
        "type": "refusal",
        "reason_code": "insufficient_evidence",
        "strategy": None,
        "message": (
            "No retrieved evidence cleared the relevance threshold for this "
            "project, so no answer was generated."
        ),
        "candidates": [],
        "guard_codes": [],
    }


#: Visualization cards (ADR-0007). Rendered beside the answer, not evidence:
#: query_stereonet returns StereonetResult(count=0) on timeout, error or no
#: data, and query_drill_traces_3d an empty result, never None — so a
#: keyword-triggered card ("3d ", "drill trace") used to satisfy the
#: zero-evidence gate on its own and send an empty context to the LLM
#: (audit 2026-09-29, AGT-4).
_VIZ_CARD_TOOLS: frozenset[str] = frozenset(("query_stereonet", "query_drill_traces_3d"))

#: Tools that return project-wide rows whatever the question asked: every
#: collar, the project's assays for an auto-picked element, the project
#: overview. They answer questions ABOUT the drill data; for anything else
#: they are context, not evidence (RAG-13).
_PROJECT_WIDE_TOOLS: frozenset[str] = frozenset((
    "query_spatial_collars", "query_assay_data", "query_project_overview",
))

#: Intents whose primary evidence is documents (retrieval_profile.py puts
#: search_documents first). For these, project-wide rows only count toward
#: the zero-evidence gate when the question is about the drill data.
#: anomaly_detection, project_summary and coverage_gap are structured-data
#: intents and are deliberately absent: what counts for them is unchanged.
_DOCUMENT_CENTRIC_INTENTS: frozenset[str] = frozenset((
    "factual_lookup", "synthesis", "hypothesis_generation",
    "uncertainty_quantification", "decision_support",
))

#: Unambiguous drill-data vocabulary: ONE hit makes a question "about the
#: drill data". Commodity NAMES are here; "lead" is not (it is also a verb and
#: a noun), nor are the bare metal symbols.
_DRILL_DATA_STRONG_RE = re.compile(
    r"\b(?:drill\w*|holes?|boreholes?|ddh|collars?|assay\w*|grades?|"
    r"intercepts?|intersect\w*|lithology|mineraliz\w*|mineralis\w*|cut-?off|"
    r"u3o8|e?u3o8|uranium|gold|silver|copper|zinc|nickel|cobalt|"
    r"molybdenum|lithium|ppm|ppb)\b|g/t|\d\s*%",
    re.IGNORECASE,
)

#: Ordinary words that are only drill-data vocabulary in company ("what is the
#: core business", "the lead investigator", "a log of the meeting", "deep
#: learning", "Co. Ltd", "au revoir"). One of these is not a signal; TWO
#: distinct ones are ("depths of the core samples").
_DRILL_DATA_WEAK_RE = re.compile(
    r"\b(?:samples?|sampling|intervals?|depths?|deep\w*|deepest|logs?|logged|"
    r"core|lead|au|ag|cu|zn|ni|co|pb|mo|li)\b",
    re.IGNORECASE,
)


def _is_drill_data_question(query: str) -> bool:
    """A question "about the drill data": drilling, holes, collars, assays,
    grades, intercepts, lithology, or a named commodity -- or two of the
    ordinary words (core, depth, samples, ...) that only mean that together.

    The old single alternation matched core, lead, log, deep, co, au and li
    on their own, so an unrelated collar dump satisfied the zero-evidence
    gate for "what is the core business of the operator?" (audit item 15).
    """
    if _DRILL_DATA_STRONG_RE.search(query):
        return True
    weak = {m.group(0).lower() for m in _DRILL_DATA_WEAK_RE.finditer(query)}
    return len(weak) >= 2


def _is_viz_card(name: str, result: Any) -> bool:
    if name in _VIZ_CARD_TOOLS:
        return True
    from app.agent.tools import DrillTrace3DResult, StereonetResult  # noqa: PLC0415

    return isinstance(result, (StereonetResult, DrillTrace3DResult))


def _counts_as_evidence(
    name: str, result: Any, *, intent: str | None, query: str | None
) -> bool:
    """Whether a non-document result satisfies the zero-evidence gate."""
    if _is_viz_card(name, result):
        return False
    if name not in _PROJECT_WIDE_TOOLS:
        # Query-specific: the hole-ID pre-pass (query_collar_details), a
        # hole's downhole log, scored public-geoscience records, and the
        # structured-intent aggregates.
        return True
    if intent is None or intent not in _DOCUMENT_CENTRIC_INTENTS or query is None:
        return True
    from app.agent.hole_id_patterns import (  # noqa: PLC0415
        iter_compact_hole_id_matches,
        iter_hole_id_matches,
    )

    # "Pre-2010" and "Zone-3" have a hole ID's shape and are not one: a
    # question that only says "pre-2010" must not count project-wide rows as
    # evidence for a document question.
    return (
        _is_drill_data_question(query)
        or next(iter_hole_id_matches(query), None) is not None
        or next(iter_compact_hole_id_matches(query), None) is not None
    )


def assess_retrieval_quality(
    tool_results: list[tuple[str, Any]],
    *,
    intent: str | None = None,
    query: str | None = None,
) -> RetrievalQualityVerdict:
    """Layer 1: judge whether ``tool_results`` grounds the query about to be
    answered.

    ``tool_results`` is expected in the shape ``state.tool_results`` has by
    the time ``assemble_node``/``validate_node`` read it — execute_node has
    ALREADY dropped every empty/zero-row result via
    ``app.agent.agentic_retrieval.nodes._worth_citing`` (which wraps
    ``app.agent.tool_result_helpers._is_empty_tool_result``), so every
    entry reaching this function carries at least one usable row. That
    means the flat per-chunk floor (``RERANKER_SCORE_THRESHOLD_HOSTED`` /
    ``RERANKER_SCORE_THRESHOLD``, applied inside ``search_documents``) has
    already run by the time this function sees a chunk — this goes beyond
    it, not in place of it.

    What counts as "other evidence" for the hard gate (2026-09-29):

      * Visualization cards (stereonet, 3-D drill traces) never count —
        they are rendered beside the answer, and arrive even when empty
        (AGT-4).
      * For a document-centric ``intent`` (``_DOCUMENT_CENTRIC_INTENTS``),
        the project-wide structured dumps (every collar, the project's
        assays, the overview) count only when ``query`` is about the drill
        data. "What metallurgical recovery did the PEA assume?" with no
        document chunk above the floor used to go to the LLM with five
        collars and seven uranium samples as its evidence; it is now
        refused (RAG-13). Hole-specific lookups, public-geoscience records
        and the structured-intent aggregates always count, and with no
        ``intent`` / ``query`` (the advisory caller) nothing changes.

    Never raises — this is pure computation over already-fetched Python
    objects, no I/O.
    """
    if not settings.RETRIEVAL_QUALITY_GATE_ENABLED:
        return RetrievalQualityVerdict(
            refuse=False,
            weak=False,
            reason=None,
            document_chunks_considered=0,
            document_chunks_confident=0,
            used_cosine_fallback=False,
            other_evidence_present=True,
        )

    from app.agent.tools import DocumentSearchResult  # noqa: PLC0415

    doc_results = [
        r for _name, r in tool_results if isinstance(r, DocumentSearchResult)
    ]
    other_results = [
        (name, r)
        for name, r in tool_results
        if not isinstance(r, DocumentSearchResult)
        and _counts_as_evidence(name, r, intent=intent, query=query)
    ]
    other_evidence_present = len(other_results) > 0

    doc_chunks = [c for r in doc_results for c in r.chunks]
    used_cosine_fallback = any(r.rerank_degraded for r in doc_results)
    chunks_considered = len(doc_chunks)

    # ── Hard gate: nothing at all was retrieved from ANY store. ───────────
    # One annotation for every branch below: mypy fixes a variable's type at
    # its first assignment, and the later branches assign None.
    reason: str | None
    if chunks_considered == 0 and not other_evidence_present:
        reason = (
            "Layer 1: retrieval quality gate failed — no document passages "
            "cleared the relevance floor and no structured data (PostGIS, "
            "public geoscience) was retrieved for this query"
        )
        logger.warning(
            "layer1_retrieval: refusing — zero evidence across %d tool "
            "call(s) (document search ran=%s, rerank_degraded=%s)",
            len(tool_results),
            bool(doc_results),
            used_cosine_fallback,
        )
        return RetrievalQualityVerdict(
            refuse=True,
            weak=False,
            reason=reason,
            document_chunks_considered=0,
            document_chunks_confident=0,
            used_cosine_fallback=used_cosine_fallback,
            other_evidence_present=False,
        )

    if chunks_considered == 0:
        # No document evidence, but a structured tool returned real rows —
        # not a Layer 1 concern; Layer 3/4 verify those claims directly.
        return RetrievalQualityVerdict(
            refuse=False,
            weak=False,
            reason=None,
            document_chunks_considered=0,
            document_chunks_confident=0,
            used_cosine_fallback=used_cosine_fallback,
            other_evidence_present=True,
        )

    min_chunks = int(getattr(settings, "RETRIEVAL_GATE_MIN_CHUNKS", 1))

    # ── Cosine/RRF fallback: count only, never a score comparison. ────────
    if used_cosine_fallback:
        weak = chunks_considered < min_chunks
        reason = (
            f"Layer 1: thin retrieval under reranker fallback — only "
            f"{chunks_considered} document chunk(s) survived (RRF/cosine "
            f"order; the reranker was unavailable for this query, so no "
            f"calibrated relevance score exists to check further)"
        ) if weak else None
        return RetrievalQualityVerdict(
            refuse=False,
            weak=weak,
            reason=reason,
            document_chunks_considered=chunks_considered,
            document_chunks_confident=0,
            used_cosine_fallback=True,
            other_evidence_present=other_evidence_present,
        )

    # ── Real reranker scores: apply the secondary "confident" threshold. ──
    # By the time a DocumentChunk reaches this function, relevance_score has
    # already been normalised to a comparable [0, 1] scale for EVERY
    # backend — app.agent.tools.search_documents sigmoid-transforms
    # cross_encoder/qwen3_causal's raw logits, and passes Cohere's own
    # [0, 1] probability through unchanged for the hosted backend (see the
    # ``needs_sigmoid`` block there). So a single threshold applies
    # regardless of RERANKER_BACKEND, unlike the flat per-chunk floor above
    # it (which compares PRE-transform values and is genuinely
    # backend-aware for that reason).
    confident_threshold = float(
        getattr(settings, "RETRIEVAL_GATE_CONFIDENT_SCORE", 0.35)
    )
    confident = sum(
        1 for c in doc_chunks if (c.relevance_score or 0.0) >= confident_threshold
    )
    weak = chunks_considered < min_chunks or confident == 0
    reason = None
    if weak:
        reason = (
            f"Layer 1: weak retrieval — {chunks_considered} chunk(s) cleared "
            f"the relevance floor but none reached the confident threshold "
            f"({confident_threshold:.2f}); the cited evidence may be only "
            f"marginally relevant. NOTE: this confident threshold is a "
            f"provisional default, unmeasured against Cohere Rerank 3.5 "
            f"(see RETRIEVAL_GATE_CONFIDENT_SCORE in config.py) — read this "
            f"warning as advisory, not as evidence of a real quality problem"
        )
    return RetrievalQualityVerdict(
        refuse=False,
        weak=weak,
        reason=reason,
        document_chunks_considered=chunks_considered,
        document_chunks_confident=confident,
        used_cosine_fallback=False,
        other_evidence_present=other_evidence_present,
    )
