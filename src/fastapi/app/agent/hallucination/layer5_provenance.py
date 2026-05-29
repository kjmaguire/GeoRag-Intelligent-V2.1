"""Layer 5 — Chunk Provenance.

Architecture reference: Section 04i, Layer 5.

Purpose
-------
Enrich each Citation in a GeoRAGResponse with provenance metadata that traces
the cited data back to a specific source file in MinIO with its sha256 hash.

The provenance chain is:

  Citation.source_chunk_id
    → silver table (collars, lithology_logs, reports, samples)
      → bronze.source_files (file_path, sha256, bucket)

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
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.config import settings
from app.models.rag import GeoRAGResponse

logger = logging.getLogger(__name__)

# Parse the source_chunk_id to determine which silver table is cited.
# Patterns:
#   silver.collars:count=20:first=8ab89d36-…
#   silver.lithology_logs:hole=PLS-20-01:collar=…:intervals=4
#   silver.samples:element=U3O8_ppm:count=25
#   georag_reports:44a67709-…:section=13:chunk=…
_SOURCE_TABLE_RE = re.compile(
    r"^(silver\.collars|silver\.lithology_logs|silver\.samples|georag_reports)"
)

# Map silver tables to their likely bronze source file patterns.
# Each entry is a SQL query that resolves the source file(s) for a given
# citation context. We use LIKE patterns on file_path because the exact
# mapping depends on the ingestion pipeline.
_TABLE_TO_SOURCE_SQL: dict[str, str] = {
    "silver.collars": (
        "SELECT file_path, sha256, file_size FROM bronze.source_files "
        "WHERE file_path LIKE 'collars/%' OR file_path LIKE 'excel/%' "
        "ORDER BY ingested_at DESC LIMIT 1"
    ),
    "silver.lithology_logs": (
        "SELECT file_path, sha256, file_size FROM bronze.source_files "
        "WHERE file_path LIKE 'lithology/%' "
        "ORDER BY ingested_at DESC LIMIT 1"
    ),
    "silver.samples": (
        "SELECT file_path, sha256, file_size FROM bronze.source_files "
        "WHERE file_path LIKE 'samples/%' "
        "ORDER BY ingested_at DESC LIMIT 1"
    ),
    "georag_reports": (
        "SELECT file_path, sha256, file_size FROM bronze.source_files "
        "WHERE file_path LIKE 'reports/%' AND mime_type = 'application/pdf' "
        "ORDER BY ingested_at DESC LIMIT 1"
    ),
}


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
        sql = _TABLE_TO_SOURCE_SQL.get(table_key)
        if not sql:
            unresolved_count += 1
            continue

        try:
            async with pg_pool.acquire() as conn:
                row = await asyncio.wait_for(
                    conn.fetchrow(sql),
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
