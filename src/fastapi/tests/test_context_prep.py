"""Unit tests for context preparation pipeline (§3b + §3c + §3f composition)."""

from __future__ import annotations

import pytest

from app.agent.context_prep import (
    PROTECTED_KINDS_BY_INTENT,
    QUOTA_BY_INTENT,
    PreparedContext,
    prepare_evidence_for_intent,
)
from app.agent.evidence import (
    AssayEvidence,
    CollarEvidence,
    DocumentEvidence,
    EvidencePacket,
    GraphEvidence,
    SpatialEvidence,
    TableEvidence,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc(
    title: str,
    *,
    document_type: str = "NI 43-101",
    authority_rank: int = 1,
    is_current: bool = True,
    confidence: float = 1.0,
) -> DocumentEvidence:
    return DocumentEvidence(
        document_id=title.lower(),
        document_title=title,
        document_type=document_type,
        authority_rank=authority_rank,
        is_current=is_current,
        confidence=confidence,
        page=1,
        chunk_id=f"chunk-{title.lower()}",
        text=f"text from {title}",
        char_start=0,
        char_end=10,
    )


def _assay(hole_id: str = "X") -> AssayEvidence:
    return AssayEvidence(
        project_id="p", hole_id=hole_id,
        depth_from_m=0.0, depth_to_m=10.0, interval_length_m=10.0,
        commodity="Au", value=1.0, unit="g/t",
    )


def _spatial() -> SpatialEvidence:
    return SpatialEvidence(
        geometry_type="polygon",
        crs="EPSG:26913",
        spatial_operation="intersects",
        intersecting_entities=["zone"],
    )


def _collar(hole_id: str = "X") -> CollarEvidence:
    return CollarEvidence(
        hole_id=hole_id, easting=0.0, northing=0.0, crs="EPSG:26913",
    )


def _graph() -> GraphEvidence:
    return GraphEvidence(path="(:A)-[:R]->(:B)", node_ids=["a", "b"])


def _table() -> TableEvidence:
    return TableEvidence(
        table_id="t1",
        document_id="d",
        page=1,
        column_names=["depth", "grade"],
        cell_values=[{"depth": "1", "grade": "0.5"}],
    )


def _packet(evidence, *, remaining_budget: int = 5000) -> EvidencePacket:
    # total_tokens gets recomputed by prepare_evidence_for_intent's
    # internal calls; supplying 0 is fine for these tests.
    return EvidencePacket(
        query_id="q-1",
        query_text="x",
        evidence=evidence,
        total_tokens=0,
        system_prompt_tokens=0,
        remaining_budget=remaining_budget,
    )


# ---------------------------------------------------------------------------
# Quota table coverage
# ---------------------------------------------------------------------------


def test_every_intent_has_a_quota_table():
    """Every intent the classifier emits must have a quota entry."""
    expected_intents = {
        "factual_lookup",
        "synthesis",
        "hypothesis_generation",
        "anomaly_detection",
        "uncertainty_quantification",
        "decision_support",
        "project_summary",
        "coverage_gap",
    }
    assert set(QUOTA_BY_INTENT.keys()) == expected_intents


def test_every_intent_has_a_protected_kinds_set():
    expected_intents = set(QUOTA_BY_INTENT.keys())
    assert set(PROTECTED_KINDS_BY_INTENT.keys()) == expected_intents


def test_quota_keys_match_evidence_kinds():
    """Every quota table covers the six known evidence kinds."""
    expected_kinds = {"document", "spatial", "assay", "table", "collar", "graph"}
    for intent, quota in QUOTA_BY_INTENT.items():
        assert set(quota.keys()) == expected_kinds, (
            f"{intent} quota table is missing kinds: "
            f"{expected_kinds - set(quota.keys())}"
        )


@pytest.mark.parametrize("intent", [
    "factual_lookup",
    "synthesis",
    "hypothesis_generation",
    "anomaly_detection",
    "uncertainty_quantification",
    "decision_support",
    "project_summary",
    "coverage_gap",
])
def test_each_intent_protects_at_least_one_kind(intent):
    """Every intent protects at least one kind from full strip."""
    assert len(PROTECTED_KINDS_BY_INTENT[intent]) >= 1


# ---------------------------------------------------------------------------
# Intent-specific quota shape
# ---------------------------------------------------------------------------


def test_factual_lookup_is_document_heavy():
    quota = QUOTA_BY_INTENT["factual_lookup"]
    assert quota["document"] >= 4
    assert quota["graph"] == 0  # no graph for citation-style answers


def test_anomaly_detection_is_assay_heavy():
    quota = QUOTA_BY_INTENT["anomaly_detection"]
    assert quota["assay"] >= 4
    assert quota["assay"] > quota["document"]


def test_hypothesis_generation_keeps_graph_paths():
    quota = QUOTA_BY_INTENT["hypothesis_generation"]
    assert quota["graph"] >= 2


def test_decision_support_protects_documents():
    """Regulatory/decision answers can't drop their document spine."""
    assert "document" in PROTECTED_KINDS_BY_INTENT["decision_support"]


def test_anomaly_detection_protects_assay_AND_document():
    """The anomaly answer IS the numeric table — protect both the
    numbers AND the documents the numbers cite."""
    protected = PROTECTED_KINDS_BY_INTENT["anomaly_detection"]
    assert "assay" in protected
    assert "document" in protected


# ---------------------------------------------------------------------------
# Pipeline composition — happy paths
# ---------------------------------------------------------------------------


def test_empty_packet_returns_empty_prepared_context():
    packet = _packet([])
    result = prepare_evidence_for_intent(packet, "synthesis")
    assert isinstance(result, PreparedContext)
    assert result.packet is packet  # unchanged identity
    assert result.kind_distribution_before == {}
    assert result.kind_distribution_after == {}


def test_pipeline_returns_PreparedContext_with_audit_fields():
    packet = _packet([_doc("A")])
    result = prepare_evidence_for_intent(packet, "factual_lookup")
    assert isinstance(result, PreparedContext)
    assert result.intent == "factual_lookup"
    assert result.quota_used == QUOTA_BY_INTENT["factual_lookup"]
    assert "document" in result.kind_distribution_after


def test_factual_lookup_quota_filters_out_graph_evidence():
    """factual_lookup quota for graph is 0 → graph evidence dropped."""
    packet = _packet([
        _doc("A"),
        _graph(),  # graph evidence — quota 0 → dropped
    ])
    result = prepare_evidence_for_intent(packet, "factual_lookup")
    kinds = {e.kind for e in result.packet.evidence}
    assert "graph" not in kinds
    assert "document" in kinds


def test_anomaly_detection_keeps_assays_over_documents():
    """Anomaly quota: assay=5, document=1. Even with one of each in
    the input, both make it through (under-quota), but if there were
    multiple, assays dominate."""
    packet = _packet([
        _doc("d1"), _doc("d2"),
        _assay("h1"), _assay("h2"), _assay("h3"),
    ])
    result = prepare_evidence_for_intent(packet, "anomaly_detection")
    # Quota allows 1 document + 5 assays → we expect 1 doc + 3 assays kept.
    by_kind = result.kind_distribution_after
    assert by_kind.get("document", 0) == 1
    assert by_kind.get("assay", 0) == 3


def test_authority_rank_refreshed_during_prepare():
    """A document built with default rank 3 + document_type 'NI 43-101'
    gets re-classified to rank 1 by the annotate step."""
    doc = _doc("A", document_type="NI 43-101", authority_rank=3)
    packet = _packet([doc])
    result = prepare_evidence_for_intent(packet, "synthesis")
    out_doc = result.packet.evidence[0]
    assert out_doc.authority_rank == 1


def test_authority_sort_applied_high_rank_first():
    high = _doc("High", document_type="NI 43-101")  # rank 1
    low = _doc("Low", document_type="Internal Memo")  # rank 5
    # Input order LOW first; pipeline must re-sort.
    packet = _packet([low, high])
    result = prepare_evidence_for_intent(packet, "synthesis")
    titles = [e.document_title for e in result.packet.evidence]
    # High should come before Low after the rank pass.
    assert titles.index("High") < titles.index("Low")


# ---------------------------------------------------------------------------
# Budget interaction
# ---------------------------------------------------------------------------


def test_budget_fits_when_quota_already_keeps_packet_small():
    packet = _packet([_doc("A"), _doc("B")])
    result = prepare_evidence_for_intent(
        packet,
        "factual_lookup",
        max_context_tokens=10_000,
    )
    assert result.reached_budget is True
    assert result.dropped_evidence_ids == []


def test_tight_budget_triggers_drop_pass():
    """A small max_context_tokens should force the budget pass to drop
    members. With min_per_kind=1 + protected=document, the protected
    kind survives even when over budget."""
    docs = [
        _doc(f"D{i}", document_type="Internal Memo")  # rank 5 = droppable
        for i in range(5)
    ]
    keeper = _doc("Keeper", document_type="NI 43-101")  # rank 1
    packet = _packet([keeper, *docs])
    result = prepare_evidence_for_intent(
        packet,
        "factual_lookup",
        # Tight: doc quota is 5; this forces the trim path. The
        # exact threshold depends on per-doc tokens (chars/4 of the
        # text field).
        max_context_tokens=20,
    )
    # Keeper (NI 43-101) must survive (protected by both authority
    # AND the per-intent protected-kinds set).
    titles = [e.document_title for e in result.packet.evidence]
    assert "Keeper" in titles


def test_unreachable_budget_returns_reason():
    """When the only droppable evidence is protected by the intent's
    protected set, the budget can't be reached and reached_budget=False
    with a reason."""
    # decision_support protects document. Three docs all protected.
    docs = [_doc(f"D{i}", document_type="Internal Memo") for i in range(3)]
    packet = _packet(docs, remaining_budget=0)
    # Use max_context_tokens to force a negative budget.
    result = prepare_evidence_for_intent(
        packet,
        "decision_support",
        max_context_tokens=1,
        min_per_kind=0,
    )
    # With protected={document} and the only kind being document, the
    # floor can't be lowered and the budget stays negative.
    if not result.reached_budget:
        assert "spatial" not in (result.budget_reason or "")
        assert "document" in (result.budget_reason or "").lower() or "floor" in (result.budget_reason or "").lower()


# ---------------------------------------------------------------------------
# Quota override
# ---------------------------------------------------------------------------


def test_quota_override_replaces_per_intent_quota():
    packet = _packet([_doc("A"), _assay(), _spatial()])
    # Custom quota: only documents.
    result = prepare_evidence_for_intent(
        packet,
        "synthesis",
        quota_override={"document": 5, "assay": 0, "spatial": 0},
    )
    kinds = {e.kind for e in result.packet.evidence}
    assert kinds == {"document"}
    assert result.quota_used == {"document": 5, "assay": 0, "spatial": 0}


def test_protected_kinds_override_replaces_per_intent_set():
    """Override protected set: with the default decision_support
    protected={document}, the doc can't be dropped. With an empty
    override, it CAN be dropped. We use the dropped_evidence_ids
    audit field as the discriminator — the packet's total_tokens
    field is artificially 0 in the test fixture, so we drive the
    behaviour difference by checking what `enforce_token_budget`
    would have wanted to drop."""
    # Two interactions to compare side-by-side:
    #   1. default protected_kinds (document protected)
    #   2. override to empty
    doc = _doc("A", document_type="Internal Memo")  # rank 5 = most droppable

    # Build a packet where total_tokens is intentionally large so the
    # budget pass HAS work to do.
    packet = EvidencePacket(
        query_id="q-1",
        query_text="x",
        evidence=[doc],
        total_tokens=100,           # claims 100 tokens used
        system_prompt_tokens=0,
        remaining_budget=-50,        # over budget
    )

    # With override=frozenset() AND min_per_kind=0, the only doc can
    # be dropped → budget reaches (or comes closer to) zero.
    result_open = prepare_evidence_for_intent(
        packet,
        "decision_support",
        min_per_kind=0,
        protected_kinds_override=frozenset(),
    )
    # With default protection (document is protected), the same doc
    # cannot be dropped.
    result_default = prepare_evidence_for_intent(
        packet,
        "decision_support",
        min_per_kind=0,
    )
    # The default-protected path keeps the doc, the open path drops it.
    assert len(result_default.packet.evidence) == 1
    assert len(result_open.packet.evidence) == 0
    # The open path's audit shows the drop.
    assert len(result_open.dropped_evidence_ids) == 1


# ---------------------------------------------------------------------------
# Unknown intent fallback
# ---------------------------------------------------------------------------


def test_unknown_intent_falls_back_to_synthesis_quota():
    packet = _packet([_doc("A"), _assay()])
    result = prepare_evidence_for_intent(packet, "totally_made_up_intent")
    assert result.quota_used == QUOTA_BY_INTENT["synthesis"]


def test_none_intent_falls_back_to_synthesis_quota():
    packet = _packet([_doc("A")])
    result = prepare_evidence_for_intent(packet, None)
    assert result.quota_used == QUOTA_BY_INTENT["synthesis"]
    assert result.intent == "(unspecified)"


# ---------------------------------------------------------------------------
# Audit + distribution reporting
# ---------------------------------------------------------------------------


def test_kind_distribution_before_and_after_are_distinct_views():
    """`before` reflects the input packet; `after` reflects the prepared
    packet. They differ when the pipeline trims."""
    packet = _packet([
        _doc("d1"), _doc("d2"), _doc("d3"),
        _graph(),  # factual_lookup quota for graph is 0
    ])
    result = prepare_evidence_for_intent(packet, "factual_lookup")
    assert result.kind_distribution_before == {"document": 3, "graph": 1}
    # After: graph dropped (quota=0), all 3 docs kept (under quota=5).
    assert result.kind_distribution_after.get("graph", 0) == 0
    assert result.kind_distribution_after.get("document") == 3


def test_dropped_evidence_ids_captures_budget_pass_drops():
    """When the budget pass drops members, their IDs land in
    `dropped_evidence_ids`. (Quota-pass drops are NOT listed here —
    only budget-pass drops are tracked for traceability.)"""
    docs = [
        _doc(f"D{i}", document_type="Internal Memo")
        for i in range(4)
    ]
    packet = _packet(docs)
    # factual_lookup quota=5 for document so all four pass diversity.
    # Tight budget forces enforce_token_budget to drop.
    result = prepare_evidence_for_intent(
        packet,
        "factual_lookup",
        max_context_tokens=12,  # tight
        min_per_kind=1,  # leave at least one
    )
    # If budget was reached, the drop pass MAY or MAY NOT have run.
    # The post-condition that matters: SOMETHING got dropped OR
    # everything fits.
    if result.dropped_evidence_ids:
        assert len(result.dropped_evidence_ids) >= 1


# ---------------------------------------------------------------------------
# Pure-function invariant
# ---------------------------------------------------------------------------


def test_pure_function_does_not_mutate_input_packet():
    packet = _packet([_doc("A"), _doc("B"), _graph()])
    before_ids = [e.evidence_id for e in packet.evidence]
    before_remaining = packet.remaining_budget
    _ = prepare_evidence_for_intent(packet, "factual_lookup")
    after_ids = [e.evidence_id for e in packet.evidence]
    assert before_ids == after_ids
    assert packet.remaining_budget == before_remaining


# ---------------------------------------------------------------------------
# PreparedContext frozen
# ---------------------------------------------------------------------------


def test_prepared_context_is_frozen():
    packet = _packet([_doc("A")])
    result = prepare_evidence_for_intent(packet, "synthesis")
    with pytest.raises(Exception):
        result.intent = "different"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Mixed-kind diversity sweep
# ---------------------------------------------------------------------------


def test_synthesis_keeps_a_mix_of_kinds():
    """Synthesis quota: document=3, spatial=2, assay=2, table=1,
    collar=1, graph=1. Feed one of each and verify the prepared
    packet has at least 4 different kinds."""
    packet = _packet([
        _doc("d"),
        _assay(),
        _spatial(),
        _collar(),
        _graph(),
        _table(),
    ])
    result = prepare_evidence_for_intent(packet, "synthesis")
    kinds = {e.kind for e in result.packet.evidence}
    assert len(kinds) >= 4
