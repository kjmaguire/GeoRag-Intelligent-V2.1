"""Unit tests for plan §5b golden-query harness foundation."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.agent.evidence import (
    AssayEvidence,
    DocumentEvidence,
    EvidencePacket,
    SpatialEvidence,
)
from app.agent.golden_query_harness import (
    EvaluationCriterion,
    GoldenQuery,
    evaluate_packet,
    load_golden_queries,
    run_golden_harness,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc(
    title: str,
    *,
    document_type: str = "NI 43-101",
    authority_rank: int = 1,
) -> DocumentEvidence:
    return DocumentEvidence(
        document_id=title.lower(),
        document_title=title,
        document_type=document_type,
        authority_rank=authority_rank,
        is_current=True,
        confidence=1.0,
        page=1,
        chunk_id=f"chunk-{title.lower()}",
        text="x",
        char_start=0,
        char_end=1,
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
        intersecting_entities=["zone"],
    )


def _packet(evidence, *, remaining_budget: int = 100) -> EvidencePacket:
    return EvidencePacket(
        query_id="q-1",
        query_text="x",
        evidence=evidence,
        total_tokens=10,
        system_prompt_tokens=0,
        remaining_budget=remaining_budget,
    )


# ---------------------------------------------------------------------------
# Criterion: contains_kind
# ---------------------------------------------------------------------------


def test_contains_kind_passes_when_kind_present():
    packet = _packet([_doc("A"), _spatial()])
    c = EvaluationCriterion(kind="contains_kind", value="spatial")
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is True


def test_contains_kind_fails_when_kind_missing():
    packet = _packet([_doc("A")])
    c = EvaluationCriterion(kind="contains_kind", value="graph")
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is False
    assert "graph" in result.results[0].message


# ---------------------------------------------------------------------------
# Criterion: min_kind_count
# ---------------------------------------------------------------------------


def test_min_kind_count_dict_passes_when_thresholds_met():
    packet = _packet([_doc("A"), _doc("B"), _spatial()])
    c = EvaluationCriterion(
        kind="min_kind_count",
        value={"document": 2, "spatial": 1},
    )
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is True


def test_min_kind_count_dict_fails_when_one_threshold_missed():
    packet = _packet([_doc("A"), _spatial()])
    c = EvaluationCriterion(
        kind="min_kind_count",
        value={"document": 2, "spatial": 1},  # document threshold = 2, got 1
    )
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is False
    assert "document" in result.results[0].message


def test_min_kind_count_tuple_shape():
    packet = _packet([_doc("A"), _doc("B"), _doc("C")])
    c = EvaluationCriterion(kind="min_kind_count", value=("document", 3))
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is True


# ---------------------------------------------------------------------------
# Criterion: max_kind_count
# ---------------------------------------------------------------------------


def test_max_kind_count_dict_passes_when_thresholds_respected():
    packet = _packet([_doc("A"), _doc("B"), _spatial()])
    c = EvaluationCriterion(
        kind="max_kind_count",
        value={"document": 5, "spatial": 2},
    )
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is True


def test_max_kind_count_fails_when_exceeded():
    packet = _packet([_doc("A"), _doc("B"), _doc("C")])
    c = EvaluationCriterion(kind="max_kind_count", value=("document", 2))
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is False


# ---------------------------------------------------------------------------
# Criterion: exact_kinds
# ---------------------------------------------------------------------------


def test_exact_kinds_passes_with_matching_set():
    packet = _packet([_doc("A"), _spatial()])
    c = EvaluationCriterion(kind="exact_kinds", value=["document", "spatial"])
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is True


def test_exact_kinds_fails_when_extra_kind_present():
    packet = _packet([_doc("A"), _spatial(), _assay()])
    c = EvaluationCriterion(kind="exact_kinds", value=["document", "spatial"])
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is False


# ---------------------------------------------------------------------------
# Criterion: first_kind_is
# ---------------------------------------------------------------------------


def test_first_kind_is_passes_when_first_matches():
    packet = _packet([_doc("A"), _assay()])
    c = EvaluationCriterion(kind="first_kind_is", value="document")
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is True


def test_first_kind_is_fails_when_empty():
    packet = _packet([])
    c = EvaluationCriterion(kind="first_kind_is", value="document")
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is False


# ---------------------------------------------------------------------------
# Criterion: first_document_type_matches
# ---------------------------------------------------------------------------


def test_first_document_type_matches_case_insensitive_substring():
    packet = _packet([_doc("A", document_type="NI 43-101 Technical Report")])
    c = EvaluationCriterion(
        kind="first_document_type_matches",
        value="ni 43-101",
    )
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is True


def test_first_document_type_matches_fails_when_no_documents():
    packet = _packet([_spatial()])
    c = EvaluationCriterion(
        kind="first_document_type_matches", value="NI 43-101",
    )
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is False
    assert "no DocumentEvidence" in result.results[0].message


# ---------------------------------------------------------------------------
# Criterion: min/max evidence_total
# ---------------------------------------------------------------------------


def test_min_evidence_total_passes_when_packet_large_enough():
    packet = _packet([_doc("A"), _doc("B"), _doc("C")])
    c = EvaluationCriterion(kind="min_evidence_total", value=2)
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is True


def test_max_evidence_total_fails_when_packet_too_large():
    packet = _packet([_doc("A"), _doc("B"), _doc("C")])
    c = EvaluationCriterion(kind="max_evidence_total", value=2)
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is False


# ---------------------------------------------------------------------------
# Criterion: budget_reached
# ---------------------------------------------------------------------------


def test_budget_reached_passes_when_remaining_non_negative():
    packet = _packet([_doc("A")], remaining_budget=500)
    c = EvaluationCriterion(kind="budget_reached", value=True)
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is True


def test_budget_reached_fails_when_remaining_negative():
    packet = _packet([_doc("A")], remaining_budget=-50)
    c = EvaluationCriterion(kind="budget_reached", value=True)
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is False


# ---------------------------------------------------------------------------
# Criterion: first_authority_rank_le
# ---------------------------------------------------------------------------


def test_first_authority_rank_le_passes_when_top_doc_is_high_authority():
    packet = _packet([_doc("A", authority_rank=1)])
    c = EvaluationCriterion(kind="first_authority_rank_le", value=2)
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is True


def test_first_authority_rank_le_fails_when_top_doc_is_low():
    packet = _packet([_doc("A", authority_rank=5)])
    c = EvaluationCriterion(kind="first_authority_rank_le", value=2)
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is False


# ---------------------------------------------------------------------------
# Criterion: evidence_id_present
# ---------------------------------------------------------------------------


def test_evidence_id_present_passes_when_id_in_packet():
    doc = _doc("A")
    packet = _packet([doc])
    c = EvaluationCriterion(
        kind="evidence_id_present", value=doc.evidence_id,
    )
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is True


def test_evidence_id_present_fails_when_id_missing():
    packet = _packet([_doc("A")])
    c = EvaluationCriterion(kind="evidence_id_present", value="not-real-id")
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is False


# ---------------------------------------------------------------------------
# Unknown criterion
# ---------------------------------------------------------------------------


def test_unknown_criterion_kind_returns_failed_result():
    """Forward-compat: an unknown criterion kind fails with a helpful
    message instead of raising."""
    packet = _packet([_doc("A")])
    c = EvaluationCriterion(kind="totally_made_up", value=1)  # type: ignore[arg-type]
    result = evaluate_packet(GoldenQuery("q", "x", criteria=(c,)), packet)
    assert result.passed is False
    assert "unknown criterion kind" in result.results[0].message


# ---------------------------------------------------------------------------
# QueryEvaluation + EvaluationReport aggregation
# ---------------------------------------------------------------------------


def test_query_evaluation_passed_requires_all_criteria_pass():
    packet = _packet([_doc("A")])
    golden = GoldenQuery(
        "q", "x",
        criteria=(
            EvaluationCriterion(kind="contains_kind", value="document"),
            EvaluationCriterion(kind="contains_kind", value="spatial"),  # fails
        ),
    )
    ev = evaluate_packet(golden, packet)
    assert ev.passed is False
    assert len(ev.failed_criteria) == 1
    assert ev.failed_criteria[0].criterion.value == "spatial"


def test_query_evaluation_packet_summary_carries_metadata():
    packet = _packet([_doc("A"), _spatial()], remaining_budget=1234)
    golden = GoldenQuery("q", "x", criteria=())
    ev = evaluate_packet(golden, packet)
    assert ev.packet_summary["evidence_total"] == 2
    assert "document" in ev.packet_summary["kinds"]
    assert ev.packet_summary["remaining_budget"] == 1234


def test_evaluation_report_aggregates_pass_rate():
    packet_pass = _packet([_doc("A")])
    packet_fail = _packet([_spatial()])

    def factory(golden: GoldenQuery) -> EvidencePacket:
        return packet_pass if golden.query_id == "G1" else packet_fail

    queries = [
        GoldenQuery(
            "G1", "needs doc",
            criteria=(EvaluationCriterion(kind="contains_kind", value="document"),),
        ),
        GoldenQuery(
            "G2", "needs doc but has spatial",
            criteria=(EvaluationCriterion(kind="contains_kind", value="document"),),
        ),
        GoldenQuery(
            "G3", "needs spatial",
            criteria=(EvaluationCriterion(kind="contains_kind", value="spatial"),),
        ),
    ]
    report = run_golden_harness(queries, factory)
    assert report.total == 3
    assert report.passed_count == 2  # G1 and G3
    assert report.failed_count == 1  # G2
    assert abs(report.pass_rate - (2 / 3)) < 1e-9
    assert "2/3 passed" in report.summary()


def test_evaluation_report_failed_queries_returns_only_failures():
    packet = _packet([_doc("A")])
    golden_pass = GoldenQuery(
        "G1", "x",
        criteria=(EvaluationCriterion(kind="contains_kind", value="document"),),
    )
    golden_fail = GoldenQuery(
        "G2", "x",
        criteria=(EvaluationCriterion(kind="contains_kind", value="spatial"),),
    )
    report = run_golden_harness([golden_pass, golden_fail], lambda g: packet)
    failed = report.failed_queries()
    assert len(failed) == 1
    assert failed[0].golden.query_id == "G2"


# ---------------------------------------------------------------------------
# Empty / skip handling
# ---------------------------------------------------------------------------


def test_empty_queries_report_pass_rate_is_one():
    """With no queries, pass_rate defaults to 1.0 (vacuous truth) so
    a CI gate of pass_rate >= 0.9 doesn't false-fire on an empty run."""
    report = run_golden_harness([], lambda g: None)
    assert report.total == 0
    assert report.pass_rate == 1.0


def test_packet_factory_returning_none_skips_by_default():
    queries = [
        GoldenQuery("G1", "x", criteria=(
            EvaluationCriterion(kind="contains_kind", value="document"),
        )),
    ]
    report = run_golden_harness(queries, lambda g: None)
    # Skipped → not counted as pass OR fail.
    assert report.total == 0


def test_packet_factory_none_can_be_treated_as_fail():
    queries = [
        GoldenQuery("G1", "x", criteria=(
            EvaluationCriterion(kind="contains_kind", value="document"),
        )),
    ]
    report = run_golden_harness(queries, lambda g: None, skip_when_no_packet=False)
    assert report.total == 1
    assert report.failed_count == 1
    failed_msg = report.evaluations[0].results[0].message
    assert "None" in failed_msg


# ---------------------------------------------------------------------------
# JSON loader
# ---------------------------------------------------------------------------


def test_load_golden_queries_round_trips_basic_shape():
    payload = [
        {
            "query_id": "Q1",
            "query_text": "deepest hole?",
            "intent": "factual_lookup",
            "criteria": [
                {"kind": "contains_kind", "value": "document"},
                {"kind": "min_kind_count", "value": {"document": 1}},
            ],
            "tags": ["regression", "factual"],
        },
    ]
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(payload, f)
        path = f.name

    try:
        loaded = load_golden_queries(path)
        assert len(loaded) == 1
        q = loaded[0]
        assert q.query_id == "Q1"
        assert q.query_text == "deepest hole?"
        assert q.intent == "factual_lookup"
        assert q.tags == ("regression", "factual")
        assert len(q.criteria) == 2
        assert q.criteria[0].kind == "contains_kind"
        assert q.criteria[0].value == "document"
    finally:
        Path(path).unlink()


def test_load_golden_queries_rejects_non_array():
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump({"not": "a list"}, f)
        path = f.name
    try:
        with pytest.raises(ValueError, match="must be a JSON array"):
            load_golden_queries(path)
    finally:
        Path(path).unlink()


def test_load_golden_queries_skips_malformed_entries():
    payload = [
        {"query_id": "Good", "query_text": "ok", "criteria": []},
        "not a dict",  # skipped
        {"query_id": "MissingText"},  # skipped (no query_text)
        {
            "query_id": "GoodAgain",
            "query_text": "x",
            "criteria": [
                {"kind": "contains_kind", "value": "document"},
                {"no_kind": "skip me"},  # malformed criterion, skipped
            ],
        },
    ]
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(payload, f)
        path = f.name
    try:
        loaded = load_golden_queries(path)
        ids = [q.query_id for q in loaded]
        assert ids == ["Good", "GoodAgain"]
        # Malformed criterion in "GoodAgain" was skipped — only the
        # valid one survives.
        assert len(loaded[1].criteria) == 1
    finally:
        Path(path).unlink()


# ---------------------------------------------------------------------------
# End-to-end: harness composed with prepare_evidence_for_intent
# ---------------------------------------------------------------------------


def test_harness_composes_with_prepare_evidence_for_intent():
    """Realistic smoke test: run a golden query through the actual
    prepare_evidence_for_intent pipeline."""
    from app.agent.context_prep import prepare_evidence_for_intent

    # Input packet: a mix that mirrors a real synthesis query —
    # 4 documents, 2 spatial, 1 assay, 1 graph.
    input_packet = _packet([
        _doc("D1", document_type="NI 43-101", authority_rank=1),
        _doc("D2", document_type="Press Release", authority_rank=3),
        _doc("D3", document_type="Internal Memo", authority_rank=5),
        _doc("D4", document_type="Assessment Report", authority_rank=2),
        _spatial(),
        _spatial(),
        _assay(),
    ], remaining_budget=10_000)

    golden = GoldenQuery(
        query_id="SYNTH_1",
        query_text="Integrate the corridor evidence",
        intent="synthesis",
        criteria=(
            EvaluationCriterion(kind="contains_kind", value="document"),
            EvaluationCriterion(kind="contains_kind", value="spatial"),
            EvaluationCriterion(kind="first_kind_is", value="document"),
            EvaluationCriterion(
                kind="first_document_type_matches", value="NI 43-101",
            ),
            EvaluationCriterion(kind="min_kind_count", value=("document", 2)),
            EvaluationCriterion(kind="budget_reached", value=True),
        ),
    )

    def factory(g: GoldenQuery):
        prepared = prepare_evidence_for_intent(input_packet, g.intent)
        return prepared.packet

    report = run_golden_harness([golden], factory)
    # All six criteria should pass against the synthesis pipeline.
    assert report.passed_count == 1, (
        f"failed criteria: {report.evaluations[0].failed_criteria}"
    )
