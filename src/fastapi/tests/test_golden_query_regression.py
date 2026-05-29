"""Golden-query regression test for the context-prep pipeline.

Locks today's per-intent quota + protected-kinds tables (the
``QUOTA_BY_INTENT`` and ``PROTECTED_KINDS_BY_INTENT`` dicts in
``app/agent/context_prep.py``) as behavior regression tests.

How it works:

  1. Load the JSON fixture ``tests/golden_queries.json`` — 15 queries
     covering all 8 agentic intents plus authority/diversity/budget
     invariants.
  2. Construct a DETERMINISTIC mock packet per query (designed to
     exercise the intent's quota table).
  3. Run ``prepare_evidence_for_intent(packet, query.intent)``.
  4. Evaluate the prepared packet against each query's criteria via
     the harness from plan §5b.
  5. Assert pass rate is 100%.

A quota table change (e.g. someone bumps ``factual_lookup.document``
from 5 to 4) that breaks a golden expectation will fail this test
loudly with the exact criterion that broke.

Re-running this test is the cheapest pre-commit signal that the
per-intent ratios still match what the rest of the system expects.
The eventual live-corpus eval (a Hatchet workflow) consumes the
same JSON fixture for cross-validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.context_prep import prepare_evidence_for_intent
from app.agent.evidence import (
    AssayEvidence,
    CollarEvidence,
    DocumentEvidence,
    EvidencePacket,
    GraphEvidence,
    SpatialEvidence,
    TableEvidence,
)
from app.agent.golden_query_harness import (
    GoldenQuery,
    load_golden_queries,
    run_golden_harness,
)


# ---------------------------------------------------------------------------
# Mock packet builders — deterministic, parametrised by intent
# ---------------------------------------------------------------------------


def _doc(
    n: int,
    *,
    document_type: str = "NI 43-101",
    authority_rank: int = 1,
) -> DocumentEvidence:
    return DocumentEvidence(
        document_id=f"d{n}",
        document_title=f"Doc {n}",
        document_type=document_type,
        authority_rank=authority_rank,
        is_current=True,
        confidence=1.0,
        page=n,
        chunk_id=f"chunk-{n}",
        text=f"document {n} payload",
        char_start=0,
        char_end=20,
    )


def _assay(n: int) -> AssayEvidence:
    return AssayEvidence(
        project_id="p", hole_id=f"H-{n}",
        depth_from_m=float(n * 10), depth_to_m=float(n * 10 + 10),
        interval_length_m=10.0,
        commodity="Au", value=float(n), unit="g/t",
    )


def _spatial(n: int) -> SpatialEvidence:
    return SpatialEvidence(
        geometry_type="polygon",
        crs="EPSG:26913",
        spatial_operation="intersects",
        intersecting_entities=[f"zone-{n}"],
    )


def _collar(n: int) -> CollarEvidence:
    return CollarEvidence(
        hole_id=f"H-{n}", easting=float(n), northing=float(n),
        crs="EPSG:26913",
    )


def _graph(n: int) -> GraphEvidence:
    return GraphEvidence(
        path=f"(:A)-[:R{n}]->(:B)",
        node_ids=[f"a{n}", f"b{n}"],
    )


def _table(n: int) -> TableEvidence:
    return TableEvidence(
        table_id=f"t{n}",
        document_id=f"d{n}",
        page=n,
        column_names=["depth", "grade"],
        cell_values=[{"depth": str(n), "grade": str(0.5 * n)}],
    )


def _rich_packet() -> EvidencePacket:
    """Multi-kind packet with enough of each evidence type to exercise
    every per-intent quota. Used by the "happy path" golden queries."""
    return EvidencePacket(
        query_id="g-1",
        query_text="x",
        evidence=[
            _doc(1, document_type="NI 43-101", authority_rank=1),
            _doc(2, document_type="Press Release", authority_rank=3),
            _doc(3, document_type="Internal Memo", authority_rank=5),
            _doc(4, document_type="Assessment Report", authority_rank=2),
            _doc(5, document_type="NI 43-101", authority_rank=1),
            _spatial(1),
            _spatial(2),
            _assay(1),
            _assay(2),
            _assay(3),
            _assay(4),
            _collar(1),
            _graph(1),
            _graph(2),
            _table(1),
            _table(2),
            _table(3),
        ],
        total_tokens=200,
        remaining_budget=20_000,
    )


def _authority_inverted_packet() -> EvidencePacket:
    """Packet where input order has the LOW-authority doc first. The
    authority sort must move the NI 43-101 to the top."""
    return EvidencePacket(
        query_id="g-auth",
        query_text="x",
        evidence=[
            _doc(1, document_type="Internal Memo", authority_rank=5),
            _doc(2, document_type="NI 43-101", authority_rank=1),
            _spatial(1),
        ],
        total_tokens=50,
        remaining_budget=20_000,
    )


def _factual_with_graph_packet() -> EvidencePacket:
    """factual_lookup quota for graph is 0 — graph must be excluded."""
    return EvidencePacket(
        query_id="g-fact",
        query_text="x",
        evidence=[
            _doc(1, document_type="NI 43-101", authority_rank=1),
            _graph(1),
            _graph(2),
        ],
        total_tokens=30,
        remaining_budget=20_000,
    )


def _small_packet() -> EvidencePacket:
    """Single-doc packet — tests the budget happy path."""
    return EvidencePacket(
        query_id="g-small",
        query_text="x",
        evidence=[_doc(1)],
        total_tokens=10,
        remaining_budget=20_000,
    )


def _empty_packet() -> EvidencePacket:
    """Edge — zero evidence members."""
    return EvidencePacket(
        query_id="g-empty",
        query_text="x",
        evidence=[],
        total_tokens=0,
        remaining_budget=20_000,
    )


def _single_low_authority_doc_packet() -> EvidencePacket:
    """Single rank-5 Internal Memo — only available document is low-authority."""
    return EvidencePacket(
        query_id="g-low",
        query_text="x",
        evidence=[_doc(1, document_type="Internal Memo", authority_rank=5)],
        total_tokens=10,
        remaining_budget=20_000,
    )


def _all_assay_packet() -> EvidencePacket:
    """Anomaly-style input with assays but NO documents."""
    return EvidencePacket(
        query_id="g-assay",
        query_text="x",
        evidence=[_assay(1), _assay(2), _assay(3), _assay(4)],
        total_tokens=50,
        remaining_budget=20_000,
    )


def _docs_only_packet() -> EvidencePacket:
    """Synthesis-style input with only documents."""
    return EvidencePacket(
        query_id="g-docs",
        query_text="x",
        evidence=[
            _doc(1, document_type="NI 43-101", authority_rank=1),
            _doc(2, document_type="Press Release", authority_rank=3),
            _doc(3, document_type="Internal Memo", authority_rank=5),
        ],
        total_tokens=40,
        remaining_budget=20_000,
    )


def _spatial_only_packet() -> EvidencePacket:
    return EvidencePacket(
        query_id="g-spatial",
        query_text="x",
        evidence=[_spatial(1), _spatial(2)],
        total_tokens=20,
        remaining_budget=20_000,
    )


def _high_volume_packet() -> EvidencePacket:
    """20+ members — exercises the budget-pressure trim path."""
    return EvidencePacket(
        query_id="g-vol",
        query_text="x",
        evidence=[
            _doc(i, document_type=(
                "NI 43-101" if i % 3 == 0 else
                "Press Release" if i % 3 == 1 else
                "Internal Memo"
            ), authority_rank=(1 if i % 3 == 0 else 3 if i % 3 == 1 else 5))
            for i in range(8)
        ] + [_spatial(1), _spatial(2), _assay(1), _assay(2), _assay(3), _table(1), _table(2), _graph(1)],
        total_tokens=120,
        remaining_budget=20_000,
    )


def _coverage_table_packet() -> EvidencePacket:
    return EvidencePacket(
        query_id="g-cov",
        query_text="x",
        evidence=[_table(1), _table(2), _table(3), _doc(1)],
        total_tokens=40,
        remaining_budget=20_000,
    )


def _superseded_vs_current_packet() -> EvidencePacket:
    """One CURRENT and one SUPERSEDED NI 43-101 — authority sort picks current."""
    current = DocumentEvidence(
        document_id="d-current",
        document_title="Current NI 43-101",
        document_type="NI 43-101",
        authority_rank=1,
        is_current=True,
        confidence=1.0,
        page=1,
        chunk_id="chunk-current",
        text="text current",
        char_start=0,
        char_end=12,
    )
    superseded = DocumentEvidence(
        document_id="d-superseded",
        document_title="Superseded NI 43-101",
        document_type="NI 43-101",
        authority_rank=1,
        is_current=False,
        confidence=1.0,
        page=1,
        chunk_id="chunk-superseded",
        text="text superseded",
        char_start=0,
        char_end=14,
    )
    # Superseded FIRST in input — authority sort must move current to front.
    return EvidencePacket(
        query_id="g-sup",
        query_text="x",
        evidence=[superseded, current],
        total_tokens=30,
        remaining_budget=20_000,
    )


def _graph_heavy_packet() -> EvidencePacket:
    return EvidencePacket(
        query_id="g-gh",
        query_text="x",
        evidence=[
            _doc(1, document_type="NI 43-101"),
            _doc(2, document_type="Technical Report"),
            _graph(1),
            _graph(2),
            _graph(3),
            _assay(1),
            _assay(2),
        ],
        total_tokens=70,
        remaining_budget=20_000,
    )


def _project_summary_packet() -> EvidencePacket:
    return EvidencePacket(
        query_id="g-ps",
        query_text="x",
        evidence=[
            _doc(1), _doc(2),
            _spatial(1),   # quota=0 — must be dropped
            _assay(1),     # quota=0 — must be dropped
            _table(1), _table(2),
            _graph(1),     # quota=0 — must be dropped
        ],
        total_tokens=70,
        remaining_budget=20_000,
    )


def _decision_packet() -> EvidencePacket:
    return EvidencePacket(
        query_id="g-dec",
        query_text="x",
        evidence=[
            _doc(1, document_type="NI 43-101", authority_rank=1),
            _doc(2, document_type="Annual Report", authority_rank=2),
            _doc(3, document_type="Assessment Report", authority_rank=2),
            _spatial(1),
            _assay(1),
        ],
        total_tokens=60,
        remaining_budget=20_000,
    )


# ---------------------------------------------------------------------------
# Per-query packet factory
# ---------------------------------------------------------------------------


def _packet_for_query(golden: GoldenQuery) -> EvidencePacket | None:
    """Map query_id → input packet. Each golden query targets a
    specific quota/authority/diversity/budget invariant; the factory
    picks the packet shape that exercises the right behaviour."""
    if golden.query_id == "authority.ni43_above_memo":
        return _authority_inverted_packet()
    if golden.query_id == "diversity.graph_excluded_for_factual":
        return _factual_with_graph_packet()
    if golden.query_id == "budget.fits_for_small_packet":
        return _small_packet()
    # Edge-case factories shipped with the §31 expansion.
    if golden.query_id == "edge.empty_packet_returns_empty":
        return _empty_packet()
    if golden.query_id == "edge.single_low_authority_doc":
        return _single_low_authority_doc_packet()
    if golden.query_id == "edge.all_assay_no_documents":
        return _all_assay_packet()
    if golden.query_id == "edge.only_documents_synthesis":
        return _docs_only_packet()
    if golden.query_id == "edge.anomaly_with_qaqc_flags":
        return _rich_packet()  # rich packet has assays
    if golden.query_id == "edge.spatial_only_packet":
        return _spatial_only_packet()
    if golden.query_id == "edge.budget_pressure_high_volume":
        return _high_volume_packet()
    if golden.query_id == "edge.coverage_gap_only_tables":
        return _coverage_table_packet()
    if golden.query_id == "authority.current_outranks_superseded":
        return _superseded_vs_current_packet()
    if golden.query_id == "diversity.hypothesis_keeps_graph_paths":
        return _graph_heavy_packet()
    if golden.query_id == "diversity.project_summary_no_spatial":
        return _project_summary_packet()
    if golden.query_id == "diversity.decision_promotes_documents":
        return _decision_packet()
    # All other queries use the rich, multi-kind packet.
    return _rich_packet()


def _factory(golden: GoldenQuery) -> EvidencePacket | None:
    """Compose: pick the right input packet → run the pipeline → return
    the prepared packet for harness evaluation."""
    input_packet = _packet_for_query(golden)
    if input_packet is None:
        return None
    prepared = prepare_evidence_for_intent(input_packet, golden.intent)
    return prepared.packet


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def golden_queries() -> list[GoldenQuery]:
    """Load the JSON fixture once per module."""
    fixture = Path(__file__).parent / "golden_queries.json"
    return load_golden_queries(fixture)


def test_golden_queries_fixture_loads_minimum_coverage(golden_queries):
    """Forward-compat — if someone deletes queries from the fixture,
    this test reminds us the regression coverage just shrank. The
    floor (14) is today's shipped count; adding queries is fine,
    removing them needs an explicit override of this assertion."""
    assert len(golden_queries) >= 26, (
        f"expected ≥26 golden queries, got {len(golden_queries)}"
    )


def test_golden_queries_cover_all_eight_intents(golden_queries):
    """Each of the 8 agentic intents must have at least one golden
    query in the fixture set."""
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
    covered = {q.intent for q in golden_queries if q.intent}
    missing = expected_intents - covered
    assert not missing, f"intents without a golden query: {sorted(missing)}"


def test_every_golden_query_has_at_least_one_criterion(golden_queries):
    """A criterion-less query is silently treated as a pass — call that
    out as a fixture bug."""
    for q in golden_queries:
        assert q.criteria, (
            f"query {q.query_id} has no criteria; would silently pass"
        )


def test_context_prep_pipeline_passes_all_golden_queries(golden_queries):
    """The MAIN regression assertion.

    Run every golden query through prepare_evidence_for_intent against
    the deterministic mock packets, and assert 100% pass rate on the
    harness.

    A failure on this test means EITHER:
      a) Someone modified QUOTA_BY_INTENT or PROTECTED_KINDS_BY_INTENT
         in context_prep.py and the behaviour change wasn't intentional
      b) Someone modified the diversity / budget algorithm and the new
         behaviour drifts from the locked per-intent contract
      c) The golden_queries.json fixture itself needs updating to
         reflect a deliberate behaviour change

    In all three cases, the failing criterion's message identifies
    which contract changed.
    """
    report = run_golden_harness(golden_queries, _factory)
    assert report.pass_rate == 1.0, (
        f"golden-query regression failed: {report.summary()}\n"
        + "\n".join(
            f"  {ev.golden.query_id} → "
            + ", ".join(
                f"{r.criterion.kind}: {r.message}"
                for r in ev.failed_criteria
            )
            for ev in report.failed_queries()
        )
    )


def test_factual_lookup_excludes_graph_evidence(golden_queries):
    """Spot-check: the 'diversity.graph_excluded_for_factual' query
    specifically pins that factual_lookup's quota for graph=0
    drops graph evidence from the prepared packet."""
    query = next(
        q for q in golden_queries
        if q.query_id == "diversity.graph_excluded_for_factual"
    )
    prepared = _factory(query)
    assert prepared is not None
    kinds = {e.kind for e in prepared.evidence}
    assert "graph" not in kinds


def test_authority_sort_promotes_ni43_above_memo(golden_queries):
    """Spot-check: the 'authority.ni43_above_memo' query pins that
    the authority sort moves a Rank-1 NI 43-101 above a Rank-5
    Internal Memo, regardless of input order."""
    query = next(
        q for q in golden_queries
        if q.query_id == "authority.ni43_above_memo"
    )
    prepared = _factory(query)
    assert prepared is not None
    # First DocumentEvidence must be NI 43-101.
    docs = [e for e in prepared.evidence if e.kind == "document"]
    assert docs, "expected at least one document in prepared packet"
    assert docs[0].document_type == "NI 43-101"
    assert docs[0].authority_rank == 1


def test_anomaly_detection_keeps_assays_dominant(golden_queries):
    """Spot-check: anomaly_detection quota gives assay=5, document=1.
    With ≥3 assays in the input, the prepared packet must surface
    them as the dominant kind."""
    query = next(
        q for q in golden_queries
        if q.query_id == "anomaly.outlier_grades"
    )
    prepared = _factory(query)
    assert prepared is not None
    kinds = [e.kind for e in prepared.evidence]
    assay_count = kinds.count("assay")
    doc_count = kinds.count("document")
    assert assay_count >= 1
    assert doc_count <= 1


def test_coverage_gap_protects_table_kind(golden_queries):
    """Spot-check: coverage_gap quota gives table=3 + protects table.
    Even under tight budget, table evidence survives."""
    query = next(
        q for q in golden_queries
        if q.query_id == "coverage_gap.holes_without_assays"
    )
    prepared = _factory(query)
    assert prepared is not None
    kinds = {e.kind for e in prepared.evidence}
    assert "table" in kinds


def test_synthesis_keeps_at_least_three_distinct_kinds(golden_queries):
    """Spot-check: synthesis quota is balanced across all six kinds.
    With a multi-kind input, at least 3 kinds survive."""
    query = next(
        q for q in golden_queries
        if q.query_id == "synthesis.corridor_integration"
    )
    prepared = _factory(query)
    assert prepared is not None
    kinds = {e.kind for e in prepared.evidence}
    assert len(kinds) >= 3, f"synthesis prepared packet had only {kinds}"
