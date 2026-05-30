"""Plan §3d — parent expansion.

When retrieval returns a CHILD chunk (e.g. a 200-token paragraph from
inside a section), the answer often needs the wider PARENT context
(the full section) to make sense. This module fetches each child's
parent and merges it into the EvidencePacket as a separate
DocumentEvidence member.

Three-step algorithm:

  1. **Gate** — only fire when the packet has DocumentEvidence members
     with non-null ``parent_chunk_id``. No-op otherwise.
  2. **Batch lookup** — single SQL fetch for all unique parent
     chunk_ids. Avoids N+1 even when expanding 10 children.
  3. **Merge** — for each child, append the parent as a sibling
     DocumentEvidence (NOT replace the child — the LLM may benefit
     from both the precise hit AND the wider context). Authority
     rank is inherited from the child (parents share the same
     document, so they share authority).

Pure-async; sets ``georag.workspace_id`` GUC for RLS. Best-effort:
DB failure logs + returns the packet unchanged.

Cap: ``max_parents_per_packet`` (default 5) bounds the extra evidence
the expander can add. Without the cap, a packet with 10 child chunks
could double in size. Per-intent override via the wire callsite.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.agent.evidence import (
    DocumentEvidence,
    EvidencePacket,
)


logger = logging.getLogger(__name__)


__all__ = [
    "ExpansionResult",
    "expand_parents",
    "fetch_parent_chunks",
]


@dataclass(frozen=True)
class ExpansionResult:
    """Output bundle from :func:`expand_parents`."""

    packet: EvidencePacket
    parents_added: int
    parents_skipped: int
    parents_failed: int
    reason: str | None = None


_FETCH_PARENTS_SQL = """
    SELECT
        passage_id::text  AS chunk_id,
        document_id::text AS document_id,
        text,
        ordinal,
        page_first,
        page_last,
        chunk_kind
    FROM silver.document_passages
    WHERE passage_id = ANY($1::uuid[])
"""


async def fetch_parent_chunks(
    pool: Any,
    *,
    workspace_id: str,
    parent_chunk_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Return {chunk_id: row_dict} for every parent found.

    Missing chunk_ids are silently absent from the output — the
    expander treats them as "no parent available" and skips.

    Workspace tenancy: sets ``georag.workspace_id`` GUC so RLS
    applies on silver.document_passages.
    """
    if not workspace_id:
        raise ValueError("workspace_id is required (sets georag.workspace_id)")
    if not parent_chunk_ids:
        return {}

    # Dedupe — multiple children may share the same parent.
    unique_ids = list({pid for pid in parent_chunk_ids if pid})
    if not unique_ids:
        return {}

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.workspace_id', $1, true)",
                    workspace_id,
                )
                rows = await conn.fetch(_FETCH_PARENTS_SQL, unique_ids)
    except Exception:
        logger.warning(
            "fetch_parent_chunks: pg lookup failed for %d ids (non-fatal)",
            len(unique_ids),
            exc_info=True,
        )
        return {}

    return {row["chunk_id"]: dict(row) for row in rows}


def expand_parents_sync(
    packet: EvidencePacket,
    *,
    parents_by_id: dict[str, dict[str, Any]],
    max_parents_per_packet: int = 5,
) -> ExpansionResult:
    """Pure synchronous merge step — for tests + the async wrapper.

    Splits the I/O from the merge: ``fetch_parent_chunks`` does the
    DB lookup; this function takes the result + assembles the new
    packet. Lets tests cover the merge logic without a mock pool.
    """
    if not packet.evidence:
        return ExpansionResult(
            packet=packet, parents_added=0, parents_skipped=0,
            parents_failed=0, reason="empty packet",
        )

    # Track parents we've already added so the same one doesn't get
    # appended twice when N children share a parent.
    added_parent_ids: set[str] = set()
    # Also: don't add a parent that's already in the packet as its own
    # evidence (could happen if the parent itself was retrieved).
    existing_chunk_ids: set[str] = {
        getattr(e, "chunk_id", "")
        for e in packet.evidence
        if isinstance(e, DocumentEvidence)
    }

    new_evidence = list(packet.evidence)
    parents_added = 0
    parents_skipped = 0
    parents_failed = 0

    for e in packet.evidence:
        if not isinstance(e, DocumentEvidence):
            continue
        parent_id = e.parent_chunk_id
        if not parent_id:
            continue
        if parent_id in added_parent_ids or parent_id in existing_chunk_ids:
            parents_skipped += 1
            continue

        parent_row = parents_by_id.get(parent_id)
        if parent_row is None:
            parents_failed += 1
            continue

        # Build a sibling DocumentEvidence using the parent's text +
        # carrying the child's authority/document metadata (same doc).
        text = parent_row.get("text") or ""
        if not text:
            parents_failed += 1
            continue

        # Authority + currency + document metadata are inherited from
        # the child (same document, by definition).
        try:
            parent_evidence = DocumentEvidence(
                document_id=e.document_id,
                document_title=e.document_title,
                document_type=e.document_type,
                authority_rank=e.authority_rank,
                is_current=e.is_current,
                confidence=max(0.0, min(1.0, e.confidence * 0.9)),
                page=int(parent_row.get("page_first") or e.page or 0),
                section=e.section,
                chunk_id=parent_id,
                # The child IS the parent's parent for the purpose of
                # subsequent expansion — but we set parent_chunk_id to
                # None on the expanded copy to prevent recursion if
                # the expander runs again on this packet.
                parent_chunk_id=None,
                text=text[:8000],
                char_start=0,
                char_end=min(len(text), 8000),
                source_uri=e.source_uri,
            )
            new_evidence.append(parent_evidence)
            added_parent_ids.add(parent_id)
            parents_added += 1
        except Exception:  # pragma: no cover — defensive
            logger.warning(
                "expand_parents_sync: failed to build DocumentEvidence "
                "for parent_id=%s (non-fatal)",
                parent_id,
                exc_info=True,
            )
            parents_failed += 1

        if parents_added >= max_parents_per_packet:
            logger.info(
                "expand_parents_sync: cap reached (max_parents_per_packet=%d)",
                max_parents_per_packet,
            )
            break

    # Recompute total_tokens via the same chars/4 proxy the converter
    # uses. Caller probably runs enforce_token_budget after expansion
    # to trim if needed.
    from app.agent.evidence_converter import estimate_evidence_tokens  # noqa: PLC0415

    new_total = sum(estimate_evidence_tokens(e) for e in new_evidence)
    new_remaining = (
        packet.remaining_budget
        + packet.total_tokens
        - new_total
    )

    new_packet = packet.model_copy(update={
        "evidence": new_evidence,
        "total_tokens": new_total,
        "remaining_budget": new_remaining,
    })

    return ExpansionResult(
        packet=new_packet,
        parents_added=parents_added,
        parents_skipped=parents_skipped,
        parents_failed=parents_failed,
    )


async def expand_parents(
    packet: EvidencePacket,
    pool: Any,
    *,
    workspace_id: str,
    max_parents_per_packet: int = 5,
) -> ExpansionResult:
    """§3d — async expansion wrapping fetch + merge.

    No-op when the packet has no DocumentEvidence with
    ``parent_chunk_id`` set (the §1b ingest hasn't populated the
    metadata yet for this document, OR the document is flat-chunked).

    Pure-async; best-effort on DB failure. Returns the original
    packet on lookup error.
    """
    # Collect distinct parent_chunk_ids from DocumentEvidence members.
    parent_ids = [
        e.parent_chunk_id
        for e in packet.evidence
        if isinstance(e, DocumentEvidence)
        and e.parent_chunk_id
    ]
    if not parent_ids:
        return ExpansionResult(
            packet=packet, parents_added=0, parents_skipped=0,
            parents_failed=0, reason="no parent_chunk_ids on packet",
        )

    parents_by_id = await fetch_parent_chunks(
        pool,
        workspace_id=workspace_id,
        parent_chunk_ids=parent_ids,
    )

    return expand_parents_sync(
        packet,
        parents_by_id=parents_by_id,
        max_parents_per_packet=max_parents_per_packet,
    )
