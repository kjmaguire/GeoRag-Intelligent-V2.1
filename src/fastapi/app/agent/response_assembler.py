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

# Pattern to find existing citation markers ([DATA-X], [NI43-X], [PUB-X],
# [PGEO-X]) in LLM output so we can detect whether the model placed inline
# markers or not.
_CITATION_MARKER_RE = re.compile(r"\[(?:DATA|NI43|PUB|PGEO)-\d+\]")


def assign_citation_ids(
    tool_results: list[tuple[str, Any]],
) -> list[list[str]]:
    """Pre-compute the citation_ids each tool result will yield.

    Returns a list parallel to tool_results. Each inner list holds the
    citation_ids that tool result will contribute — typically exactly one,
    EXCEPT for ``PublicGeoscienceSearchResult`` where we emit one citation
    per record (plan §04i Layer 5 — every cited fact must trace to exactly
    one upstream record, not to the first-record-of-the-tool-call).

    Called by ``_build_context`` so the LLM prompt can tag each PG record
    with the exact ``[PGEO-N]`` marker the assembler will emit, and by
    ``assemble_response`` so the citation objects use those same ids.

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
        else:
            counter += 1
            cit_type = _citation_type_for_tool(tool_name, result)
            out.append([f"[{cit_type}-{counter}]"])
    return out


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

    for (tool_name, result), bundle in zip(tool_results, id_bundles):
        if isinstance(result, PublicGeoscienceSearchResult):
            # Emit one Citation per record so each cited fact traces to the
            # exact upstream entity, not to record[0] (plan §04i Layer 5).
            for record, citation_id in zip(result.records, bundle):
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

    # If we have tool results but the LLM text has no markers, append them.
    if citations and not _CITATION_MARKER_RE.search(text):
        markers = " ".join(c.citation_id for c in citations)
        text = f"{text.rstrip('.')} {markers}."

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
        if not _CITATION_MARKER_RE.search(text):
            text = f"{text.rstrip('.')} [DATA-1]."

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
    # holds only the synthetic 'no-tool-call' placeholder — the answer is
    # ungrounded and must NOT ship at normal confidence, regardless of whether
    # the LLM happened to phrase it as a refusal. Floor confidence hard so the
    # downstream demotion/UI surfaces it as untrusted (the citation-first
    # generator, when restored, is the proper salvage path).
    _real_sources = [s for s in sources_used if s and s != "no-tool-call"]
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
    )


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


def _extract_section_page(result: Any) -> tuple[str | None, int | None]:
    """Return (section_label, page_number) for the top result chunk, or (None, None)."""
    if isinstance(result, DocumentSearchResult) and result.chunks:
        first = result.chunks[0]
        # Build the most descriptive section label available.
        if first.section_number and first.section_title:
            section_label = f"{first.section_number} — {first.section_title}"
        elif first.section_title:
            section_label = first.section_title
        elif first.section_number:
            section_label = first.section_number
        else:
            section_label = None
        return section_label, first.page
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
# model to refuse + correct queries like "above 500% uranium"). When ANY of
# these phrases appear, confidence must be low regardless of tool-call success.
_REFUSAL_PHRASES = (
    "i don't have",
    "i do not have",
    "don't have data",
    "do not have data",
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
    # Impossible-premise refusal shapes from the NUMERIC few-shots:
    "not a possible value",
    "no hole can",
    "no drill hole",
    "not possible",
    "well beyond",
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


def _is_refusal(text: str) -> bool:
    """Detect whether the LLM answer is a refusal rather than a real answer.

    Two detection paths:
      1. Substring match against _REFUSAL_PHRASES — catches "I don't have",
         "insufficient", "no record", and (post-wave-4) "not a possible value".
      2. Starts-with refusal preamble — the system prompt's RULE 10
         (impossible-premise) instructs models to BEGIN refusals with "No"
         or "That's not possible". A leading "No <noun> can be / cannot be"
         is a much more reliable signal of refusal than any single phrase.
    """
    if not text:
        return False
    lower = text.lower().lstrip()
    if any(phrase in lower for phrase in _REFUSAL_PHRASES):
        return True
    # Starts-with refusal preambles (system prompt RULE 10 emits these).
    # Anchored on the FIRST sentence so a body paragraph that happens to
    # contain "no" doesn't trip the heuristic.
    first_sentence = lower.split(".", 1)[0]
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
