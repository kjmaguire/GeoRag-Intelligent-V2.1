"""Unit tests for plan §3f dynamic context budgeting."""

from __future__ import annotations

import pytest

from app.agent.context_budget import (
    BudgetTrimResult,
    enforce_token_budget,
    estimate_budget_pressure,
)
from app.agent.evidence import (
    AssayEvidence,
    DocumentEvidence,
    EvidencePacket,
    SpatialEvidence,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc(
    title: str,
    *,
    authority_rank: int = 1,
    is_current: bool = True,
    confidence: float = 1.0,
    text: str = "x",
) -> DocumentEvidence:
    return DocumentEvidence(
        document_id=title.lower(),
        document_title=title,
        document_type="NI 43-101",
        authority_rank=authority_rank,
        is_current=is_current,
        confidence=confidence,
        page=1,
        chunk_id=f"chunk-{title.lower()}",
        text=text,
        char_start=0,
        char_end=len(text),
    )


def _assay() -> AssayEvidence:
    return AssayEvidence(
        project_id="p", hole_id="X",
        depth_from_m=0.0, depth_to_m=10.0, interval_length_m=10.0,
        commodity="Au", value=1.0, unit="g/t",
    )


def _spatial() -> SpatialEvidence:
    return SpatialEvidence(
        geometry_type="polygon",
        crs="EPSG:26913",
        spatial_operation="intersects",
        intersecting_entities=["zone-1"],
    )


def _packet(
    evidence,
    *,
    total_tokens: int = 0,
    system_prompt_tokens: int = 0,
    remaining_budget: int = 0,
) -> EvidencePacket:
    return EvidencePacket(
        query_id="q-1",
        query_text="x",
        evidence=evidence,
        total_tokens=total_tokens,
        system_prompt_tokens=system_prompt_tokens,
        remaining_budget=remaining_budget,
    )


# ---------------------------------------------------------------------------
# Fast paths
# ---------------------------------------------------------------------------


def test_packet_that_already_fits_is_no_op():
    packet = _packet(
        [_doc("A"), _doc("B")],
        total_tokens=100,
        remaining_budget=500,
    )
    result = enforce_token_budget(packet)
    assert isinstance(result, BudgetTrimResult)
    assert result.reached_target is True
    assert result.dropped_evidence_ids == []
    assert result.packet.evidence == packet.evidence


def test_empty_packet_returns_no_op_unreached_when_budget_negative():
    packet = _packet([], total_tokens=0, remaining_budget=-50)
    result = enforce_token_budget(packet)
    assert result.reached_target is False
    assert result.dropped_evidence_ids == []
    assert "no evidence" in (result.reason or "")


def test_empty_packet_returns_no_op_reached_when_budget_non_negative():
    packet = _packet([], total_tokens=0, remaining_budget=0)
    result = enforce_token_budget(packet)
    assert result.reached_target is True
    assert result.dropped_evidence_ids == []


# ---------------------------------------------------------------------------
# Drop ordering
# ---------------------------------------------------------------------------


def test_lowest_authority_dropped_first():
    """When dropping is needed, rank-5 evidence goes before rank-1."""
    high = _doc("Keeper", authority_rank=1, text="x")
    low = _doc("Dropme", authority_rank=5, text="x")
    # Two docs of same `kind` so min_per_kind=1 still allows one drop.
    packet = _packet(
        [high, low],
        total_tokens=2,
        remaining_budget=-1,  # one drop is enough
    )
    result = enforce_token_budget(packet)
    assert result.reached_target is True
    titles = [e.document_title for e in result.packet.evidence]
    assert titles == ["Keeper"]
    assert len(result.dropped_evidence_ids) == 1


def test_superseded_dropped_before_current_at_same_rank():
    keeper = _doc("Current", authority_rank=1, is_current=True)
    dropme = _doc("Superseded", authority_rank=1, is_current=False)
    packet = _packet(
        [keeper, dropme],
        total_tokens=2,
        remaining_budget=-1,
    )
    result = enforce_token_budget(packet)
    assert result.reached_target is True
    titles = [e.document_title for e in result.packet.evidence]
    assert titles == ["Current"]


def test_lowest_confidence_dropped_at_same_rank_and_currency():
    keeper = _doc("HighConf", authority_rank=1, confidence=0.95)
    dropme = _doc("LowConf", authority_rank=1, confidence=0.3)
    packet = _packet(
        [keeper, dropme],
        total_tokens=2,
        remaining_budget=-1,
    )
    result = enforce_token_budget(packet)
    titles = [e.document_title for e in result.packet.evidence]
    assert titles == ["HighConf"]


def test_multiple_drops_until_budget_fits():
    # Each low-rank doc is ~40 chars → estimate_evidence_tokens returns
    # ~10 tokens each. Five of them = 50 tokens freed. Start with -40
    # deficit so 4 drops put us in the black (reached_target=True), but
    # all 5 droppable members are eligible until budget is non-negative.
    docs = [
        _doc(f"D{i}", authority_rank=5, text="x" * 40)
        for i in range(5)
    ]
    keeper = _doc("Keeper", authority_rank=1, text="x")
    packet = _packet(
        [keeper, *docs],
        total_tokens=1000,
        remaining_budget=-30,
    )
    result = enforce_token_budget(packet, min_per_kind=1)
    assert result.reached_target is True
    # The high-authority keeper must survive.
    titles = [e.document_title for e in result.packet.evidence]
    assert "Keeper" in titles
    # And at least three low-authority docs were dropped (enough to
    # cover the deficit). The exact count depends on the per-drop
    # token estimate; we just assert the floor allowed it to reach
    # ≥ 0 by dropping low-authority members first.
    assert len(result.dropped_evidence_ids) >= 3


# ---------------------------------------------------------------------------
# Per-kind floor
# ---------------------------------------------------------------------------


def test_per_kind_floor_blocks_full_strip():
    """With min_per_kind=1, the last doc of a kind cannot be dropped
    even if dropping it would fit the budget."""
    only_doc = _doc("LonelyDoc", authority_rank=5, text="x" * 50)
    packet = _packet(
        [only_doc],
        total_tokens=12,
        remaining_budget=-12,
    )
    result = enforce_token_budget(packet, min_per_kind=1)
    assert result.reached_target is False  # floor pinned the last doc
    assert result.packet.evidence == [only_doc]
    assert "floor" in (result.reason or "").lower()


def test_min_per_kind_zero_allows_full_strip():
    only_doc = _doc("Strippable", authority_rank=5, text="x" * 50)
    packet = _packet(
        [only_doc],
        total_tokens=12,
        remaining_budget=-12,
    )
    result = enforce_token_budget(packet, min_per_kind=0)
    assert result.reached_target is True
    assert result.packet.evidence == []
    assert len(result.dropped_evidence_ids) == 1


def test_floor_preserves_each_kind_separately():
    """With min_per_kind=1, every PRESENT kind keeps at least one
    representative even when budget is tight."""
    doc1 = _doc("d1", authority_rank=5, text="x" * 100)
    doc2 = _doc("d2", authority_rank=1, text="x" * 100)
    assay = _assay()
    spatial = _spatial()
    packet = _packet(
        [doc1, doc2, assay, spatial],
        total_tokens=200,
        remaining_budget=-50,
    )
    result = enforce_token_budget(packet, min_per_kind=1)
    kinds_left = {e.kind for e in result.packet.evidence}
    # All four kinds still have at least one entry — wait, only 3 kinds.
    assert kinds_left == {"document", "assay", "spatial"}


# ---------------------------------------------------------------------------
# Protected kinds
# ---------------------------------------------------------------------------


def test_protected_kind_never_dropped():
    """Spatial evidence flagged as protected stays even if it's the
    cheapest drop. The doc gets dropped instead."""
    doc = _doc("d", authority_rank=5, text="x" * 100)
    spatial = _spatial()
    packet = _packet(
        [doc, spatial],
        total_tokens=200,
        remaining_budget=-1,
    )
    result = enforce_token_budget(
        packet,
        min_per_kind=0,  # no floor so doc is droppable to zero
        protected_kinds={"spatial"},
    )
    assert any(e.kind == "spatial" for e in result.packet.evidence)


def test_protected_kind_can_pin_budget_unreachable():
    """If the only droppable members are protected, the budget stays
    negative and `reached_target=False`."""
    spatial = _spatial()
    packet = _packet(
        [spatial],
        total_tokens=100,
        remaining_budget=-100,
    )
    result = enforce_token_budget(
        packet,
        min_per_kind=0,
        protected_kinds={"spatial"},
    )
    assert result.reached_target is False
    assert "spatial" in (result.reason or "")


# ---------------------------------------------------------------------------
# Budget arithmetic
# ---------------------------------------------------------------------------


def test_drops_recompute_total_tokens_and_remaining_budget():
    """After trimming, ``total_tokens`` shrinks and ``remaining_budget``
    grows by the same amount each drop frees."""
    docs = [_doc(f"D{i}", authority_rank=5, text="x" * 40) for i in range(3)]
    packet = _packet(
        docs,
        total_tokens=30,
        remaining_budget=-10,
    )
    result = enforce_token_budget(packet, min_per_kind=1)
    # remaining_budget should now be ≥ 0.
    assert result.packet.remaining_budget >= 0
    # total_tokens shrank.
    assert result.packet.total_tokens < packet.total_tokens
    # Invariant: total_tokens + remaining_budget = const (no system_prompt change).
    assert (
        result.packet.total_tokens + result.packet.remaining_budget
        == packet.total_tokens + packet.remaining_budget
    )


def test_max_context_tokens_kw_recomputes_budget():
    """When ``max_context_tokens`` is supplied, the function recomputes
    ``remaining_budget`` from that ceiling first (and may then trim)."""
    packet = _packet(
        [_doc("A", text="x" * 4)],
        total_tokens=1,
        system_prompt_tokens=10,
        remaining_budget=1000,  # input claims comfortable budget
    )
    # Caller passes a much smaller window: 12 tokens. After accounting
    # for system_prompt(10) + total(1) → remaining = 12-10-1 = 1.
    # That's ≥ 0 so no trim, but the field is updated.
    result = enforce_token_budget(packet, max_context_tokens=12, min_per_kind=1)
    assert result.reached_target is True
    assert result.packet.remaining_budget == 1


def test_max_context_tokens_kw_forces_trim_when_window_shrunk():
    docs = [_doc(f"D{i}", authority_rank=5, text="x" * 40) for i in range(3)]
    packet = _packet(
        docs,
        total_tokens=30,
        system_prompt_tokens=5,
        remaining_budget=1000,  # stale; doesn't reflect tight window
    )
    # Tight window: 10 total context → remaining = 10-5-30 = -25; trim.
    result = enforce_token_budget(packet, max_context_tokens=10, min_per_kind=1)
    # min_per_kind=1 protects the last doc; whether we reach ≥ 0
    # depends on the kept doc's size. Either outcome is acceptable; we
    # only check the post-condition: SOMETHING was dropped.
    assert len(result.dropped_evidence_ids) >= 1


# ---------------------------------------------------------------------------
# Pure-function invariant
# ---------------------------------------------------------------------------


def test_pure_function_does_not_mutate_input():
    docs = [_doc(f"D{i}", authority_rank=5, text="x" * 40) for i in range(3)]
    packet = _packet(
        docs,
        total_tokens=30,
        remaining_budget=-10,
    )
    before_ids = [e.evidence_id for e in packet.evidence]
    _ = enforce_token_budget(packet, min_per_kind=1)
    after_ids = [e.evidence_id for e in packet.evidence]
    assert before_ids == after_ids
    # Field copies unchanged on input.
    assert packet.remaining_budget == -10


# ---------------------------------------------------------------------------
# BudgetTrimResult unpacking
# ---------------------------------------------------------------------------


def test_result_unpacks_as_tuple():
    packet = _packet([_doc("A")], total_tokens=1, remaining_budget=500)
    result = enforce_token_budget(packet)
    a, b, c, d = result
    assert a is result.packet
    assert b == []
    assert c is True
    assert d is None


# ---------------------------------------------------------------------------
# estimate_budget_pressure
# ---------------------------------------------------------------------------


def test_pressure_zero_when_budget_is_more_than_half_the_window():
    packet = _packet(
        [_doc("A")],
        total_tokens=100,
        system_prompt_tokens=100,
        remaining_budget=300,  # 300 / 500 = 60% → comfortable
    )
    assert estimate_budget_pressure(packet) == 0.0


def test_pressure_one_when_budget_is_negative():
    packet = _packet(
        [_doc("A")],
        total_tokens=100,
        system_prompt_tokens=100,
        remaining_budget=-50,
    )
    assert estimate_budget_pressure(packet) == 1.0


def test_pressure_in_middle_when_budget_is_tight():
    # Window 1000, remaining 100 → 10% remaining → pressure ramps high.
    packet = _packet(
        [_doc("A")],
        total_tokens=400,
        system_prompt_tokens=500,
        remaining_budget=100,
    )
    p = estimate_budget_pressure(packet)
    assert 0.0 < p < 1.0
    # 10% remaining: pressure = (0.5 - 0.1) * 2 = 0.8
    assert abs(p - 0.8) < 1e-9


def test_pressure_empty_window_returns_zero():
    packet = _packet(
        [],
        total_tokens=0,
        system_prompt_tokens=0,
        remaining_budget=0,
    )
    assert estimate_budget_pressure(packet) == 0.0


# ---------------------------------------------------------------------------
# Parametric sweep — invariants under varying budget
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("remaining_budget", [-100, -10, -1])
def test_always_drops_at_least_one_when_over_budget_and_floor_allows(
    remaining_budget,
):
    """Property: as long as some kind has > min_per_kind entries, the
    drop loop is guaranteed to drop at least one member."""
    # Three same-kind docs: floor=1 allows two drops, leaving one.
    docs = [_doc(f"D{i}", authority_rank=5, text="x" * 30) for i in range(3)]
    packet = _packet(docs, total_tokens=22, remaining_budget=remaining_budget)
    result = enforce_token_budget(packet, min_per_kind=1)
    assert len(result.dropped_evidence_ids) >= 1
