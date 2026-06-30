"""Unit tests for plan §3c source diversity reranking."""

from __future__ import annotations

import pytest

from app.agent.evidence import (
    AssayEvidence,
    CollarEvidence,
    DocumentEvidence,
    EvidencePacket,
    GraphEvidence,
    SpatialEvidence,
)
from app.agent.source_diversity import (
    DEFAULT_KIND_PRIORITY,
    apply_source_diversity,
    compute_kind_distribution,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc(title: str, authority_rank: int = 1) -> DocumentEvidence:
    return DocumentEvidence(
        document_id=title.lower(),
        document_title=title,
        document_type="NI 43-101",
        authority_rank=authority_rank,
        is_current=True,
        confidence=1.0,
        page=1,
        chunk_id=f"chunk-{title.lower()}",
        text=f"text from {title}",
        char_start=0,
        char_end=10,
    )


def _assay(hole_id: str) -> AssayEvidence:
    return AssayEvidence(
        project_id="p",
        hole_id=hole_id,
        depth_from_m=0.0,
        depth_to_m=10.0,
        interval_length_m=10.0,
        commodity="Au",
        value=1.0,
        unit="g/t",
    )


def _collar(hole_id: str) -> CollarEvidence:
    return CollarEvidence(
        hole_id=hole_id, easting=0.0, northing=0.0, crs="EPSG:26913",
    )


def _spatial(label: str) -> SpatialEvidence:
    return SpatialEvidence(
        geometry_type="polygon",
        crs="EPSG:26913",
        spatial_operation="intersects",
        intersecting_entities=[label],
    )


def _graph(path: str) -> GraphEvidence:
    return GraphEvidence(
        path=path,
        node_ids=["a", "b"],
        relationship_types=["RELATES_TO"],
    )


def _packet(*evidence) -> EvidencePacket:
    return EvidencePacket(
        query_id="q-1",
        query_text="x",
        evidence=list(evidence),
        total_tokens=99,           # arbitrary, gets recomputed on rebuild
        system_prompt_tokens=10,
        remaining_budget=5000,
    )


# ---------------------------------------------------------------------------
# compute_kind_distribution
# ---------------------------------------------------------------------------


def test_compute_kind_distribution_counts_each_kind():
    packet = _packet(
        _doc("A"), _doc("B"),
        _assay("h1"),
        _spatial("zone"),
    )
    dist = compute_kind_distribution(packet)
    assert dist == {"document": 2, "assay": 1, "spatial": 1}


def test_compute_kind_distribution_empty_packet():
    packet = _packet()
    assert compute_kind_distribution(packet) == {}


# ---------------------------------------------------------------------------
# Round-robin mode
# ---------------------------------------------------------------------------


def test_round_robin_interleaves_kinds():
    packet = _packet(
        _doc("A"), _doc("B"), _doc("C"),
        _spatial("zone"),
        _assay("h1"),
    )
    out = apply_source_diversity(packet)
    kinds = [e.kind for e in out.evidence]
    # First pass: document, spatial, assay (the three priority kinds
    # present); subsequent passes fill in remaining documents.
    assert kinds[:3] == ["document", "spatial", "assay"]
    # All five entries kept (no max_total).
    assert len(out.evidence) == 5


def test_round_robin_respects_max_total():
    packet = _packet(
        _doc("A"), _doc("B"), _doc("C"),
        _spatial("zone"),
        _assay("h1"),
    )
    out = apply_source_diversity(packet, max_total=3)
    assert len(out.evidence) == 3
    kinds = [e.kind for e in out.evidence]
    # Diversity priority: we should see document + spatial + assay (one
    # of each) rather than three documents.
    assert set(kinds) == {"document", "spatial", "assay"}


def test_round_robin_preserves_authority_within_kind():
    high = _doc("High", authority_rank=1)
    low = _doc("Low", authority_rank=5)
    # Input order is authority-sorted: High before Low.
    packet = _packet(high, low, _spatial("zone"))
    out = apply_source_diversity(packet)
    doc_titles = [e.document_title for e in out.evidence if e.kind == "document"]
    # High-authority doc still comes before low-authority doc in output.
    assert doc_titles == ["High", "Low"]


def test_round_robin_priority_orders_kinds_on_first_pass():
    """The DEFAULT_KIND_PRIORITY puts document first, spatial second."""
    # Build with kinds out of priority order to verify the rerank
    # is doing the work (not just preserving input order).
    packet = _packet(
        _graph("a-b"),     # graph (low priority, last in DEFAULT)
        _spatial("zone"),  # spatial (second in DEFAULT)
        _doc("A"),         # document (first in DEFAULT)
    )
    out = apply_source_diversity(packet)
    kinds = [e.kind for e in out.evidence]
    assert kinds == ["document", "spatial", "graph"]


def test_round_robin_custom_priority():
    packet = _packet(
        _doc("A"),
        _spatial("zone"),
        _graph("a-b"),
    )
    # Caller wants graph first.
    out = apply_source_diversity(
        packet, kind_priority=("graph", "spatial", "document"),
    )
    kinds = [e.kind for e in out.evidence]
    assert kinds == ["graph", "spatial", "document"]


def test_round_robin_handles_extra_kinds_outside_priority():
    """An evidence kind not in kind_priority still gets included — after
    the priority kinds have been drained, in alphabetical order."""
    packet = _packet(
        _doc("A"),
        _collar("h1"),  # 'collar' IS in DEFAULT priority but late
        _spatial("zone"),
    )
    out = apply_source_diversity(
        packet,
        kind_priority=("document", "spatial"),  # collar not listed
    )
    # First pass: document + spatial; second pass: collar (the extra).
    kinds = [e.kind for e in out.evidence]
    assert kinds == ["document", "spatial", "collar"]


def test_round_robin_empty_packet_unchanged():
    packet = _packet()
    out = apply_source_diversity(packet)
    assert out is packet  # no-op shortcut


def test_round_robin_negative_max_total_returns_empty():
    packet = _packet(_doc("A"), _spatial("zone"))
    out = apply_source_diversity(packet, max_total=0)
    assert out.evidence == []


def test_round_robin_recomputes_token_budget():
    """Trimming evidence must shrink total_tokens and free up budget."""
    packet = _packet(_doc("A"), _doc("B"), _doc("C"))
    # Capture baseline.
    original_total = packet.total_tokens
    original_remaining = packet.remaining_budget
    out = apply_source_diversity(packet, max_total=1)
    assert len(out.evidence) == 1
    # New total_tokens reflects ONE doc's footprint, not three.
    assert out.total_tokens < original_total
    # Freed budget = (original_total - new_total) added to remaining.
    assert out.remaining_budget == original_remaining + (
        original_total - out.total_tokens
    )


def test_round_robin_pure_function_does_not_mutate_input():
    packet = _packet(_doc("A"), _doc("B"), _spatial("zone"))
    before = [e.evidence_id for e in packet.evidence]
    _ = apply_source_diversity(packet, max_total=2)
    after = [e.evidence_id for e in packet.evidence]
    assert before == after


def test_round_robin_no_membership_change_is_a_noop():
    """If max_total is ≥ packet size AND the input is already in
    priority order, the output IS the input (identity shortcut)."""
    packet = _packet(_doc("A"), _spatial("zone"), _assay("h1"))
    out = apply_source_diversity(packet)
    # We don't require object identity strictly, but the evidence list
    # contents should be identical AND in the same order.
    assert [e.evidence_id for e in out.evidence] == [
        e.evidence_id for e in packet.evidence
    ]


# ---------------------------------------------------------------------------
# Quota mode
# ---------------------------------------------------------------------------


def test_quota_caps_per_kind():
    packet = _packet(
        _doc("A"), _doc("B"), _doc("C"), _doc("D"),
        _spatial("zone1"), _spatial("zone2"),
        _assay("h1"), _assay("h2"),
    )
    out = apply_source_diversity(
        packet,
        kind_quotas={"document": 2, "spatial": 1, "assay": 1},
    )
    # 2 doc + 1 spatial + 1 assay = 4 entries total.
    assert len(out.evidence) == 4
    dist = compute_kind_distribution(out)
    assert dist == {"document": 2, "spatial": 1, "assay": 1}


def test_quota_unspecified_kind_dropped_by_default():
    packet = _packet(
        _doc("A"),
        _graph("a-b"),  # NOT named in kind_quotas
    )
    out = apply_source_diversity(packet, kind_quotas={"document": 5})
    assert [e.kind for e in out.evidence] == ["document"]


def test_quota_unspecified_kind_kept_when_unspecified_quota_set():
    packet = _packet(
        _doc("A"),
        _graph("a-b"),
    )
    out = apply_source_diversity(
        packet,
        kind_quotas={"document": 5},
        unspecified_quota=2,
    )
    assert [e.kind for e in out.evidence] == ["document", "graph"]


def test_quota_preserves_authority_within_kind():
    high = _doc("High", authority_rank=1)
    low = _doc("Low", authority_rank=5)
    packet = _packet(high, low)
    out = apply_source_diversity(packet, kind_quotas={"document": 1})
    # Quota=1 keeps the FIRST (highest-authority) document only.
    assert [e.document_title for e in out.evidence] == ["High"]


def test_quota_zero_drops_the_kind():
    packet = _packet(_doc("A"), _spatial("zone"))
    out = apply_source_diversity(
        packet, kind_quotas={"document": 1, "spatial": 0},
    )
    assert [e.kind for e in out.evidence] == ["document"]


def test_quota_max_total_caps_after_quotas():
    packet = _packet(
        _doc("A"), _doc("B"), _doc("C"),
        _spatial("z1"), _spatial("z2"),
        _assay("h1"),
    )
    out = apply_source_diversity(
        packet,
        kind_quotas={"document": 3, "spatial": 2, "assay": 1},
        max_total=4,
    )
    # Quotas would yield 6; max_total trims to 4. Priority order takes
    # document first → all 3 docs + 1 spatial.
    assert len(out.evidence) == 4
    dist = compute_kind_distribution(out)
    assert dist["document"] == 3
    assert dist["spatial"] == 1


def test_quota_default_priority_orders_known_kinds_first():
    packet = _packet(
        _graph("a-b"),
        _spatial("zone"),
        _doc("A"),
    )
    out = apply_source_diversity(
        packet,
        kind_quotas={"document": 1, "spatial": 1, "graph": 1},
    )
    # Output order follows DEFAULT_KIND_PRIORITY for known kinds.
    assert [e.kind for e in out.evidence] == ["document", "spatial", "graph"]


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_default_priority_lists_all_six_kinds():
    """Regression — if a new kind gets added to EvidenceUnion the
    priority list must be updated alongside it OR the new kind sinks
    to the alphabetical extras bucket (acceptable but worth noticing
    when this assertion fails)."""
    assert set(DEFAULT_KIND_PRIORITY) == {
        "document", "table", "assay", "collar", "spatial", "graph",
    }


@pytest.mark.parametrize("max_total", [1, 2, 3, 4, 5, 10])
def test_round_robin_always_respects_max_total(max_total):
    packet = _packet(
        _doc("A"), _doc("B"),
        _spatial("z"),
        _assay("h"),
        _graph("g"),
    )
    out = apply_source_diversity(packet, max_total=max_total)
    assert len(out.evidence) == min(max_total, len(packet.evidence))
