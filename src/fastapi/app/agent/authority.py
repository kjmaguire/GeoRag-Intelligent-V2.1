"""Plan §3b — document authority ranking.

Maps a ``document_type`` string to one of five authority ranks and
sorts an :class:`EvidencePacket` so the highest-authority evidence
appears first (the LLM reads top-to-bottom; ordering matters).

This is the foundation for plan §3b multi-document synthesis:

  - For property-level / project-level questions, retrieval pulls from
    ALL relevant documents (not just the highest-authority one).
  - The primary claim in the answer should reference the highest-
    authority source.
  - Lower-authority sources surface as supporting context — explicitly
    flagged when they CONFLICT with the high-authority claim, never
    silently merged. (Global Invariant 7.)

This module does NOT do the conflict detection or the response-side
surfacing — those wire into ``response_assembler`` and are downstream.
This file ships the ranking primitive that future code reads.

Authority hierarchy (plan §3b verbatim):

  Rank 1  Very High  — NI 43-101 / Technical Report, Feasibility Study
                      (PEA/PFS/FS), Reserve/Resource Statement
  Rank 2  High       — Government Assessment Report, Annual Report,
                      Fact Sheet, Corporate Disclosure (43-101F1)
  Rank 3  Medium     — Press Release, Investor Presentation, Corporate
                      Presentation
  Rank 4  Medium-Low — Historical Report (varies by age — caller passes
                      `age_years` for finer-grained scoring)
  Rank 5  Low        — Internal Notes, Uncited Imported Text, Email,
                      Memo, Field Note
"""

from __future__ import annotations

import re
from typing import Iterable

from app.agent.evidence import (
    DocumentEvidence,
    EvidencePacket,
    EvidenceUnion,
)


__all__ = [
    "DEFAULT_AUTHORITY_RANK",
    "DOCUMENT_TYPE_RANK_PATTERNS",
    "infer_authority_rank",
    "rank_evidence_by_authority",
    "annotate_evidence_packet_with_authority",
]


# ---------------------------------------------------------------------------
# Authority hierarchy table — case-insensitive substring matching
# ---------------------------------------------------------------------------
#
# Order matters: earlier patterns win. Each entry is
#   (compiled-regex-pattern, rank).
# Patterns use word boundaries / hyphen-tolerance to handle the messy
# variants we see in ingested document metadata:
#   "NI 43-101", "NI43-101", "ni43101", "Technical Report NI 43-101F1", etc.
#
# Defensive: a document_type that matches no pattern falls back to
# DEFAULT_AUTHORITY_RANK (3 — middle of the road; not the worst rank,
# but not authoritative either). Logging the fallback would be the
# caller's responsibility.


DEFAULT_AUTHORITY_RANK: int = 3


DOCUMENT_TYPE_RANK_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    # ── Rank 1 — Very High ──────────────────────────────────────────────
    (re.compile(r"ni[\s\-]?43[\s\-]?101", re.IGNORECASE), 1),
    (re.compile(r"\btechnical[\s\-]?report\b", re.IGNORECASE), 1),
    (re.compile(r"\bfeasibility[\s\-]?study\b", re.IGNORECASE), 1),
    (re.compile(r"\b(?:p?(?:p?fs|fs|pea))\b", re.IGNORECASE), 1),
    (re.compile(r"\b(?:reserve|resource)[\s\-]?(?:estimate|statement)\b", re.IGNORECASE), 1),
    (re.compile(r"\bjorc\b", re.IGNORECASE), 1),
    (re.compile(r"\bcrirsco\b", re.IGNORECASE), 1),
    # ── Rank 2 — High ───────────────────────────────────────────────────
    (re.compile(r"\bassessment[\s\-]?report\b", re.IGNORECASE), 2),
    (re.compile(r"\bannual[\s\-]?(?:report|filing)\b", re.IGNORECASE), 2),
    (re.compile(r"\bfact[\s\-]?sheet\b", re.IGNORECASE), 2),
    (re.compile(r"\b43[\s\-]?101F1?\b", re.IGNORECASE), 2),
    (re.compile(r"\bgovernment[\s\-]?disclosure\b", re.IGNORECASE), 2),
    (re.compile(r"\bsedar\b", re.IGNORECASE), 2),
    # ── Rank 3 — Medium ─────────────────────────────────────────────────
    (re.compile(r"\bpress[\s\-]?release\b", re.IGNORECASE), 3),
    (re.compile(r"\binvestor[\s\-]?(?:presentation|deck)\b", re.IGNORECASE), 3),
    (re.compile(r"\bcorporate[\s\-]?presentation\b", re.IGNORECASE), 3),
    (re.compile(r"\bnews[\s\-]?release\b", re.IGNORECASE), 3),
    # ── Rank 4 — Medium-Low ─────────────────────────────────────────────
    (re.compile(r"\bhistorical[\s\-]?report\b", re.IGNORECASE), 4),
    # 'archive', 'archived', and 'archival' have different stems —
    # "archive" is NOT a substring of "archival" (no 'e' after 'archiv').
    (re.compile(r"\barchiv(?:ed|al|e)[\s\-]?report\b", re.IGNORECASE), 4),
    (re.compile(r"\blegacy[\s\-]?(?:report|data)\b", re.IGNORECASE), 4),
    # ── Rank 5 — Low ────────────────────────────────────────────────────
    (re.compile(r"\binternal[\s\-]?(?:notes?|memo|memorandum)\b", re.IGNORECASE), 5),
    (re.compile(r"\bemail\b", re.IGNORECASE), 5),
    (re.compile(r"\bfield[\s\-]?note\b", re.IGNORECASE), 5),
    (re.compile(r"\buncited\b", re.IGNORECASE), 5),
)


def infer_authority_rank(document_type: str | None) -> int:
    """Return the authority rank (1–5) for a document_type string.

    Pure function, no I/O. ``None`` / empty / unmatched →
    :data:`DEFAULT_AUTHORITY_RANK` (3).

    Args:
        document_type: A free-form string from
            ``DocumentEvidence.document_type`` or
            ``silver.reports.report_type`` etc. Pattern matched case-
            insensitively against the table above.

    Returns:
        An int in [1, 5]. 1 = highest authority.
    """
    if not document_type:
        return DEFAULT_AUTHORITY_RANK
    for pattern, rank in DOCUMENT_TYPE_RANK_PATTERNS:
        if pattern.search(document_type):
            return rank
    return DEFAULT_AUTHORITY_RANK


# ---------------------------------------------------------------------------
# Packet-level sorting + annotation
# ---------------------------------------------------------------------------


def _sort_key(e: EvidenceUnion) -> tuple[int, int, float]:
    """Stable sort key.

    Tuple components, in order:
      1. authority_rank (lower = better; only DocumentEvidence has it)
      2. is_current bool inverted (current first, superseded after)
      3. negative confidence (higher confidence first within same rank)

    Non-Document evidence types get a "middle" authority position
    (DEFAULT_AUTHORITY_RANK + 0 padding) so they interleave naturally
    with mid-rank documents rather than sinking to the bottom.
    """
    if isinstance(e, DocumentEvidence):
        # is_current: True (1) flipped to 0 so current sorts before
        # superseded (0 < 1 in tuple compare).
        return (e.authority_rank, 0 if e.is_current else 1, -e.confidence)
    # Other kinds: sort to the middle rank, treat as "current".
    return (DEFAULT_AUTHORITY_RANK, 0, -getattr(e, "confidence", 1.0))


def rank_evidence_by_authority(
    packet: EvidencePacket,
) -> EvidencePacket:
    """Return a new :class:`EvidencePacket` with the evidence list re-
    sorted by authority (best first), then by currency
    (current before superseded), then by confidence (descending).

    Sort is stable: ties preserve original order. Pure function — the
    input packet is not mutated; a shallow copy with re-ordered evidence
    is returned so the caller can replace the packet in flight without
    affecting upstream references.

    Non-DocumentEvidence members (Table/Assay/Collar/Spatial/Graph) all
    share the default mid-rank and therefore stay clustered together in
    their pre-sort relative order between the high-authority documents
    and the low-authority ones.
    """
    sorted_evidence = sorted(packet.evidence, key=_sort_key)
    return packet.model_copy(update={"evidence": sorted_evidence})


def annotate_evidence_packet_with_authority(
    packet: EvidencePacket,
) -> EvidencePacket:
    """Return a new packet whose :class:`DocumentEvidence` members have
    their ``authority_rank`` field re-computed from ``document_type``.

    Useful when the retrieval layer constructed Document evidence with
    the default rank (3) and the caller wants to refresh based on what
    the document_type actually says. Non-Document members pass through
    unchanged.

    Idempotent: re-annotating a packet whose ranks already match
    document_type is a no-op (the returned packet still differs by
    identity from the input).
    """
    updated: list[EvidenceUnion] = []
    for e in packet.evidence:
        if isinstance(e, DocumentEvidence):
            inferred = infer_authority_rank(e.document_type)
            if inferred != e.authority_rank:
                e = e.model_copy(update={"authority_rank": inferred})
        updated.append(e)
    return packet.model_copy(update={"evidence": updated})


def iter_top_authority(
    packet: EvidencePacket,
    *,
    limit: int | None = None,
) -> Iterable[DocumentEvidence]:
    """Yield document evidence in authority order, optionally limited.

    Convenience for the response assembler: 'give me the top N
    highest-authority documents that contributed to this answer' for
    the citation header / primary-source claim block.
    """
    ranked = rank_evidence_by_authority(packet)
    if limit is not None and limit <= 0:
        return
    count = 0
    for e in ranked.evidence:
        if not isinstance(e, DocumentEvidence):
            continue
        yield e
        count += 1
        if limit is not None and count >= limit:
            break
