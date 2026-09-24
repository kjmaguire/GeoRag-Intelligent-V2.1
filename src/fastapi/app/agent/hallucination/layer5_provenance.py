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

Gate half (restored 2026-09-24)
--------------------------------
``enrich_provenance`` above never rejects a citation — CLAUDE.md hard rule
5 records that as the gap: "provenance is enrichment, not a gate."
:func:`gate_citation_provenance` is the gate. It runs BEFORE enrichment
(and before Layer 2's second pass — see ``validate_node``) and REJECTS a
document-chunk citation outright when its ``source_chunk_id`` does not
resolve to a chunk actually retrieved for THIS query, or carries no
document id. A rejected citation is dropped from the response, not
silently kept: its marker becomes an orphan in the text, which Layer 2's
existing orphan-marker stripping then removes on its second pass — no
new text-editing logic needed. Scoped to ``georag_reports:...``
(document-chunk) citations only, same scope ``enrich_provenance`` already
uses; structured ``silver.*`` citations have no per-row source-file
linkage to check against (see the module docstring above).

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
         ``DocumentSearchResult`` in ``tool_results``). Retrieval is
         already workspace-scoped server-side
         (``app.agent.tools.search_documents`` resolves and filters on
         ``workspace_id`` before querying Qdrant — the GI-9 mandatory
         tenant filter), so membership in this set proves BOTH "this chunk
         was really retrieved for this query" and, transitively, "this
         chunk belongs to the caller's workspace." A citation naming a
         chunk id outside that set could only arise from a bug (stale
         citations reused across a retry/reissue) or an adversarial
         marker the LLM invented that happens to collide with a real
         chunk id from elsewhere — either way, it must not ship.

    Non-document-chunk citations (DATA, PGEO, ``no-tool-call``, the
    zero-row sentinels) are left untouched — "chunk provenance" does not
    apply to them; see the module docstring for why ``enrich_provenance``
    already draws the same line.

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
            continue

        if chunk_id not in retrieved_chunk_ids:
            warnings.append(
                f"Layer 5: citation {citation.citation_id} references chunk "
                f"{chunk_id!r}, which was not retrieved for this query "
                f"(stale or cross-tenant citation) — rejected"
            )
            continue

        kept.append(citation)

    if not warnings:
        return response, []

    if not kept:
        # GeoRAGResponse.citations requires >= 1 entry. Same placeholder
        # shape response_assembler.assemble_response uses when no tool was
        # called — honest about the gap rather than inventing a source.
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
        pass

    return response.model_copy(update={"citations": kept}), warnings

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
