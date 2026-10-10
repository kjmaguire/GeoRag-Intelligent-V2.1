"""Layer 5 — Chunk Provenance.

Architecture reference: Section 04i, Layer 5.

Purpose
-------
Enrich each Citation in a GeoRAGResponse with provenance metadata that traces
the cited data back to a specific source file in object storage with its
sha256 hash.

The provenance chain is:

  Citation.source_chunk_id ("georag_reports:<report_id>:…")
    → silver.reports (source_object_key, source_file_sha256)

(It used to continue into bronze.source_files, which PDFs never reach and
whose column names the query had wrong — see ``_REPORT_SOURCE_SQL``.)

Structured silver.* citations (collars, lithology_logs, samples) have no
per-row source-file linkage yet and are left unenriched — a wrong sha256
is worse than none.

This layer runs AFTER assembly and AFTER Layer 2 validation. It does not
reject or modify the response — it only enriches Citation objects with
additional metadata for audit purposes. If the provenance lookup fails
(e.g. the report predates source_file_sha256), the citation is left
unchanged; a failed query is logged at WARNING.

The enriched data is added to ``Citation.provenance`` (not rendered) as a
human-readable provenance string:
``"source: collars/sample_collars.csv (sha256:743495c…)"``. It used to be
appended to ``Citation.section``, which the chat displays.

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
resolve to a chunk actually retrieved for THIS query, names a document
other than that chunk's own, or carries no document id although the chunk
has one. A retrieved chunk that genuinely has no document — an ADR-0012
structured summary — is gated on membership alone (2026-09-29, RAG-8).

A rejected citation is dropped from the response, and — per CLAUDE.md
hard rule 4 ("every claim must include a source_chunk_id or be
rejected") — so is the SENTENCE(S) that carried its marker. Stripping
only the bracket text (``[NI43-1]``) and leaving the sentence
("The grade is 1.85 g/t Au.") would ship an uncited claim, which is
exactly what rule 4 forbids; a rejected citation means the claim it
backed is unverified, not that the marker alone was cosmetically wrong.
:func:`scrub_rejected_markers` removes each sentence whose ONLY
marker(s) were rejected; a sentence that ALSO carries a surviving valid
marker keeps the sentence and that marker, with just the rejected
marker's bracket text removed. If nothing citeable survives — either
every citation was rejected, or the only sentences left after scrubbing
were empty — the whole response falls through to the same refusal text
its own plain text (:data:`app.agent.hallucination.refusals.PROVENANCE_REFUSAL_TEXT`,
stamped with an ``unsupported_by_sources`` ``refusal_payload``), with citations reset to the same inert "nothing to cite" placeholder
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
import uuid
from typing import Any

from app.agent.hallucination.citation_markers import (
    CITATION_MARKER_CAPTURE_RE,
    EV_MARKER_CAPTURE_RE,
    canonical_ev_marker,
    canonical_marker,
    ungroup_response_markers,
)
from app.agent.hallucination.claim_sentences import (
    Unit,
    drop_units,
)
from app.agent.hallucination.refusals import (
    PROVENANCE_REFUSAL_TEXT,
    UNSUPPORTED_BY_SOURCES_MESSAGE,
    make_refusal_payload,
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
#: "none" is what an f-string makes of a Python None: the ADR-0012 structured
#: summaries (nl_summaries.py) are written with document_id NULL by design,
#: so their payload's report_id is None and their source_chunk_id reads
#: "georag_reports:None:..." (audit 2026-09-29, RAG-8).
_NO_REPORT_ID_PLACEHOLDERS: frozenset[str] = frozenset(
    ("", "empty", "unknown", "none", "null")
)


def _is_placeholder_report_id(report_id: Any) -> bool:
    return report_id is None or str(report_id).strip().lower() in _NO_REPORT_ID_PLACEHOLDERS


#: Cleans up double-spaces left behind after a marker is removed mid-sentence.
_MULTI_SPACE_RE = re.compile(r"  +")
#: ... and a space left in front of the punctuation that followed it.
_SPACE_BEFORE_PUNCTUATION_RE = re.compile(r"[ \t]+([.,;:!?])")


def scrub_rejected_markers(
    text: str,
    rejected_citation_ids: set[str],
    valid_citation_ids: set[str],
    *,
    proactive_insights_offset: int | None = None,
) -> tuple[str, int | None, int]:
    """Remove the sentence(s) that carried a REJECTED marker (hard rule 4).

    Per-sentence rule:
      - No rejected marker in the sentence → sentence is untouched
        (regardless of whether it carries other markers or none at all).
      - A rejected marker AND at least one surviving valid marker →
        sentence is KEPT, with only the rejected marker's bracket text
        removed (the valid marker still backs the sentence's claim).
      - A rejected marker and NO surviving valid marker → the whole
        sentence is dropped.

    Sentences come from :func:`app.agent.hallucination.claim_sentences.split_units`,
    which folds a trailing marker-only fragment ("Claim. [NI43-1]") back
    onto the sentence it cites, does not break on "approx." / "Fig." /
    "e.g." (RISK-4), and keeps every separator — so the surviving answer
    keeps its paragraphs, bullets, tables and headings instead of being
    re-joined onto one line. Known simplification, unchanged: a marker
    at the START of a following sentence that has prose of its own
    ("Claim. [NI43-1] More text.") is attributed to that following
    sentence.

    Shared by Layer 5 (rejected provenance) and Layer 2 (invented markers
    with no Citation behind them). Only the text before
    ``proactive_insights_offset`` is touched; the returned offset is moved
    to match.

    Returns ``(text, proactive_insights_offset, dropped_sentence_count)``.
    """
    if not rejected_citation_ids:
        return text, proactive_insights_offset, 0

    # An evidence-id marker ("[ev:abc123]") is a rejected id when the caller
    # names it so ([ev:abc123] in ``rejected_citation_ids``); otherwise it is
    # a citation like any other only while the span resolver is on.
    ev_cites = bool(getattr(settings, "CITATION_SPAN_RESOLVER_ENABLED", False))

    def _ids(piece: str) -> list[str]:
        return [
            canonical_marker(m.group(1), m.group(3))
            for m in CITATION_MARKER_CAPTURE_RE.finditer(piece)
        ] + [
            canonical_ev_marker(m.group(1)) for m in EV_MARKER_CAPTURE_RE.finditer(piece)
        ]

    def _valid(cid: str) -> bool:
        if cid.startswith("[ev:"):
            return ev_cites and cid not in rejected_citation_ids
        return cid in valid_citation_ids

    def _drop(i: int, units: list[Unit]) -> bool:
        ids = _ids(units[i].text)
        if not any(cid in rejected_citation_ids for cid in ids):
            return False
        return not any(_valid(cid) for cid in ids)

    def _rewrite(i: int, units: list[Unit]) -> str:
        sentence = units[i].text
        for m in list(CITATION_MARKER_CAPTURE_RE.finditer(sentence)):
            if canonical_marker(m.group(1), m.group(3)) in rejected_citation_ids:
                sentence = sentence.replace(m.group(0), "")
        for m in list(EV_MARKER_CAPTURE_RE.finditer(sentence)):
            if canonical_ev_marker(m.group(1)) in rejected_citation_ids:
                sentence = sentence.replace(m.group(0), "")
        if sentence == units[i].text:
            return sentence
        # "Claim [NI43-1] [NI43-9]." loses the second marker's brackets but
        # not the space in front of it: close that gap before the full stop.
        sentence = _SPACE_BEFORE_PUNCTUATION_RE.sub(r"\1", sentence)
        return _MULTI_SPACE_RE.sub(" ", sentence).strip()

    return drop_units(
        text,
        _drop,
        rewrite=_rewrite,
        proactive_insights_offset=proactive_insights_offset,
    )


def _retrieved_chunk_reports(tool_results: list[tuple[str, Any]]) -> dict[str, Any]:
    """chunk_id → report_id for every chunk retrieved for THIS query."""
    from app.agent.tools import DocumentSearchResult  # noqa: PLC0415

    out: dict[str, Any] = dict()
    for _name, result in tool_results:
        if isinstance(result, DocumentSearchResult):
            for chunk in result.chunks:
                out[str(chunk.chunk_id)] = getattr(chunk, "report_id", None)
    return out


def gate_citation_provenance(
    response: GeoRAGResponse,
    tool_results: list[tuple[str, Any]],
    *,
    rendered_citation_ids: set[str] | frozenset[str] | None = None,
) -> tuple[GeoRAGResponse, list[str]]:
    """Layer 5 (gate half): reject citations whose chunk was not retrieved.

    For every Citation whose ``source_chunk_id`` matches the document-chunk
    format (``georag_reports:<report_id>:section=..:chunk=<chunk_id>``):

      1. ``chunk_id`` must be a member of the chunk ids ACTUALLY retrieved
         for this query (every ``DocumentChunk.chunk_id`` across every
         ``DocumentSearchResult`` in ``tool_results`` — ``state.tool_results``
         for the CURRENT turn only; multi-turn history carries no tool
         results, so a chunk that only appeared in an earlier turn is never
         in this set). Retrieval is workspace-scoped server-side (GI-9), so
         membership proves both "retrieved for this query" and, transitively,
         "belongs to the caller's workspace". A chunk id outside the set can
         only come from a bug (stale citations reused across a retry, carried
         over from a previous turn) or an invented marker colliding with a
         real id — either way it must not ship.
      2. The citation's ``report_id`` must be the document the retrieved chunk
         actually belongs to. A mismatch, or a placeholder where the chunk
         has a real document, is rejected ("carries no document id").
      3. An ORPHAN passage — a retrieved chunk that genuinely has no document,
         the ADR-0012 structured summaries built from silver rows
         (nl_summaries.py, ``document_id`` NULL by design) — is gated on
         membership alone. Its citation reads ``georag_reports:None:...``
         because there is no document to name, and rejecting it for that
         deleted every citation of exactly the passages built to answer
         "which holes returned >1% U3O8" (audit 2026-09-29, RAG-8). Real
         document chunks are NOT relaxed: they still need a matching id.
      4. ``rendered_citation_ids`` (optional): when the caller knows which
         citation ids were actually rendered into the model's context, a
         citation that was retrieved but never rendered is rejected too —
         the model never read it (RAG-20). NOT WIRED on the live path yet:
         ``validate_node`` does not pass it, because the context renderer
         does not record what it rendered. Until it does, check 1 cannot
         catch a citation to a retrieved-but-truncated chunk.

    Non-document-chunk citations (DATA, PGEO, ``no-tool-call``, the
    zero-row sentinels) are left untouched — "chunk provenance" does not
    apply to them; ``enrich_provenance`` draws the same line.

    Rejecting a citation also removes the sentence(s) that cited it (see
    :func:`scrub_rejected_markers`) and drops its ``source_chunk_id`` from
    ``response.sources_used``. If nothing citeable survives, the response
    becomes a typed refusal (see the module docstring's "Gate half").

    Returns ``(response, warnings)``. ``warnings`` is empty and ``response``
    is returned UNCHANGED (same object) when nothing was rejected -- bar the
    one rewrite of grouped markers into single ones ("[NI43-1, NI43-2]"),
    which comes back as a copy. Never raises on well-formed input — pure
    computation, no I/O.
    """
    if not settings.CHUNK_PROVENANCE_GATE_ENABLED:
        return response, []
    if not response.citations:
        return response, []

    # One marker per bracket: "[NI43-1, NI43-2]" cites two chunks, and a
    # rejected one must be removable from the sentence on its own.
    response = ungroup_response_markers(response)

    retrieved = _retrieved_chunk_reports(tool_results)

    kept: list[Citation] = []
    warnings: list[str] = []
    rejected_citation_ids: set[str] = set()
    rejected_source_chunk_ids: set[str] = set()

    def _reject(citation: Citation, why: str) -> None:
        warnings.append(f"Layer 5: citation {citation.citation_id} {why} — rejected")
        rejected_citation_ids.add(citation.citation_id)
        rejected_source_chunk_ids.add(citation.source_chunk_id)

    for citation in response.citations:
        match = _DOC_CHUNK_SOURCE_RE.match(citation.source_chunk_id or "")
        if match is None:
            kept.append(citation)
            continue

        report_id = match.group("report_id")
        chunk_id = match.group("chunk_id")

        if chunk_id not in retrieved:
            if _is_placeholder_report_id(report_id):
                _reject(
                    citation,
                    f"carries no document id (source_chunk_id="
                    f"{citation.source_chunk_id!r}) and its chunk was not "
                    f"retrieved for this query",
                )
            else:
                _reject(
                    citation,
                    f"references chunk {chunk_id!r}, which was not retrieved "
                    f"for this query (stale or cross-tenant citation)",
                )
            continue

        retrieved_report = retrieved[chunk_id]
        if _is_placeholder_report_id(report_id):
            if not _is_placeholder_report_id(retrieved_report):
                _reject(
                    citation,
                    f"carries no document id (source_chunk_id="
                    f"{citation.source_chunk_id!r}) although chunk "
                    f"{chunk_id!r} belongs to document {retrieved_report!r}",
                )
                continue
            # Orphan passage (ADR-0012 summary): membership is the gate.
        elif _is_placeholder_report_id(retrieved_report) or str(retrieved_report) != report_id:
            _reject(
                citation,
                f"names document {report_id!r} but chunk {chunk_id!r} was "
                f"retrieved from document {retrieved_report!r}",
            )
            continue

        if (
            rendered_citation_ids is not None
            and citation.citation_id not in rendered_citation_ids
        ):
            _reject(
                citation,
                f"references chunk {chunk_id!r}, which was retrieved but never "
                f"rendered into the model's context",
            )
            continue

        kept.append(citation)

    if not warnings:
        return response, []

    valid_citation_ids = set(c.citation_id for c in kept)
    scrubbed_text, insights_offset, _dropped = scrub_rejected_markers(
        response.text,
        rejected_citation_ids,
        valid_citation_ids,
        proactive_insights_offset=response.proactive_insights_offset,
    )
    sources_used = [
        s for s in response.sources_used if s not in rejected_source_chunk_ids
    ]

    # Hard rule 4: nothing citeable survives -- either every citation was
    # rejected, or the sentence(s) that carried a rejected marker were the
    # only substantive content. Shipping whatever prose is left, backed by
    # zero real citations, would itself be an uncited claim. Fall through
    # to a provenance-specific refusal.
    refusal_payload: dict[str, Any] | None = None
    if not kept or not scrubbed_text.strip():
        # Its own text, NOT Layer 1's build_refusal_text(): that one says
        # "Document search found no passages that cleared the relevance
        # threshold", which is false here -- passages were found and the
        # draft cited ones that were not among them (audit item 8).
        scrubbed_text = PROVENANCE_REFUSAL_TEXT
        insights_offset = None
        kept = []
        sources_used = []
        refusal_payload = make_refusal_payload(
            "unsupported_by_sources", UNSUPPORTED_BY_SOURCES_MESSAGE
        )

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
                document_title="No supporting source",
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

    update: dict[str, Any] = dict(
        citations=kept,
        text=scrubbed_text,
        sources_used=sources_used,
        proactive_insights_offset=insights_offset,
    )
    if refusal_payload is not None:
        update["refusal_payload"] = refusal_payload
    return response.model_copy(update=update), warnings


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
# the source_chunk_id carries the report_id, and silver.reports itself
# records where the PDF came from — ``source_object_key`` (the bronze object
# the report was parsed from, 2026_08_19_030000) and ``source_file_sha256``
# (2026_04_13_000000), both written by ingest_pdf's INSERT_REPORT_SQL.
#
# This used to JOIN bronze.source_files on bf.sha256 and select
# bf.file_path / bf.sha256 / bf.file_size. None of those columns exist
# (bronze.source_files has file_sha256, original_filename, seaweedfs_key,
# file_size_bytes), so every lookup raised UndefinedColumn, the handler
# below swallowed it at DEBUG, and enrichment never enriched a single
# citation. Fixing the column names alone would not have helped:
# bronze.source_files is written by the drill-upload path only, never by
# ingest_pdf, so no PDF would ever join (audit 2026-09-29, PG-5).
#
# The silver.* structured kinds have no per-row file linkage yet; the old
# LIKE-on-file_path lookups just attached the most-recently-ingested file's
# sha256 to EVERY citation — provenance fabrication. Skip, don't guess.
#
# One statement for every distinct report the answer cites (audit item 16):
# it used to be one round-trip per CITATION, so twelve chunks of one report
# cost twelve identical queries on the path to the `completed` frame.
_REPORT_SOURCE_SQL = (
    "SELECT report_id::text AS report_id, source_object_key, source_file_sha256 "
    "FROM silver.reports "
    "WHERE report_id = ANY($1::uuid[])"
)


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$"
)


def _as_uuid(value: str) -> str | None:
    """``value`` as a canonical UUID string, or None when it is not one
    (a placeholder such as "None" / "empty" has no report row)."""
    if not _UUID_RE.match(value or ""):
        return None
    return str(uuid.UUID(value))


async def enrich_provenance(
    response: GeoRAGResponse,
    pg_pool: Any,
) -> GeoRAGResponse:
    """Enrich citations with source file provenance (Layer 5).

    For each citation, resolves the silver.reports source record and appends
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

    # Pass 1: which citations name a looked-up report, and which reports.
    pending: list[tuple[Citation, str, str]] = []  # (citation, source_id, report uuid)
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
        report_uuid = _as_uuid(parts[1]) if len(parts) > 1 else None
        if report_uuid is None:
            # Placeholder ("None" for an ADR-0012 summary, "empty") or not a
            # UUID at all: there is no report row to look up.
            unresolved_count += 1
            continue
        pending.append((citation, source_id, report_uuid))

    # Pass 2: ONE query for the distinct report ids.
    rows_by_report: dict[str, Any] = {}
    if pending:
        distinct_ids = list(dict.fromkeys(rid for _c, _s, rid in pending))
        try:
            # The bound covers ``pool.acquire()`` as well as the fetch (AL-10):
            # an exhausted pool made the acquire wait with no timeout, and the
            # answer waited with it. A TimeoutError takes the soft path below
            # -- the citations ship without their provenance line.
            async with asyncio.timeout(settings.TIMEOUT_POSTGIS_S), pg_pool.acquire() as conn:
                fetched = await conn.fetch(_REPORT_SOURCE_SQL, distinct_ids)
            rows_by_report = {str(r["report_id"]).lower(): r for r in fetched}
        except Exception:
            # WARNING, not DEBUG: a query that fails on every call is how
            # this enrichment went dead unnoticed (PG-5).
            logger.warning(
                "layer5_provenance: failed to resolve sources for %d report(s)",
                len(distinct_ids),
                exc_info=True,
            )

    for citation, source_id, report_uuid in pending:
        row = rows_by_report.get(report_uuid.lower())
        if row is None:
            unresolved_count += 1
            continue

        file_path = row["source_object_key"] or "unknown object"
        sha256 = (row["source_file_sha256"] or "").lower().strip()
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

        # Technical detail goes in its own field, NOT in ``section``: section
        # renders on the citation chip, and an S3 object key plus a hash is
        # not a section heading (audit item 22).
        citation.provenance = f"source: {file_path} (sha256:{sha_short}…)"

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
