"""Plan §3c — source diversity reranking.

Given an authority-ranked :class:`EvidencePacket` (the output of plan §3b),
return a NEW packet whose ``evidence`` list has been re-ordered so that the
top-N entries draw from MULTIPLE evidence kinds rather than letting one
high-volume retrieval source crowd out the others.

Why this matters (plan §3c verbatim):

  "A high-recall document search can return 8 NI 43-101 chunks all from
  the same report; the answer should also see the spatial result, the
  assay row, and the graph hop that the smaller retrieval branches
  produced. Diversity reranking enforces a kind-quota over the
  authority-sorted candidate pool."

Two modes:

  1. **Round-robin** (default). Walk the per-kind queues in a fixed
     priority order and pick one each pass. Stops when ``max_total`` is
     reached or every queue is empty.

  2. **Quota** (explicit). Caller passes ``kind_quotas={kind: n}``.
     The output contains AT MOST ``n`` entries of each named kind, in
     authority order within that kind. Unnamed kinds get
     ``unspecified_quota`` (default 0 — drop them).

In both modes the **authority order is preserved within each kind**
(plan §3b is monotonic — diversity NEVER promotes a low-authority
member ahead of a higher-authority member of the same kind).

This module does NOT touch I/O and never mutates its input. The
returned packet is a ``model_copy`` of the original with a re-ordered /
trimmed ``evidence`` list and recomputed token + budget fields.

Wiring (later session):
  ``assemble_node`` → ``apply_source_diversity(state.evidence_packet)``
  after ``rank_evidence_by_authority`` runs in ``execute_node``.
  Wiring requires a per-intent quota table — that's a downstream call
  not made here.
"""

from __future__ import annotations

import logging
from typing import Mapping

from app.agent.evidence import EvidencePacket, EvidenceUnion


logger = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_KIND_PRIORITY",
    "apply_source_diversity",
    "compute_kind_distribution",
]


# ---------------------------------------------------------------------------
# Kind priority for round-robin mode
# ---------------------------------------------------------------------------
#
# The priority order matters for the FIRST pass only — once the
# round-robin has visited every non-empty queue once, the order of
# subsequent visits is irrelevant. We lead with ``document`` because the
# plan §4a answer format places "Direct answer" and "Key numbers" first,
# and those mostly cite documents. ``spatial`` comes early because the
# §6b MapLibre card is most useful when it appears alongside the answer
# rather than after a long tail of doc chunks.

DEFAULT_KIND_PRIORITY: tuple[str, ...] = (
    "document",
    "spatial",
    "assay",
    "table",
    "collar",
    "graph",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_source_diversity(
    packet: EvidencePacket,
    *,
    max_total: int | None = None,
    kind_quotas: Mapping[str, int] | None = None,
    unspecified_quota: int = 0,
    kind_priority: tuple[str, ...] = DEFAULT_KIND_PRIORITY,
) -> EvidencePacket:
    """Return a new packet with evidence reordered/trimmed for diversity.

    Two operating modes:

      * ``kind_quotas is None`` → **round-robin** across kinds, capped by
        ``max_total`` (default: keep all evidence — only re-order).
      * ``kind_quotas is not None`` → **quota** mode. Each kind gets at
        most ``kind_quotas[kind]`` entries; kinds NOT named get
        ``unspecified_quota`` (default 0 → dropped). ``max_total`` is
        applied AFTER quotas — useful for a hard ceiling on top of
        per-kind caps.

    Args:
        packet: Input packet (typically post-``rank_evidence_by_authority``).
        max_total: Hard ceiling on output evidence count. ``None`` =
            unlimited.
        kind_quotas: Per-kind allowance map. ``None`` enables round-robin.
        unspecified_quota: When ``kind_quotas`` IS set, kinds missing
            from the map get this allowance. Default 0 = drop.
        kind_priority: Order in which kinds are visited during
            round-robin's first pass. Reorder the default tuple to
            favour a different kind at the top of the output.

    Returns:
        A new :class:`EvidencePacket` (the input is never mutated). The
        copy's ``evidence`` field is the diversity-reranked list;
        ``total_tokens`` + ``remaining_budget`` are recomputed using the
        same chars/4 proxy the converter uses.

    Notes:
        - Stable within-kind: authority order from the input is
          preserved for every kept member.
        - ``max_total <= 0`` produces an empty packet (signals "drop
          all evidence" — useful for refusal-track flows).
        - ``packet.evidence`` empty returns the packet untouched.
    """
    # Defensive paths first.
    if not packet.evidence:
        return packet
    if max_total is not None and max_total <= 0:
        return _rebuild_with(packet, evidence=[])

    # Build a queue per kind, preserving the input's authority order.
    queues: dict[str, list[EvidenceUnion]] = {}
    for e in packet.evidence:
        queues.setdefault(e.kind, []).append(e)

    selected: list[EvidenceUnion]
    if kind_quotas is None:
        selected = _round_robin(queues, max_total, kind_priority)
    else:
        selected = _quota(
            queues,
            kind_quotas=kind_quotas,
            unspecified_quota=unspecified_quota,
            max_total=max_total,
            kind_priority=kind_priority,
        )

    if len(selected) == len(packet.evidence):
        # No change in membership; we may still have reordered. Skip the
        # token-budget recompute when the set is identical AND order
        # equal — pure no-op shortcut.
        same_order = all(
            a.evidence_id == b.evidence_id
            for a, b in zip(packet.evidence, selected)
        )
        if same_order:
            return packet

    return _rebuild_with(packet, evidence=selected)


def compute_kind_distribution(packet: EvidencePacket) -> dict[str, int]:
    """Return ``{kind: count}`` for the packet's current evidence list.

    Useful for trace logging and for callers deciding whether the packet
    is already diverse enough to skip the reranker. Pure read.
    """
    out: dict[str, int] = {}
    for e in packet.evidence:
        out[e.kind] = out.get(e.kind, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _round_robin(
    queues: dict[str, list[EvidenceUnion]],
    max_total: int | None,
    kind_priority: tuple[str, ...],
) -> list[EvidenceUnion]:
    """Walk kind queues in priority order, picking one each pass."""
    selected: list[EvidenceUnion] = []
    # Ordered list of kind names: priority entries that actually exist
    # in the queues first, then any other kinds (alphabetical for
    # determinism).
    ordered_kinds: list[str] = [k for k in kind_priority if queues.get(k)]
    extra = sorted(k for k in queues.keys() if k not in ordered_kinds)
    ordered_kinds.extend(extra)

    # Round-robin loop — exit when every queue is empty or max_total
    # is hit. The total count of items across queues bounds the outer
    # loop so we can never spin forever.
    total_remaining = sum(len(q) for q in queues.values())
    while total_remaining > 0:
        progressed = False
        for kind in ordered_kinds:
            q = queues.get(kind)
            if not q:
                continue
            selected.append(q.pop(0))
            total_remaining -= 1
            progressed = True
            if max_total is not None and len(selected) >= max_total:
                return selected
        if not progressed:
            break
    return selected


def _quota(
    queues: dict[str, list[EvidenceUnion]],
    *,
    kind_quotas: Mapping[str, int],
    unspecified_quota: int,
    max_total: int | None,
    kind_priority: tuple[str, ...],
) -> list[EvidenceUnion]:
    """Apply explicit per-kind quotas, then optionally cap to max_total.

    Output order: kind_priority for known kinds (and known kinds with
    non-zero quotas come BEFORE the alphabetical extras). Within each
    kind, the input's authority order is preserved. The final list is
    then trimmed to ``max_total`` if it's set.
    """
    selected: list[EvidenceUnion] = []
    ordered_kinds: list[str] = [k for k in kind_priority if k in queues]
    extra = sorted(k for k in queues.keys() if k not in ordered_kinds)
    ordered_kinds.extend(extra)

    for kind in ordered_kinds:
        q = queues.get(kind) or []
        quota = kind_quotas.get(kind, unspecified_quota)
        if quota <= 0:
            continue
        selected.extend(q[:quota])
    if max_total is not None and len(selected) > max_total:
        selected = selected[:max_total]
    return selected


def _rebuild_with(
    packet: EvidencePacket,
    *,
    evidence: list[EvidenceUnion],
) -> EvidencePacket:
    """Return a ``model_copy`` of the packet with new evidence + recomputed
    token + budget fields."""
    # Re-derive total_tokens from the kept members. Reuses the converter's
    # proxy so packet arithmetic stays consistent across modules.
    from app.agent.evidence_converter import estimate_evidence_tokens  # noqa: PLC0415

    new_total = sum(estimate_evidence_tokens(e) for e in evidence)
    new_remaining = (
        packet.remaining_budget
        + packet.total_tokens
        - new_total
    )
    return packet.model_copy(
        update={
            "evidence": evidence,
            "total_tokens": new_total,
            "remaining_budget": new_remaining,
        }
    )
