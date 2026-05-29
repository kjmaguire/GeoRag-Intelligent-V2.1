"""Plan §3f — dynamic context budgeting.

Once :func:`apply_source_diversity` has reordered the packet for kind
balance, the packet may STILL be over the model's context window
(``remaining_budget < 0``). This module trims the packet from the
*bottom* of the authority list — dropping low-authority / superseded /
low-confidence members one by one — until ``remaining_budget ≥ 0`` or
a per-kind floor stops further drops.

Why this is its own pass:

  - :func:`rank_evidence_by_authority` (§3b) decides ORDER.
  - :func:`apply_source_diversity` (§3c) decides MIX.
  - This module decides FIT — the packet has to actually pass the
    model's token cap, after the previous two decisions.

The drop order mirrors §3b's sort key, REVERSED:

  ``(authority_rank DESC, is_current False first, confidence ASC)``

So at the same rank: superseded docs go before current docs; same
currency: lowest-confidence first. The very-first drop is therefore
the rank-5 / superseded / low-confidence corner — exactly the
member the LLM would have read last anyway.

Per-kind floor (``min_per_kind``):

  When a kind has only ``min_per_kind`` entries left, it's protected
  — we stop dropping from that kind even if dropping it would help
  the budget. Rationale: a synthesis answer that has zero spatial
  evidence is structurally worse than one that's barely over budget.
  If the floor pins enough evidence to make the budget unreachable,
  the function returns the current best-effort packet with a negative
  ``remaining_budget`` value — the caller (assemble_node) sees the
  signal and can refuse, demote, or repair.

Pure function: returns a fresh :class:`EvidencePacket` via ``model_copy``;
input is never mutated.

Wiring (later session, paired with §3c wiring):
  ``assemble_node`` →
    ``apply_source_diversity(rank_evidence_by_authority(packet))`` →
    ``enforce_token_budget(packet, max_context_tokens=settings.MAX_CONTEXT_TOKENS)``
  before the LLM call.
"""

from __future__ import annotations

import logging
from typing import Iterable

from app.agent.authority import DEFAULT_AUTHORITY_RANK
from app.agent.evidence import (
    DocumentEvidence,
    EvidencePacket,
    EvidenceUnion,
)


logger = logging.getLogger(__name__)


__all__ = [
    "enforce_token_budget",
    "BudgetTrimResult",
    "estimate_budget_pressure",
]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


class BudgetTrimResult:
    """Outcome of a budget trim pass.

    Carries the trimmed packet plus a small audit trail — what got
    dropped, how many drops were attempted, whether the budget could
    actually be brought back to ≥ 0, and (when the floor pins evidence)
    a reason string the caller can surface in a guard message.

    Not a NamedTuple / dataclass on purpose: we want
    ``packet, dropped, reached_target = enforce_token_budget(...)``
    to keep working AS WELL AS attribute access. Simple ``.unpack()``
    method handles the tuple form.
    """

    __slots__ = ("packet", "dropped_evidence_ids", "reached_target", "reason")

    def __init__(
        self,
        *,
        packet: EvidencePacket,
        dropped_evidence_ids: list[str],
        reached_target: bool,
        reason: str | None,
    ) -> None:
        self.packet = packet
        self.dropped_evidence_ids = dropped_evidence_ids
        self.reached_target = reached_target
        self.reason = reason

    def __iter__(self):
        """Allow ``packet, dropped, reached, reason = result``."""
        yield self.packet
        yield self.dropped_evidence_ids
        yield self.reached_target
        yield self.reason

    def __repr__(self) -> str:  # pragma: no cover — convenience
        return (
            f"BudgetTrimResult(remaining_budget={self.packet.remaining_budget}, "
            f"dropped={len(self.dropped_evidence_ids)}, "
            f"reached_target={self.reached_target}, reason={self.reason!r})"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enforce_token_budget(
    packet: EvidencePacket,
    *,
    max_context_tokens: int | None = None,
    min_per_kind: int = 1,
    protected_kinds: Iterable[str] = (),
) -> BudgetTrimResult:
    """Trim the packet until it fits the context window, or as close as
    the per-kind floor allows.

    Args:
        packet: Input packet. Typically post-``apply_source_diversity``.
        max_context_tokens: When provided, the budget target is
            ``max_context_tokens - packet.system_prompt_tokens``. When
            ``None``, the input packet's ``remaining_budget`` field is
            already the truth — the function trims until that field
            reaches ≥ 0.
        min_per_kind: Per-kind floor. A kind with ``min_per_kind``
            entries is protected from further drops. Default 1 — drop
            everything from a kind only when the kind is over-represented.
            Set to 0 to disable the floor (drops can fully strip a kind).
        protected_kinds: Iterable of kind names that must NEVER lose any
            evidence regardless of ``min_per_kind``. Use this when the
            caller has domain knowledge (e.g. "spatial evidence is
            required for §6b MapLibre rendering — never drop it").

    Returns:
        :class:`BudgetTrimResult`. Unpacks as
        ``(packet, dropped_ids, reached_target, reason)``.

    Notes:
        - When ``packet.evidence`` is empty → no-op, ``reached_target``
          mirrors whether the existing ``remaining_budget`` is ≥ 0.
        - When the packet ALREADY fits → no-op, ``reached_target=True``.
        - When the floor blocks further drops → returns the best-effort
          trimmed packet; ``reached_target=False`` and ``reason`` carries
          a short human-readable string.
    """
    # Step 1 — compute the target remaining_budget. The caller may have
    # tightened the ceiling since the packet was built (e.g. switched
    # to a smaller model); recompute remaining_budget from
    # max_context_tokens when supplied.
    work_packet = packet
    if max_context_tokens is not None:
        new_remaining = (
            max_context_tokens
            - packet.system_prompt_tokens
            - packet.total_tokens
        )
        if new_remaining != packet.remaining_budget:
            work_packet = packet.model_copy(
                update={"remaining_budget": new_remaining},
            )

    # Step 2 — fast path: already fits.
    if work_packet.remaining_budget >= 0:
        return BudgetTrimResult(
            packet=work_packet,
            dropped_evidence_ids=[],
            reached_target=True,
            reason=None,
        )

    # Step 3 — empty packet: nothing left to drop.
    if not work_packet.evidence:
        return BudgetTrimResult(
            packet=work_packet,
            dropped_evidence_ids=[],
            reached_target=False,
            reason="packet has no evidence to drop",
        )

    # Step 4 — drop loop.
    protected_set = set(protected_kinds)
    return _drop_loop(
        work_packet,
        min_per_kind=max(0, min_per_kind),
        protected_kinds=protected_set,
    )


def estimate_budget_pressure(packet: EvidencePacket) -> float:
    """Return a 0.0-1.0 pressure score for the packet's budget.

    0.0 = comfortable (≥ 50% of context remains).
    0.5 = tight (between 0 and 50% remaining).
    1.0 = over budget (remaining_budget < 0).

    Convenience for the trace + UI surface — both want a single number
    to colour-code a pressure indicator without re-computing the
    fraction at every call site.
    """
    total_window = packet.system_prompt_tokens + packet.total_tokens + packet.remaining_budget
    if total_window <= 0:
        return 0.0
    if packet.remaining_budget < 0:
        return 1.0
    fraction_remaining = packet.remaining_budget / total_window
    if fraction_remaining >= 0.5:
        return 0.0
    # Linear ramp from 0.5 down to 0.0 fraction → 0.0 up to 1.0 pressure.
    return max(0.0, min(1.0, (0.5 - fraction_remaining) * 2.0))


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _drop_loop(
    packet: EvidencePacket,
    *,
    min_per_kind: int,
    protected_kinds: set[str],
) -> BudgetTrimResult:
    """Iteratively drop the worst evidence until budget fits or the
    floor stops us."""
    from app.agent.evidence_converter import estimate_evidence_tokens  # noqa: PLC0415

    evidence = list(packet.evidence)
    dropped_ids: list[str] = []

    # Compute droppability rank for each member up-front; the order in
    # which we drop is the worst → best traversal of this sort. Within
    # the loop we recompute the per-kind count, so we know when a kind
    # is at its floor.
    droppability_order = sorted(
        evidence,
        key=_drop_rank,
        reverse=True,  # worst FIRST
    )

    # We'll mutate `evidence` (the kept list) and the per-kind counts;
    # `droppability_order` is the iteration plan.
    kind_counts: dict[str, int] = {}
    for e in evidence:
        kind_counts[e.kind] = kind_counts.get(e.kind, 0) + 1

    remaining = packet.remaining_budget
    total_tokens = packet.total_tokens

    floor_blocked_kinds: set[str] = set()

    for candidate in droppability_order:
        if remaining >= 0:
            break
        kind = candidate.kind
        if kind in protected_kinds:
            floor_blocked_kinds.add(kind)
            continue
        if kind_counts.get(kind, 0) <= min_per_kind:
            floor_blocked_kinds.add(kind)
            continue
        # Drop this member.
        try:
            evidence.remove(candidate)
        except ValueError:  # pragma: no cover — shouldn't happen
            continue
        kind_counts[kind] -= 1
        freed = estimate_evidence_tokens(candidate)
        total_tokens -= freed
        remaining += freed
        dropped_ids.append(candidate.evidence_id)

    reached = remaining >= 0
    reason: str | None = None
    if not reached:
        if floor_blocked_kinds or protected_kinds:
            pinned = sorted(floor_blocked_kinds | protected_kinds)
            reason = (
                f"per-kind floor pinned {len(pinned)} kind(s) — "
                f"cannot drop further: {pinned}"
            )
        else:
            reason = "drop loop exhausted with budget still negative"

    trimmed = packet.model_copy(
        update={
            "evidence": evidence,
            "total_tokens": max(0, total_tokens),
            "remaining_budget": remaining,
        },
    )
    return BudgetTrimResult(
        packet=trimmed,
        dropped_evidence_ids=dropped_ids,
        reached_target=reached,
        reason=reason,
    )


def _drop_rank(e: EvidenceUnion) -> tuple[int, int, float]:
    """Droppability sort key.

    Returns a tuple suitable for ``sorted(..., reverse=True)`` — bigger
    means worse (drop earlier). Mirrors authority._sort_key but flipped
    so the natural reverse-sort puts low-authority / superseded /
    low-confidence members at the head of the drop queue.

    Components:
      1. authority_rank — bigger is worse (rank 5 > rank 1)
      2. is_current inverted — superseded (1) > current (0)
      3. confidence — flipped: lower confidence sorts AFTER higher when
         ``reverse=True`` lifts smaller numbers up. We store
         ``1.0 - confidence`` so lower confidence = bigger key = drop
         first.
    """
    if isinstance(e, DocumentEvidence):
        return (e.authority_rank, 0 if e.is_current else 1, 1.0 - e.confidence)
    # Non-document kinds get the same middle rank as in authority._sort_key.
    return (DEFAULT_AUTHORITY_RANK, 0, 1.0 - getattr(e, "confidence", 1.0))
