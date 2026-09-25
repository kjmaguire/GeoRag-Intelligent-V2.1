"""Layer 5 — Chunk Provenance.

Architecture reference: Section 04i, Layer 5.

Purpose
-------
Enrich each Citation in a GeoRAGResponse with provenance metadata that traces
the cited data back to a specific source file in MinIO with its sha256 hash.

The provenance chain is:

  Citation.source_chunk_id ("georag_reports:<report_id>:…")
    → silver.reports.source_file_sha256
      → bronze.source_files (file_path, sha256, bucket)

Structured silver.* citations (collars, lithology_logs, samples) have no
per-row source-file linkage yet and are left unenriched — a wrong sha256
is worse than none.

This layer runs AFTER assembly and AFTER Layer 2 validation. It does not
reject or modify the response — it only enriches Citation objects with
additional metadata for audit purposes. If the provenance lookup fails
(e.g. the source file is not yet tracked in bronze.source_files), the
citation is left unchanged and a warning is logged.

The enriched data is added to ``Citation.section`` as a human-readable
provenance string: ``"source: collars/sample_collars.csv (sha256:743495c…)"``.

Usage
-----
    from app.agent.hallucination.layer5_provenance import enrich_provenance
    response = await enrich_provenance(response, pg_pool)

Gate half (restored 2026-09-24; sentence-level removal added 2026-09-24
rag-expert follow-up)
-------------------------------------------------------------------------
``enrich_provenance`` above never rejects a citation — CLAUDE.md hard rule
5 records that as the gap: "provenance is enrichment, not a gate."
:func:`gate_citation_provenance` is the gate. It runs BEFORE enrichment
(and before Layer 2's second pass — see ``validate_node``) and REJECTS a
document-chunk citation outright when its ``source_chunk_id`` does not
resolve to a chunk actually retrieved for THIS query, or carries no
document id.

A rejected citation is dropped from the response, and — per CLAUDE.md
hard rule 4 ("every claim must include a source_chunk_id or be
rejected") — so is the SENTENCE(S) that carried its marker. Stripping
only the bracket text (``[NI43-1]``) and leaving the sentence
("The grade is 1.85 g/t Au.") would ship an uncited claim, which is
exactly what rule 4 forbids; a rejected citation means the claim it
backed is unverified, not that the marker alone was cosmetically wrong.
:func:`_scrub_rejected_sentences` removes each sentence whose ONLY
marker(s) were rejected; a sentence that ALSO carries a surviving valid
marker keeps the sentence and that marker, with just the rejected
marker's bracket text removed. If nothing citeable survives — either
every citation was rejected, or the only sentences left after scrubbing
were empty — the whole response falls through to the same refusal text
Layer 1's hard gate uses (:func:`app.agent.hallucination.layer1_retrieval.build_refusal_text`),
with citations reset to the same inert "nothing to cite" placeholder
``response_assembler.assemble_response`` uses for a no-tool-call answer.
That placeholder is deliberately not referenced by any marker in the
text — safe here specifically because the text is now itself a refusal
making no claims, the same reason ``assemble_response`` gives for why ITS
placeholder carries no marker either.

Scoped to ``georag_reports:...`` (document-chunk) citations only, same
scope ``enrich_provenance`` already uses; structured ``silver.*``
citations have no per-row source-file linkage to check against (see the
module docstring above).

Usage
-----
    from app.agent.hallucination.layer5_provenance import gate_citation_provenance
    response, warnings = gate_citation_provenance(response, tool_results)
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.agent.hallucination.citation_markers import (
    ALL_MARKER_RE,
    CITATION_MARKER_CAPTURE_RE,
    canonical_marker,
)
from app.config import settings
from app.models.rag import Citation, GeoRAGResponse

logger = logging.getLogger(__name__)

# Parses the format app.agent.response_assembler._source_chunk_id_for_doc_chunk
# emits for every document-chunk citation:
#   georag_reports:<report_id>:section=<section|unknown>:chunk=<chunk_id>
# chunk_id is a Qdrant point id (UUID or int) and never contains ":", so a
# greedy tail match is safe.
_DOC_CHUNK_SOURCE_RE = re.compile(
    r"^georag_reports:(?P<report_id>[^:]*):section=(?P<section>[^:]*):chunk=(?P<chunk_id>.+)$"
)

#: report_id placeholders that mean "no real document id" rather than a
#: genuine UUID — see _source_chunk_id_for_doc_chunk / DocumentChunk.report_id.
_NO_REPORT_ID_PLACEHOLDERS: frozenset[str] = frozenset({"", "empty", "unknown", "none"})

# Sentence splitter — same simple regex orchestrator_validators.py's
# completeness guard uses (no nltk/spacy dependency). Duplicated rather
# than imported: it is private to that module and this file has no other
# reason to depend on it.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

#: Cleans up double-spaces left behind after a marker is removed mid-sentence.
_MULTI_SPACE_RE = re.compile(r"  +")


def _is_marker_only_fragment(piece: str) -> bool:
    """True when ``piece`` is nothing but citation marker(s) and whitespace.

    The prompts have the model emit citations as a trailing bracket AFTER
    the sentence's closing period ("The grade is 1.85 g/t Au. [NI43-1]"),
    not before it. ``_SENTENCE_SPLIT_RE`` splits on ``.`` + whitespace, so
    that shape produces TWO fragments — "The grade is 1.85 g/t Au." and
    "[NI43-1]" — and a naive per-fragment marker check would never see the
    marker and the claim in the same unit. ``verify_completeness`` in
    orchestrator_validators.py has the identical problem and solves it by
    also checking "does the NEXT sentence open with a marker"; this is the
    mirror-image fold used by :func:`_fold_trailing_marker_fragments`.
    """
    stripped = piece.strip()
    return bool(stripped) and not ALL_MARKER_RE.sub("", stripped).strip()


def _fold_trailing_marker_fragments(pieces: list[str]) -> list[str]:
    """Fold a marker-only fragment back onto the sentence before it.

    See :func:`_is_marker_only_fragment`. A fragment that is ONLY markers
    (optionally several, e.g. "[NI43-1] [DATA-2]") is not a sentence of
    its own — it is the citation for whatever came before it — so it is
    appended to the previous unit rather than treated as a standalone one.
    A leading marker-only fragment (no previous unit to fold onto, e.g.
    the text opens with a marker) is kept as its own unit unchanged.
    """
    units: list[str] = []
    for piece in pieces:
        if units and _is_marker_only_fragment(piece):
            units[-1] = f"{units[-1]} {piece}"
        else:
            units.append(piece)
    return units


def _scrub_rejected_sentences(
    text: str,
    rejected_citation_ids: set[str],
    valid_citation_ids: set[str],
) -> str:
    """Remove the sentence(s) that carried a REJECTED marker (hard rule 4).

    Per-sentence rule:
      - No rejected marker in the sentence → sentence is untouched
        (regardless of whether it carries other markers or none at all).
      - A rejected marker AND at least one surviving valid marker →
        sentence is KEPT, with only the rejected marker's bracket text
        removed (the valid marker still backs the sentence's claim).
      - A rejected marker and NO surviving valid marker → the whole
        sentence is dropped.

    A trailing marker-only fragment ("Claim. [NI43-1]") is folded back
    onto the sentence it cites before this rule is applied — see
    :func:`_fold_trailing_marker_fragments` — so the claim and its
    (rejected) marker are evaluated and removed together. This is a
    known simplification, not full NLP: a marker that lands at the START
    of the FOLLOWING sentence together with more prose of its own (e.g.
    "Claim. [NI43-1] More text.") is attributed to that following
    sentence, same ambiguity ``verify_completeness`` already accepts.

    Uses ``CITATION_MARKER_CAPTURE_RE`` (numeric DATA/NI43/PUB/PGEO
    markers only, colon or dash form) — the same marker vocabulary
    ``layer2_typed_output``'s own orphan-stripping matches against.

    Returns the reassembled text (sentences rejoined with a single
    space — this does not attempt to preserve original paragraph
    whitespace, same trade-off ``layer2_typed_output.validate_and_repair``
    accepts for its own cleanup regex).
    """
    if not rejected_citation_ids:
        return text

    sentences = _fold_trailing_marker_fragments(_SENTENCE_SPLIT_RE.split(text))
    kept_sentences: list[str] = []

    for sentence in sentences:
        matches = list(CITATION_MARKER_CAPTURE_RE.finditer(sentence))
        if not matches:
            kept_sentences.append(sentence)
            continue

        canonical_ids = [canonical_marker(m.group(1), m.group(3)) for m in matches]
        has_rejected = any(cid in rejected_citation_ids for cid in canonical_ids)
        if not has_rejected:
            kept_sentences.append(sentence)
            continue

        has_valid = any(cid in valid_citation_ids for cid in canonical_ids)
        if not has_valid:
            # Every marker in this sentence is either rejected or unknown,
            # and at least one is rejected -- nothing here is safe to ship.
            continue

        cleaned = sentence
        for match, cid in zip(matches, canonical_ids, strict=True):
            if cid in rejected_citation_ids:
                cleaned = cleaned.replace(match.group(0), "")
        cleaned = _MULTI_SPACE_RE.sub(" ", cleaned).strip()
        if cleaned:
            kept_sentences.append(cleaned)

    return " ".join(s for s in kept_sentences if s.strip()).strip()


def gate_citation_provenance(
    response: GeoRAGResponse,
    tool_results: list[tuple[str, Any]],
) -> tuple[GeoRAGResponse, list[str]]:
    """Layer 5 (gate half): reject citations whose chunk was not retrieved.

    For every Citation whose ``source_chunk_id`` matches the document-chunk
    format (``georag_reports:<report_id>:section=..:chunk=<chunk_id>``),
    checks two things:

      1. ``report_id`` is present and not a known "no document" placeholder
         — a citation with no document id has no provenance to speak of.
      2. ``chunk_id`` is a member of the chunk ids ACTUALLY retrieved for
         this query (every ``DocumentChunk.chunk_id`` across every
         ``DocumentSearchResult`` in ``tool_results`` — note this is
         ``state.tool_results`` for the CURRENT turn only; multi-turn
         history carries no tool results at all, see
         ``app.agent.multi_turn_resolver.ConversationTurn``, so a chunk
         that only appeared in an earlier turn is never in this set).
         Retrieval is already workspace-scoped server-side
         (``app.agent.tools.search_documents`` resolves and filters on
         ``workspace_id`` before querying Qdrant — the GI-9 mandatory
         tenant filter), so membership in this set proves BOTH "this chunk
         was really retrieved for this query" and, transitively, "this
         chunk belongs to the caller's workspace." A citation naming a
         chunk id outside that set could only arise from a bug (stale
         citations reused across a retry/reissue, or carried over from a
         previous conversation turn) or an adversarial marker the LLM
         invented that happens to collide with a real chunk id from
         elsewhere — either way, it must not ship.

    Non-document-chunk citations (DATA, PGEO, ``no-tool-call``, the
    zero-row sentinels) are left untouched — "chunk provenance" does not
    apply to them; see the module docstring for why ``enrich_provenance``
    already draws the same line.

    Rejecting a citation also removes the sentence(s) that cited it (see
    :func:`_scrub_rejected_sentences`) and drops its ``source_chunk_id``
    from ``response.sources_used`` — a rejected chunk is not "used," it's
    refused. If nothing citeable survives, the response becomes a typed
    refusal (see the module docstring's "Gate half" section).

    Returns ``(response, warnings)``. ``warnings`` is empty and ``response``
    is returned UNCHANGED (same object) when nothing was rejected. Never
    raises — pure computation over already-fetched objects, no I/O.
    """
    if not settings.CHUNK_PROVENANCE_GATE_ENABLED:
        return response, []
    if not response.citations:
        return response, []

    from app.agent.tools import DocumentSearchResult  # noqa: PLC0415

    retrieved_chunk_ids: set[str] = {
        str(chunk.chunk_id)
        for _name, result in tool_results
        if isinstance(result, DocumentSearchResult)
        for chunk in result.chunks
    }

    kept: list[Citation] = []
    warnings: list[str] = []
    rejected_citation_ids: set[str] = set()
    rejected_source_chunk_ids: set[str] = set()

    for citation in response.citations:
        match = _DOC_CHUNK_SOURCE_RE.match(citation.source_chunk_id or "")
        if match is None:
            kept.append(citation)
            continue

        report_id = match.group("report_id")
        chunk_id = match.group("chunk_id")

        if report_id.lower() in _NO_REPORT_ID_PLACEHOLDERS:
            warnings.append(
                f"Layer 5: citation {citation.citation_id} carries no "
                f"document id (source_chunk_id={citation.source_chunk_id!r}) "
                f"— rejected"
            )
            rejected_citation_ids.add(citation.citation_id)
            rejected_source_chunk_ids.add(citation.source_chunk_id)
            continue

        if chunk_id not in retrieved_chunk_ids:
            warnings.append(
                f"Layer 5: citation {citation.citation_id} references chunk "
                f"{chunk_id!r}, which was not retrieved for this query "
                f"(stale or cross-tenant citation) — rejected"
            )
            rejected_citation_ids.add(citation.citation_id)
            rejected_source_chunk_ids.add(citation.source_chunk_id)
            continue

        kept.append(citation)

    if not warnings:
        return response, []

    valid_citation_ids = {c.citation_id for c in kept}
    scrubbed_text = _scrub_rejected_sentences(
        response.text, rejected_citation_ids, valid_citation_ids
    )
    sources_used = [
        s for s in response.sources_used if s not in rejected_source_chunk_ids
    ]

    # Hard rule 4: nothing citeable survives -- either every citation was
    # rejected, or the sentence(s) that carried a rejected marker were the
    # only substantive content. Shipping whatever prose is left, backed by
    # zero real citations, would itself be an uncited claim. Fall through
    # to the same refusal text Layer 1's hard gate uses.
    if not kept or not scrubbed_text.strip():
        from app.agent.hallucination.layer1_retrieval import (  # noqa: PLC0415
            build_refusal_text,
        )

        scrubbed_text = build_refusal_text()
        kept = []
        sources_used = []

    if not kept:
        # GeoRAGResponse.citations requires >= 1 entry. Same "nothing to
        # cite" placeholder response_assembler.assemble_response uses for
        # a no-tool-call answer -- deliberately NOT referenced by any
        # marker in the text, which is safe here specifically because the
        # text above is now itself a refusal (no claims to back).
        kept.append(
            Citation(
                citation_id="[DATA-1]",
                citation_type="DATA",
                source_chunk_id="provenance-rejected",
                document_title="No citation passed the provenance gate",
                section=None,
                page=None,
                relevance_score=0.0,
            )
        )
        sources_used = [*sources_used, "provenance-rejected"]

    logger.warning(
        "layer5_provenance: rejected %d/%d citation(s) on the provenance "
        "gate: %s",
        len(warnings),
        len(response.citations),
        warnings,
    )
    try:
        from app.metrics import CHUNK_PROVENANCE_REJECTED_TOTAL  # noqa: PLC0415

        CHUNK_PROVENANCE_REJECTED_TOTAL.inc(len(warnings))
    except Exception:  # noqa: BLE001 — metrics must never break the gate
        logger.debug("CHUNK_PROVENANCE_REJECTED_TOTAL increment failed", exc_info=True)

    return (
        response.model_copy(
            update={
                "citations": kept,
                "text": scrubbed_text,
                "sources_used": sources_used,
            }
        ),
        warnings,
    )

# Parse the source_chunk_id to determine which silver table is cited.
# Patterns:
#   silver.collars:count=20:first=8ab89d36-…
#   silver.lithology_logs:hole=PLS-20-01:collar=…:intervals=4
#   silver.samples:element=U3O8_ppm:count=25
#   georag_reports:44a67709-…:section=13:chunk=…
_SOURCE_TABLE_RE = re.compile(
    r"^(silver\.collars|silver\.lithology_logs|silver\.samples|georag_reports)"
)

# Only georag_reports citations resolve to a SPECIFIC source file today:
# the source_chunk_id carries the report_id, and silver.reports records
# the ingested file's sha256 (source_file_sha256 — written by ingest_pdf's
# INSERT_REPORT_SQL), which joins to bronze.source_files. The silver.*
# structured kinds have no per-row file linkage yet; the old LIKE-on-
# file_path lookups just attached the most-recently-ingested file's sha256
# to EVERY citation — provenance fabrication. Skip, don't guess.
_REPORT_SOURCE_SQL = (
    "SELECT bf.file_path, bf.sha256, bf.file_size "
    "FROM silver.reports r "
    "JOIN bronze.source_files bf ON bf.sha256 = r.source_file_sha256 "
    "WHERE r.report_id = $1 "
    "ORDER BY bf.ingested_at DESC LIMIT 1"
)


async def enrich_provenance(
    response: GeoRAGResponse,
    pg_pool: Any,
) -> GeoRAGResponse:
    """Enrich citations with source file provenance (Layer 5).

    For each citation, resolves the bronze.source_files record and appends
    the file path + sha256 prefix to the citation's section field.

    Args:
        response: The assembled GeoRAGResponse.
        pg_pool: asyncpg connection pool.

    Returns:
        The response with enriched provenance metadata. Never raises.
    """
    if not response.citations:
        return response

    enriched_count = 0
    unresolved_count = 0
    # Eval 08 P3 — validate sha256 shape before trusting it as provenance.
    # A malformed sha (truncated, all-zeros, non-hex) means the bronze
    # ingest path is corrupted upstream; the row is no longer a valid
    # anchor for a citation's "where did this evidence come from?" claim.
    import re as _re_local
    _SHA256_OK = _re_local.compile(r"^[0-9a-f]{64}$")
    _ZERO_SHA = "0" * 64

    for citation in response.citations:
        source_id = citation.source_chunk_id
        if not source_id:
            unresolved_count += 1
            continue

        # Determine which silver table this citation references.
        match = _SOURCE_TABLE_RE.match(source_id)
        if not match:
            unresolved_count += 1
            continue

        table_key = match.group(1)
        if table_key != "georag_reports":
            # silver.* structured kinds: no reliable citation → file join
            # exists yet — returning NO provenance beats attaching a
            # random file's sha256 to the citation.
            unresolved_count += 1
            continue

        # georag_reports:<report_id>:section=..:chunk=.. — resolve the
        # provenance of THIS report, not whichever PDF was ingested last.
        parts = source_id.split(":", 2)
        report_id = parts[1] if len(parts) > 1 else ""
        if not report_id or report_id == "empty":
            unresolved_count += 1
            continue

        try:
            async with pg_pool.acquire() as conn:
                row = await asyncio.wait_for(
                    conn.fetchrow(_REPORT_SOURCE_SQL, report_id),
                    timeout=settings.TIMEOUT_POSTGIS_S,
                )
        except Exception:
            logger.debug(
                "layer5_provenance: failed to resolve source for %s",
                source_id,
            )
            unresolved_count += 1
            continue

        if row is None:
            unresolved_count += 1
            continue

        file_path = row["file_path"]
        sha256 = (row["sha256"] or "").lower().strip()
        # Provenance hardening: reject malformed/zero-SHA rows. A zero
        # hash means the ingester computed but never recorded the real
        # digest; a non-hex value means a downstream consumer corrupted
        # the column. Either way we can't legitimately claim "this is
        # the file the evidence came from," so degrade gracefully.
        if not _SHA256_OK.match(sha256) or sha256 == _ZERO_SHA:
            logger.warning(
                "layer5_provenance: malformed sha256 for %s (file=%s) — "
                "dropping provenance claim",
                source_id,
                file_path,
            )
            unresolved_count += 1
            continue
        sha_short = sha256[:12]

        # Enrich the section field with provenance info.
        provenance_str = f"source: {file_path} (sha256:{sha_short}…)"
        if citation.section:
            citation.section = f"{citation.section} | {provenance_str}"
        else:
            citation.section = provenance_str

        enriched_count += 1

    if enriched_count > 0:
        logger.info(
            "layer5_provenance: enriched %d/%d citations with source file provenance "
            "(unresolved=%d)",
            enriched_count,
            len(response.citations),
            unresolved_count,
        )
    else:
        logger.debug(
            "layer5_provenance: no citations could be enriched "
            "(no matching source files, unresolved=%d)",
            unresolved_count,
        )

    return response
