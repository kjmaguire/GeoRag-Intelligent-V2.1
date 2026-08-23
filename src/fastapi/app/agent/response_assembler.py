"""Response assembler — build GeoRAGResponse from LLM text + tool call history.

Instead of asking the LLM to produce perfectly-structured JSON (which small
local models handle poorly), we let the agent return plain text and then
construct the GeoRAGResponse programmatically from what actually happened:

  - text:         the LLM's free-form answer
  - citations:    one Citation per unique tool call, with real source_chunk_ids
  - confidence:   computed from validator pass rate and tool result quality
  - sources_used: list of tool names + row IDs that were actually called

This approach is more robust than NativeOutput/PromptedOutput for Ollama-hosted
models because the LLM only has one job: write good text. The structured
metadata is assembled from ground truth (tool results) rather than being
invented by the LLM.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from app.agent.hallucination.citation_markers import CITATION_MARKER_RE
from app.agent.llm_calls import get_run_llm_model
from app.agent.public_geoscience_tool import (
    PublicGeoscienceRecord,
    PublicGeoscienceSearchResult,
)
from app.agent.tools import (
    AssayDataResult,
    CollarDetailsResult,
    CoverageGapResult,
    DocumentSearchResult,
    DownholeLogsResult,
    DrillTrace3DResult,
    GraphTraversalResult,
    ProjectOverviewResult,
    ProjectSummaryResult,
    SpatialQueryResult,
    StereonetResult,
)
from app.config import settings
from app.models.rag import Citation, GeoRAGResponse, MapPayload, VizPayload

logger = logging.getLogger(__name__)

# Pattern to find existing citation markers ([DATA-X]/[DATA:X], [NI43-X], etc.)
# in LLM output so we can detect whether the model placed inline markers or not
# (shared pattern — see citation_markers.py for the colon/dash rationale).
_CITATION_MARKER_RE = CITATION_MARKER_RE

# Sentinel source_chunk_id values that carry NO real upstream evidence — the
# synthetic no-tool-call placeholder plus the empty-retrieval sentinels the
# assembler emits when a search tool returned zero hits. Shared between the
# IND-6 ungrounded-answer guard here and
# ``confidence_computer._count_independent_sources`` so the two filters can
# never drift again (RAG-quality audit 2026-08-14, finding 3 — an empty
# DocumentSearchResult's "georag_reports:empty" citation used to pass IND-6
# while being excluded from the independent-source count).
EMPTY_SOURCE_SENTINELS: frozenset[str] = frozenset({
    "no-tool-call",
    "georag_reports:empty",
    "pg_public_geoscience:empty",
    "silver.collars:miss",
})

#: Suffixes `_extract_source_id` mints when a result carried no rows.
#:
#: The zero-row id is structural, not a fixed sentinel: an assay lookup that
#: found nothing yields `silver.samples:element=U3O8:count=0`, a spatial one
#: `silver.collars:count=0`, a graph one `neo4j:count=0`. Enumerating those
#: as literals is what left the set covering three shapes out of eleven.
_EMPTY_SOURCE_SUFFIXES: tuple[str, ...] = (
    ":count=0",          # assays, spatial collars, neo4j
    ":rows=0:first_row=none",   # ADR-0007 project summary card
)

#: Substrings that mark a zero-row card result.
_EMPTY_SOURCE_MARKERS: tuple[str, ...] = (
    ":holes=0:",         # ADR-0007 drill-traces card
    ":curves=0:reports=0",   # project overview with no metadata at all
)


def is_empty_source_id(source_id: str) -> bool:
    """True when this source id came from a tool result carrying no rows.

    Used by BOTH second-line filters -- the IND-6 ungrounded-answer guard
    below and `confidence_computer._count_independent_sources` -- because a
    Citation carries only the id string, not the result it came from.

    `test_empty_source_ids.py` builds an empty instance of every type
    `_extract_source_id` handles and requires this to agree with
    `_is_empty_tool_result`. That test, not this function, is what stops
    the next result type being silently uncovered.

    The lithology case is the one that needs a condition rather than a
    suffix: `silver.lithology_logs:hole=X:collar=Y:intervals=0` is a hole
    with a collar and no logged intervals, which `_is_empty_tool_result`
    deliberately treats as NON-empty -- collar metadata alone answers
    "tell me about hole X". Only the collar-less form is empty.
    """
    if not source_id:
        return True
    if source_id in EMPTY_SOURCE_SENTINELS:
        return True
    if source_id.endswith(_EMPTY_SOURCE_SUFFIXES):
        return True
    if any(marker in source_id for marker in _EMPTY_SOURCE_MARKERS):
        return True
    return source_id.endswith(":intervals=0") and ":collar=" not in source_id


def assign_citation_ids(
    tool_results: list[tuple[str, Any]],
) -> list[list[str]]:
    """Pre-compute the citation_ids each tool result will yield.

    Returns a list parallel to tool_results. Each inner list holds the
    citation_ids that tool result will contribute — typically exactly one,
    EXCEPT for:

      * ``PublicGeoscienceSearchResult`` — one citation per record (plan
        §04i Layer 5 — every cited fact must trace to exactly one upstream
        record, not to the first-record-of-the-tool-call).
      * ``DocumentSearchResult`` with chunks — one citation PER CHUNK
        (RAG-quality audit 2026-08-14, finding 1: all 12 chunks of a
        search_documents call used to share one ``[NI43-n]``, so chips /
        pages / Layer-5 provenance always described chunk 1 regardless of
        which chunk actually grounded the sentence). An EMPTY
        DocumentSearchResult still yields one sentinel id so the
        empty-retrieval citation ("georag_reports:empty") survives for the
        IND-6 refusal path.

    Called by ``_render_tool_results_context`` so the LLM prompt can tag
    each chunk/record with the exact marker the assembler will emit, and by
    ``assemble_response`` so the citation objects use those same ids. The
    two MUST stay in lockstep — Layer 2 strips any marker without a
    matching Citation (layer2_typed_output.py).

    A shared counter across tool-result types matches the existing
    interleaved behavior — two consecutive DATA then NI43 tool results
    yield ``[DATA-1]`` ``[NI43-2]``.
    """
    out: list[list[str]] = []
    counter = 0
    for tool_name, result in tool_results:
        if isinstance(result, PublicGeoscienceSearchResult):
            ids: list[str] = []
            for _ in result.records:
                counter += 1
                ids.append(f"[PGEO-{counter}]")
            out.append(ids)
        elif isinstance(result, DocumentSearchResult) and result.chunks:
            ids = []
            for chunk in result.chunks:
                counter += 1
                ids.append(f"[{_citation_type_for_chunk(chunk)}-{counter}]")
            out.append(ids)
        else:
            counter += 1
            cit_type = _citation_type_for_tool(tool_name, result)
            out.append([f"[{cit_type}-{counter}]"])
    return out


def _citation_type_for_chunk(chunk: Any) -> Literal["NI43", "PUB"]:
    """Citation type for ONE document chunk (per-chunk citation path).

    Mirrors the first-chunk logic in ``_citation_type_for_tool`` but at
    chunk granularity, so a mixed result set (PUB + NI43 chunks) labels
    each chunk by its own document_type.
    """
    dtype = (getattr(chunk, "document_type", "") or "").upper()
    return "PUB" if dtype == "PUB" else "NI43"


def _citation_type_for_tool(
    tool_name: str, result: Any
) -> Literal["DATA", "NI43", "PUB", "PGEO"]:
    """Determine the citation type for a given tool result.

    - DocumentSearchResult with NI43/NI 43-101 document_type → "NI43"
    - DocumentSearchResult with PUB document_type → "PUB"
    - PublicGeoscienceSearchResult → "PGEO" (plan §08 jurisdiction-aware citation)
    - Everything else (spatial queries, graph traversal) → "DATA"

    The document_type field in each DocumentChunk payload is set by the Dagster
    index_reports asset at indexing time. We inspect the first chunk's type as
    representative of the whole result set (all chunks in a single
    search_documents call come from the same collection and typically the same
    report or similar report types).
    """
    if isinstance(result, PublicGeoscienceSearchResult):
        return "PGEO"
    if isinstance(result, DocumentSearchResult):
        if result.chunks:
            dtype = result.chunks[0].document_type.upper()
            if dtype == "PUB":
                return "PUB"
        return "NI43"
    return "DATA"


def assemble_response(
    text: str,
    tool_results: list[tuple[str, Any]],
    map_payload: MapPayload | None = None,
    viz_payload: VizPayload | None = None,
) -> GeoRAGResponse:
    """Build a GeoRAGResponse from LLM text and the list of tool call results.

    Args:
        text: The free-form text the LLM generated.
        tool_results: List of (tool_name, result) tuples from ctx.messages
            extraction. Each result is a dataclass like SpatialQueryResult.

    Returns:
        A valid GeoRAGResponse with citations derived from tool calls and
        confidence computed from result quality.

    Citation type mapping (hallucination Layer 2):
      - DocumentSearchResult  → citation_type="NI43" or "PUB", id prefix [NI43-X] / [PUB-X]
      - SpatialQueryResult    → citation_type="DATA", id prefix [DATA-X]
      - GraphTraversalResult  → citation_type="DATA", id prefix [DATA-X]

    If the LLM text contains no citation markers, we append them to the end so
    the text + citations list stay consistent.
    """
    citations: list[Citation] = []
    sources_used: list[str] = []  # all chunk IDs involved (cited + retrieved)

    # Pre-assign citation_ids so that (a) PGEO results get one id per record,
    # (b) the ids here are identical to what _build_context wrote into the
    # LLM prompt, and (c) the assembler is purely deterministic — no hidden
    # counter reset.
    id_bundles = assign_citation_ids(tool_results)

    for (tool_name, result), bundle in zip(tool_results, id_bundles, strict=False):
        if isinstance(result, PublicGeoscienceSearchResult):
            # Emit one Citation per record so each cited fact traces to the
            # exact upstream entity, not to record[0] (plan §04i Layer 5).
            for record, citation_id in zip(result.records, bundle, strict=False):
                source_chunk_id = _source_chunk_id_for_pg_record(record)
                citations.append(
                    Citation(
                        citation_id=citation_id,
                        citation_type="PGEO",
                        source_chunk_id=source_chunk_id,
                        document_title=_pg_record_title(record),
                        section=None,
                        page=None,
                        relevance_score=float(record.relevance_score or 0.0),
                        corpus="public_geo",
                        jurisdiction_code=record.jurisdiction_code or None,
                        jurisdiction_name=record.jurisdiction_name,
                        license_summary=record.license_summary,
                        license_url=record.license_url,
                        source_url=record.source_url,
                        staleness_seconds=record.staleness_seconds,
                    )
                )
                sources_used.append(source_chunk_id)
            continue

        if isinstance(result, DocumentSearchResult) and result.chunks:
            # Per-chunk citations (audit 2026-08-14 finding 1) — one
            # Citation per retrieved chunk so each [NI43-n]/[PUB-n] maps to
            # the REAL chunk_id / section / page that grounded it, mirroring
            # the PGEO per-record branch above. Ids come from the same
            # ``assign_citation_ids`` call the context renderer used, so the
            # prompt markers and the emitted citations stay in lockstep.
            for chunk, citation_id in zip(result.chunks, bundle, strict=False):
                source_chunk_id = _source_chunk_id_for_doc_chunk(chunk)
                section_label, page = _section_page_for_chunk(chunk)
                citations.append(
                    Citation(
                        citation_id=citation_id,
                        citation_type=_citation_type_for_chunk(chunk),
                        source_chunk_id=source_chunk_id,
                        document_title=chunk.document_title,
                        section=section_label,
                        page=page,
                        relevance_score=float(chunk.relevance_score or 0.0),
                        corpus="internal_archive",
                    )
                )
                sources_used.append(source_chunk_id)
            continue

        # Non-PG path — exactly one citation per tool result.
        citation_id = bundle[0]
        cit_type = _citation_type_for_tool(tool_name, result)
        source_chunk_id = _extract_source_id(tool_name, result)
        document_title = _extract_document_title(tool_name, result)
        relevance_score = _extract_relevance(result)
        section, page = _extract_section_page(result)

        citations.append(
            Citation(
                citation_id=citation_id,
                citation_type=cit_type,
                source_chunk_id=source_chunk_id,
                document_title=document_title,
                section=section,
                page=page,
                relevance_score=relevance_score,
                corpus="internal_archive",
            )
        )
        sources_used.append(source_chunk_id)

    # The answer text is returned as written. This used to staple every
    # citation id onto the last sentence whenever the model emitted none:
    # a five-sentence geological interpretation with no markers came back
    # reading "… [NI43-1] [NI43-2] [DATA-3]." and the frontend rendered
    # three citation chips. Every claim then LOOKED sourced while no claim
    # was mapped to any chunk, and a fabricated sentence was indistinguishable
    # from a grounded one.
    #
    # It also destroyed the signal: an answer with no markers is a detectable
    # failure, and stapling markers on made it undetectable. classify_guards
    # now raises CITATION_INCOMPLETE for exactly this state (see the
    # text_has_markers argument), which is what CLAUDE.md hard rule 4 needs in
    # order to be enforceable at all.

    # Fallback citation if the LLM produced text but no tools were called.
    if not citations:
        citations.append(
            Citation(
                citation_id="[DATA-1]",
                citation_type="DATA",
                source_chunk_id="no-tool-call",
                document_title="No tool call executed",
                section=None,
                page=None,
                relevance_score=0.0,
            )
        )
        sources_used.append("no-tool-call")
        # No marker is appended for the placeholder either. GeoRAGResponse
        # requires at least one Citation, so the sentinel exists to satisfy
        # the type — writing its marker into the answer would present "no
        # tool call executed" to the reader as a source.

    # Compute confidence from tool result quality AND answer text.
    # Refusal responses get low confidence even when tools succeeded.
    confidence = _compute_confidence(tool_results, text=text)

    # Apply qualitative claim penalty — vague geological assertions
    # reduce confidence to signal the answer needs verification.
    from app.agent.hallucination.qualitative_detector import (
        confidence_penalty,
        detect_qualitative_claims,
    )
    qual_claims = detect_qualitative_claims(text)
    qual_penalty = confidence_penalty(qual_claims)
    if qual_penalty > 0:
        confidence = max(0.1, confidence - qual_penalty)

    # Phase 1 / Step 1.2 — OIUR parse, flag-gated. Behaviour with the flag
    # OFF is byte-identical to the legacy path: geo_answer stays None and
    # the flat ``text`` field is the sole answer payload. Flag ON: try to
    # parse the LLM markdown into a GeoAnswer; on any parser warning the
    # legacy path is the fallback (geo_answer=None, flat text unchanged).
    #
    # Phase 1 / Step 1.3 — when the parse succeeds, override the LLM's
    # emitted confidence Level with a rule-based Stage-1 computation from
    # retrieval signals. Stage 2 (guard demotion) runs later in the
    # orchestrator after run_post_assembly_validation.
    # Audit 2026-06-28 (IND-6, Hard Rule 4): deterministic ungrounded-answer
    # guard. If NO real evidence backs the answer — sources_used is empty or
    # holds only synthetic placeholders — the answer is ungrounded and must
    # NOT ship at normal confidence, regardless of whether the LLM happened
    # to phrase it as a refusal. Floor confidence hard so the downstream
    # demotion/UI surfaces it as untrusted (the citation-first generator,
    # when restored, is the proper salvage path).
    #
    # Audit 2026-08-14 (finding 3): the filter now uses the SAME sentinel
    # set as confidence_computer._count_independent_sources — previously it
    # excluded only 'no-tool-call', so an empty DocumentSearchResult's
    # 'georag_reports:empty' citation counted as real evidence and a
    # zero-hit retrieval shipped as a normal-confidence answer.
    _real_sources = [s for s in sources_used if not is_empty_source_id(s)]
    if not _real_sources and not _is_refusal(text):
        logger.warning(
            "assemble_response: ungrounded answer (no real sources_used; "
            "citations=%d) — flooring confidence (IND-6 guard).",
            len(citations),
        )
        confidence = min(confidence, 0.05)

    geo_answer = _maybe_parse_geo_answer(
        text, citations=citations, refusal=_is_refusal(text)
    )

    return GeoRAGResponse(
        text=text.strip(),
        citations=citations,
        map_payload=map_payload,
        viz_payload=viz_payload,
        confidence=confidence,
        sources_used=sources_used,
        geo_answer=geo_answer,
        degraded_sources=_collect_degraded_sources(tool_results),
        # The model that actually produced `text`, read from the per-run
        # contextvar `_call_llm` sets on every answer-producing call. Laravel
        # persists it to query_audit_log.llm_model; see the block above
        # `record_run_llm_model` in llm_calls.py for why the previous
        # mechanism (a "routing" SSE frame) never delivered a value.
        llm_model=get_run_llm_model(),
    )


#: Markers a tool stamps into ``data_source`` when it returned partial or
#: no data. The orchestrator deliberately falls through on a tool failure
#: — "partial data is always preferable to a hard failure" — which is the
#: right call, but it left the user unable to tell a thin answer from a
#: complete one.
_DEGRADED_MARKERS: tuple[str, ...] = (
    "(timeout)",
    "(error)",
    "(rerank unavailable)",
)


def _collect_degraded_sources(tool_results: list[tuple[str, Any]]) -> list[str]:
    """Human-readable labels for retrieval surfaces that did not fully work.

    GeoRAGResponse.degraded_sources has existed since the C7 audit, with a
    description of the warning chip the frontend would render from it. Until
    now nothing ever wrote to it: it defaulted to an empty list on every
    response, which the UI reads as "all sources succeeded". So a Qdrant
    timeout and a clean retrieval produced identical-looking answers, and a
    reranker outage was invisible in the response entirely — the data_source
    string still said "(reranked)".

    Deliberately derived from the tool results rather than threaded through
    as a parameter: a tool that learns to report its own degradation is then
    surfaced without touching this function or its callers.
    """
    labels: list[str] = []

    for tool_name, result in tool_results:
        if getattr(result, "rerank_degraded", False):
            # One label per surface. This result's data_source also carries
            # the "(rerank unavailable)" marker, and reporting both would
            # tell the reader the same thing twice in different words.
            labels.append("Document ranking (reranker unavailable)")
            continue

        source = str(getattr(result, "data_source", "") or "")
        if any(marker in source for marker in _DEGRADED_MARKERS):
            labels.append(f"{source} via {tool_name}")

    # Stable order, no duplicates — two tools hitting the same dead backend
    # is one degraded source to a reader, not two.
    return sorted(set(labels))


def _maybe_parse_geo_answer(
    text: str,
    *,
    citations: list[Citation],
    refusal: bool,
):
    """Attempt OIUR parse when the feature flag is on, then apply Stage-1
    rule-based confidence Level.

    Returns a ``GeoAnswer`` or None. None covers three cases:
      1. flag disabled — no parse attempted (default rollout state)
      2. refusal answer — the refusal path already produced a flat-text
         answer; the OIUR schema does not model refusals
      3. parse failure — assembler falls back to the flat-text path

    Imports are local so the module stays importable when the agent
    schema package is missing (e.g. during incremental dev rebases).
    """
    if not getattr(settings, "GEO_ANSWER_OIUR_ENABLED", False):
        return None
    if refusal:
        return None
    try:
        from app.agent.oiur_parser import parse_oiur_markdown
    except Exception:  # pragma: no cover — defensive
        logger.exception("assemble_response: oiur_parser import failed")
        return None
    answer, warnings = parse_oiur_markdown(text)
    if warnings:
        logger.info(
            "assemble_response: OIUR parse produced %d warning(s) (geo_answer=%s): %s",
            len(warnings),
            "present" if answer else "None",
            "; ".join(warnings[:5]),
        )
    if answer is None:
        return None

    # Step 1.3 — override the LLM-emitted Level with a rule-based value
    # computed from retrieval signals. The LLM's prose reason and drivers
    # are preserved.
    try:
        from app.agent.confidence_computer import (
            apply_level_to_geo_answer,
            compute_initial_level,
        )
    except Exception:  # pragma: no cover — defensive
        logger.exception("assemble_response: confidence_computer import failed")
        return answer
    initial_level, note = compute_initial_level(citations)
    if (
        hasattr(answer.uncertainty, "confidence")
        and answer.uncertainty.confidence.level != initial_level  # type: ignore[union-attr]
    ):
        logger.info(
            "assemble_response: rule-based Level %s overrides LLM-emitted %s (%s)",
            initial_level,
            answer.uncertainty.confidence.level,  # type: ignore[union-attr]
            note,
        )
    return apply_level_to_geo_answer(answer, initial_level)


def _extract_source_id(tool_name: str, result: Any) -> str:
    """Extract a stable source identifier from a tool result.

    For DocumentSearchResult the source_chunk_id encodes the Qdrant point ID of
    the top-ranked chunk plus the report_id so the Laravel layer can resolve the
    full provenance chain (hallucination Layer 5 — chunk provenance).
    """
    if isinstance(result, AssayDataResult):
        return (
            f"silver.samples:element={result.element}"
            f":count={result.count}"
        )
    if isinstance(result, DownholeLogsResult):
        if result.collar:
            return (
                f"silver.lithology_logs:hole={result.collar.hole_id}"
                f":collar={result.collar.collar_id}"
                f":intervals={result.count}"
            )
        return f"silver.lithology_logs:intervals={result.count}"
    if isinstance(result, CollarDetailsResult):
        if result.collar_id:
            return (
                f"silver.collars:hole={result.hole_id or 'unknown'}"
                f":collar={result.collar_id}"
                f":assays={result.assay_count}"
                f":litho={result.lithology_count}"
            )
        return "silver.collars:miss"
    if isinstance(result, SpatialQueryResult):
        if result.collars:
            return f"silver.collars:count={result.count}:first={result.collars[0].collar_id}"
        return f"silver.collars:count={result.count}"
    if isinstance(result, DocumentSearchResult):
        if result.chunks:
            first = result.chunks[0]
            section_part = (
                f"section={first.section_number}"
                if first.section_number
                else "section=unknown"
            )
            return f"georag_reports:{first.report_id}:{section_part}:chunk={first.chunk_id}"
        return "georag_reports:empty"
    if isinstance(result, GraphTraversalResult):
        if result.entities:
            return f"neo4j:entities={result.count}:first={result.entities[0].entity_id}"
        return f"neo4j:count={result.count}"
    if isinstance(result, ProjectOverviewResult):
        return (
            f"silver.projects:slug={result.slug or 'unknown'}"
            f":company={result.company or 'unknown'}"
            f":curves={len(result.distinct_curves)}"
            f":reports={result.report_count}"
        )
    if isinstance(result, ProjectSummaryResult):
        # ADR-0007 PR-1 — citation binds to the breakdown rowset so the
        # citation guard can verify any quoted count / metric against the
        # tool result, not the LLM's paraphrase. The first row's IDs are
        # the deepest-link anchor; the per-row source_row_ids stay on the
        # ProjectSummaryResult itself for the validator's row-level pass.
        first_ids = ""
        if result.technique_breakdown and result.technique_breakdown[0].source_row_ids:
            first_ids = result.technique_breakdown[0].source_row_ids[0]
        return (
            f"silver.project_summary:project={result.project_id}"
            f":rows={result.count}:first_row={first_ids or 'none'}"
        )
    if isinstance(result, CoverageGapResult):
        return (
            f"silver.coverage_gap:project={result.project_id}"
            f":indexed={result.ingest_gap.indexed}"
            f":processed={result.ingest_gap.processed}"
            f":attrs={len(result.attribute_coverage)}"
        )
    if isinstance(result, DrillTrace3DResult):
        # ADR-0007 PR-4 — citation binds to the project's drill_traces
        # set so the validator can verify any quoted hole_id / coordinate
        # against the tool result. source_row_ids on the result itself
        # carry the per-collar / per-interval / per-structure detail
        # used by the row-level pass.
        first_collar = result.collars[0].collar_id if result.collars else "none"
        return (
            f"silver.drill_traces:project={result.project_id}"
            f":holes={result.count}"
            f":first_collar={first_collar}"
            f":hole_filter={result.hole_id_filter or 'all'}"
        )
    if isinstance(result, StereonetResult):
        # ADR-0007 PR-2 — citation binds to the project's stereonet
        # rowset. Per-point source_row_ids stay on the StereonetPoint
        # objects for the row-level validator.
        first_pt = result.points[0].source_row_id if result.points else "none"
        return (
            f"gold.structure_measurements_visual:project={result.project_id}"
            f":points={result.count}:first={first_pt or 'none'}"
        )
    if isinstance(result, PublicGeoscienceSearchResult):
        if result.records:
            first = result.records[0]
            # Format: pg_<canonical_type>:<source_id>:feature=<source_feature_id>:pg_id=<uuid>
            # Parsed by Laravel CitationController::resolve() prefix routing
            # (plan §08 two-stage citation model).
            return (
                f"pg_{first.canonical_type}:{first.source_id}"
                f":feature={first.source_feature_id or 'unknown'}"
                f":pg_id={first.pg_id}"
            )
        return "pg_public_geoscience:empty"
    return f"{tool_name}:result"


def _source_chunk_id_for_doc_chunk(chunk: Any) -> str:
    """Canonical source_chunk_id for ONE document chunk.

    Same ``georag_reports:<report_id>:section=..:chunk=..`` format as the
    legacy first-chunk path in ``_extract_source_id`` — Laravel
    ``CitationController::resolve()`` and Layer 5 provenance both parse it —
    but computed per chunk so each citation binds to the chunk that actually
    grounded it (audit 2026-08-14 finding 1).
    """
    section_part = (
        f"section={chunk.section_number}"
        if chunk.section_number
        else "section=unknown"
    )
    return f"georag_reports:{chunk.report_id}:{section_part}:chunk={chunk.chunk_id}"


def _section_page_for_chunk(chunk: Any) -> tuple[str | None, int | None]:
    """Return (section_label, page_number) for ONE document chunk."""
    if chunk.section_number and chunk.section_title:
        section_label: str | None = f"{chunk.section_number} — {chunk.section_title}"
    elif chunk.section_title:
        section_label = chunk.section_title
    elif chunk.section_number:
        section_label = chunk.section_number
    else:
        section_label = None
    return section_label, chunk.page


def _extract_section_page(result: Any) -> tuple[str | None, int | None]:
    """Return (section_label, page_number) for the top result chunk, or (None, None)."""
    if isinstance(result, DocumentSearchResult) and result.chunks:
        return _section_page_for_chunk(result.chunks[0])
    return None, None


def _extract_document_title(tool_name: str, result: Any) -> str:
    """Extract a human-readable title from a tool result."""
    if isinstance(result, AssayDataResult):
        return f"Assay data — {result.element} ({result.count} samples)"
    if isinstance(result, DownholeLogsResult):
        hole = result.collar.hole_id if result.collar else "unknown"
        return f"Lithology log for {hole} ({result.count} intervals)"
    if isinstance(result, CollarDetailsResult):
        if not result.collar_id:
            return "Hole lookup — no match"
        depth_str = (
            f"{result.total_depth:.1f}m"
            if result.total_depth is not None
            else "unknown depth"
        )
        kind = result.drill_type or result.hole_type or "hole"
        return f"Hole {result.hole_id or 'unknown'} — {kind} · {depth_str}"
    if isinstance(result, SpatialQueryResult):
        return f"Drill collars from PostGIS ({result.count} records)"
    if isinstance(result, DocumentSearchResult):
        if result.chunks:
            return result.chunks[0].document_title
        return "Qdrant document search (no results)"
    if isinstance(result, GraphTraversalResult):
        return f"Neo4j knowledge graph ({result.count} entities)"
    if isinstance(result, ProjectOverviewResult):
        name = result.project_name or "Project overview"
        return (
            f"{name} — {result.collar_count} hole(s), "
            f"{len(result.distinct_curves)} log curve(s), "
            f"{result.report_count} report(s)"
        )
    if isinstance(result, ProjectSummaryResult):
        return (
            f"Data collection breakdown — {result.count} bucket(s) "
            f"across campaigns / collars / geophysics / reports"
        )
    if isinstance(result, CoverageGapResult):
        return (
            f"Coverage gap analysis — "
            f"{result.ingest_gap.indexed} indexed / "
            f"{result.ingest_gap.processed} processed; "
            f"{len(result.attribute_coverage)} attribute coverage row(s); "
            f"{len(result.findings)} finding(s)"
        )
    if isinstance(result, DrillTrace3DResult):
        if result.hole_id_filter and result.collars:
            return (
                f"3D drill trace — {result.collars[0].hole_id} "
                f"({len(result.intervals)} interval(s), "
                f"{len(result.structures)} structure(s))"
            )
        return (
            f"3D drill traces — {result.count} hole(s), "
            f"{len(result.intervals)} interval(s), "
            f"{len(result.structures)} structure(s)"
        )
    if isinstance(result, StereonetResult):
        return f"Stereonet — {result.count} structural measurement(s)"
    if isinstance(result, PublicGeoscienceSearchResult):
        if result.records:
            first = result.records[0]
            juris = first.jurisdiction_name or first.jurisdiction_code or "Public Geoscience"
            return f"{juris} — {first.name}"
        return "Public Geoscience search (no results)"
    return f"Result from {tool_name}"


def _extract_relevance(result: Any) -> float:
    """Extract an average relevance score from a tool result."""
    if isinstance(result, AssayDataResult):
        return 1.0 if result.count > 0 else 0.0
    if isinstance(result, DownholeLogsResult):
        return 1.0 if result.count > 0 else 0.0
    if isinstance(result, CollarDetailsResult):
        return 1.0 if result.count > 0 else 0.0
    if isinstance(result, SpatialQueryResult):
        # Spatial queries are deterministic — if the tool returned data it is
        # 100% relevant to the query that triggered it.
        return 1.0 if result.count > 0 else 0.0
    if isinstance(result, DocumentSearchResult):
        if not result.chunks:
            return 0.0
        scores = [c.relevance_score for c in result.chunks]
        return sum(scores) / len(scores)
    if isinstance(result, GraphTraversalResult):
        return 1.0 if result.count > 0 else 0.0
    if isinstance(result, ProjectOverviewResult):
        # Project metadata is deterministic structured data — 100% relevant
        # to the query that triggered it. The empty-result filter (F.4)
        # already drops cases where the project has neither metadata nor
        # curves, so we never reach here with an actually-empty result.
        return 1.0 if (result.count or 0) > 0 else 0.0
    if isinstance(result, ProjectSummaryResult):
        return 1.0 if result.count > 0 else 0.0
    if isinstance(result, CoverageGapResult):
        return 1.0 if result.count > 0 else 0.0
    if isinstance(result, DrillTrace3DResult):
        return 1.0 if result.count > 0 else 0.0
    if isinstance(result, StereonetResult):
        return 1.0 if result.count > 0 else 0.0
    if isinstance(result, PublicGeoscienceSearchResult):
        if not result.records:
            return 0.0
        scores = [r.relevance_score for r in result.records]
        return sum(scores) / len(scores)
    return 0.5


def _source_chunk_id_for_pg_record(record: PublicGeoscienceRecord) -> str:
    """Canonical source_chunk_id for one PG record.

    Format parsed by Laravel ``CitationController::resolve()`` prefix
    routing — see plan §08 two-stage citation model.
    """
    return (
        f"pg_{record.canonical_type}:{record.source_id}"
        f":feature={record.source_feature_id or 'unknown'}"
        f":pg_id={record.pg_id}"
    )


def _pg_record_title(record: PublicGeoscienceRecord) -> str:
    """Jurisdiction-qualified display title for one PG record.

    Matches the shape the chat UI expects on Citation.document_title —
    "{jurisdiction} — {entity name}".
    """
    juris = record.jurisdiction_name or record.jurisdiction_code or "Public Geoscience"
    name = record.name or f"{record.canonical_type.replace('_', ' ').title()} record"
    return f"{juris} — {name}"


# Phrases that indicate the LLM is refusing to answer due to insufficient data
# OR refusing because the user's question contained a physically impossible
# premise (P1 wave-4 follow-up — the NUMERIC system prompt now teaches the
# model to refuse + correct queries like "above 500% uranium").
#
# 2026-08-21 — split into two tiers, because this was one tuple scanned as
# unanchored substrings over the ENTIRE answer, and half of it is ordinary
# geological vocabulary. "no data", "not found", "insufficient", "not
# available" and "no drill hole" appear in most real, well-grounded,
# fully-cited answers, because most real answers say what they could not
# establish. Every one of those was classified as a refusal and had its
# confidence forced to 0.1:
#
#   "Hole PLS-22-08 returned 1.85 g/t Au over 12.5 m [DATA-1]. Core
#    recovery data is not available for the upper 40 m [NI43-2]."
#
# That answer is the behaviour the citation contract asks for, and it scored
# the same as "I don't have that." It also defeated the safeguard the
# starts-with branch below already carried — its comment says it exists so
# "No drill holes intersected mineralisation" is read as an answer rather
# than a refusal, and then "no drill hole" in this tuple overrode it.

#: Refusals that name the ASSISTANT, or state a physical impossibility.
#: Unambiguous wherever they land, so these are scanned over the whole body.
_REFUSAL_PHRASES_ANYWHERE = (
    "i don't have",
    "i do not have",
    "don't have data",
    "do not have data",
    "not a possible value",
    "physically impossible",
    "beyond physical",
    "impossible value",
    # Phase G follow-up — scope-refusal patterns. These don't say "no
    # data" but they DO refuse to answer (e.g. when asked for PII,
    # weather, or other out-of-scope content).
    "i can only answer geological",
    "i can only answer questions",
    "only geological questions",
)

#: Ordinary geological vocabulary that only means refusal when the answer
#: LEADS with it. A refusal opens with the refusal; an answer mentions the
#: gap after it has said what it does know.
_REFUSAL_PHRASES_OPENING = (
    "no data",
    "insufficient",
    "unable to",
    "cannot find",
    "can't find",
    "not found",
    "not in the database",
    "no record",
    "no information",
    "not available",
    "out of scope",
    # Impossible-premise refusal shapes from the NUMERIC few-shots.
    #
    # "no drill hole" and "well beyond" used to be here and are deliberately
    # gone. "No drill holes intersected mineralisation above the cut-off" is
    # an ANSWER, and it is the exact sentence the starts-with branch below
    # documents itself as protecting — the phrase list overrode that branch
    # for months. The genuine refusal shape ("No drill hole CAN be 3,000 m
    # deep") is caught by that branch instead, which requires a can/is verb.
    # "well beyond" is likewise ordinary prose; "beyond physical" and
    # "physically impossible" carry the impossible-premise meaning.
    "no hole can",
    "not possible",
)

#: Union, kept so anything reasoning about "the refusal vocabulary" as a
#: whole still has one name for it.
_REFUSAL_PHRASES = _REFUSAL_PHRASES_ANYWHERE + _REFUSAL_PHRASES_OPENING

#: Below this length an answer IS its refusal — there is no room for it to
#: have said something substantive first — so the opening tier is scanned
#: over the whole text. This is what keeps two-sentence refusals like
#: "I checked silver.collars for the Triple R zone. No records were
#: returned." detectable without re-admitting the false positives, which
#: are long, cited answers.
_SHORT_ANSWER_CHARS = 400


def _first_sentence(lower: str) -> str:
    """First sentence of an already-lowercased answer.

    Splits on sentence-ending punctuation FOLLOWED BY WHITESPACE, so a
    decimal does not end a sentence. `lower.split(".", 1)[0]` cut
    "1.85 g/t Au over 12.5 m" down to "1", which made every grade-bearing
    opening sentence unreadable to the checks below.
    """
    return re.split(r"(?<=[.!?])\s+", lower, maxsplit=1)[0]


def _is_refusal(text: str) -> bool:
    """Detect whether the LLM answer is a refusal rather than a real answer.

    Three detection paths:
      1. Substring match against _REFUSAL_PHRASES_ANYWHERE — first-person
         and physical-impossibility forms, unambiguous anywhere in the body.
      2. _REFUSAL_PHRASES_OPENING in the first sentence (or anywhere, if
         the whole answer is shorter than _SHORT_ANSWER_CHARS), and only
         when the answer carries no citation markers at all. These are
         ordinary geological words; whether the answer grounded anything,
         and where the words sit, is what separates a refusal from an
         answer that reports a gap.
      3. Starts-with refusal preamble — the system prompt's RULE 10
         (impossible-premise) instructs models to BEGIN refusals with "No"
         or "That's not possible". A leading "No <noun> can be / cannot be"
         is a much more reliable signal of refusal than any single phrase.
    """
    if not text:
        return False
    lower = text.lower().lstrip()
    if any(phrase in lower for phrase in _REFUSAL_PHRASES_ANYWHERE):
        return True

    # Starts-with refusal preambles (system prompt RULE 10 emits these).
    # Anchored on the FIRST sentence so a body paragraph that happens to
    # contain "no" doesn't trip the heuristic.
    first_sentence = _first_sentence(lower)

    # The opening tier is ordinary geological vocabulary, so it is only
    # consulted when the answer has grounded nothing. A refusal cites
    # nothing by construction; an answer that carries citation markers AND
    # says what it could not establish is a qualified answer — which is
    # precisely the behaviour CLAUDE.md rule 4 asks for, and it was being
    # scored identically to "I don't have that."
    #
    # A first-person hedge that does cite ("I searched X [NI43-1] but ...")
    # still reads as a refusal via the ANYWHERE tier above.
    if not CITATION_MARKER_RE.search(text):
        opening_scope = (
            lower if len(lower) <= _SHORT_ANSWER_CHARS else first_sentence
        )
        if any(phrase in opening_scope for phrase in _REFUSAL_PHRASES_OPENING):
            return True

    refusal_preambles = (
        "no ",
        "that's not possible",
        "that is not possible",
        "no, ",
        "no.",
    )
    if any(first_sentence.startswith(p) for p in refusal_preambles):
        # Plus a sanity check — the first sentence must contain a "can" or
        # "is" verb so we don't mis-fire on negative numeric claims like
        # "No drill holes intersected mineralisation" (which is an answer,
        # not a refusal).
        if any(verb in first_sentence for verb in (" can ", " cannot", " can't ", " is ", " are ")):
            return True
    return False


def _compute_confidence(tool_results: list[tuple[str, Any]], text: str = "") -> float:
    """Compute overall response confidence from tool result quality AND answer text.

    A refusal response ("I don't have data on that") must have LOW confidence
    even if the tools returned lots of data — the data was retrieved but did
    not contain what the user asked for. This is critical for the hallucination
    prevention contract: confidence must reflect answer quality, not just
    retrieval quality.
    """
    # Layer A: refusal detection overrides everything else.
    if text and _is_refusal(text):
        return 0.1

    # Layer B: no tool calls = no grounding = low confidence.
    if not tool_results:
        return 0.1

    # Layer C: average tool relevance, capped at 0.95.
    relevances = [_extract_relevance(r) for _, r in tool_results]
    avg_relevance = sum(relevances) / len(relevances)
    return min(0.95, avg_relevance)
