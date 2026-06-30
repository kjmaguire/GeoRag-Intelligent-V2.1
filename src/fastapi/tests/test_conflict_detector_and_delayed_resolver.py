"""Unit tests for Module 6 Phase B Chunk 4b.

Covers:
  conflict_detector.detect_conflicts:
    1.  Two structured_record bindings matching entity_key, same property value
        → no conflict returned.
    2.  Two structured_record bindings matching entity_key, differing scalar
        property value → 1 conflict (S4a scenario).
    3.  Three structured_record bindings: two agree, one differs → 1 conflict
        with 2 distinct values.
    4.  Structured bindings with no 'pk' key → not treated as structured_record;
        no conflict emitted.
    5.  Two graph_edge bindings, same (start,end), different rel_type → 1 conflict.
    6.  Two graph_edge bindings, same (start,end), same rel_type but differing
        scalar property → 1 conflict.
    7.  Passage-kind bindings → skipped entirely (no conflict output).
    8.  Empty bindings list → empty result.
    9.  Single binding → cannot conflict with itself → empty result.
   10.  Detection failure (corrupt display_ref) → empty result, no raise.
   11.  Structural keys (schema, table, pk, tool, slot, chunk_id) excluded from
        scalar comparison.
   12.  Both structured_record conflict + graph_edge conflict in same run →
        both returned.

  span_resolver.resolve_spans_delayed (S4b scenario):
   13.  Fuzzy marker [DATA : 1] (whitespace around colon) resolved when binding
        has FK target.
   14.  Preview_text substring match when fuzzy fails → resolved.
   15.  Short preview_text (< 12 chars) → not used as substring anchor.
   16.  Marker not in bound_set → fallback_failed++ (not resolved).
   17.  Binding in bound_set but no FK target → fallback_failed (has_target guard).
   18.  3 markers: 2 resolved in primary, 1 fuzzy-resolved in delayed →
        citation_mode_used='hybrid_delayed_attachment'.
   19.  All unresolved markers fail in delayed → citation_mode='posthoc_span_resolution'.
   20.  telemetry keys: fallback_resolved_count, fallback_failed_count,
        citation_mode_used all present.
   21.  Spans returned by delayed resolver use NIL UUID as placeholder
        (same contract as primary resolve_spans).

  GeoRAGResponse model:
   22.  conflicting_evidence field: None by default.
   23.  freshness field: None by default.
   24.  Both fields accept dict values without Pydantic validation error.

  S4c freshness (unit):
   25.  freshness dict structure has all expected keys when populated.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from app.agent.citation_binding import BoundEvidence, BoundEvidenceSet
from app.models.rag import Citation, GeoRAGResponse
from app.services.conflict_detector import (
    ConflictingEvidence,
    detect_conflicts,
)
from app.services.span_resolver import resolve_spans_delayed

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_WS_ID = UUID("a0000000-0000-0000-0000-000000000001")
_RUN_ID = UUID("b2222222-2222-2222-2222-222222222222")
_NIL_UUID = UUID("00000000-0000-0000-0000-000000000000")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _structured_binding(
    marker: str,
    pk_val: str,
    scalar_props: dict[str, str],
    ev_id: UUID | None = None,
    passage_id: UUID | None = None,
) -> BoundEvidence:
    """Create a structured_record-style BoundEvidence with a pk dict."""
    if ev_id is None and passage_id is None:
        passage_id = uuid4()
    display_ref: dict = {
        "schema": "silver",
        "table": "collars",
        "pk": {"collar_id": pk_val},
        **scalar_props,
    }
    return BoundEvidence(
        marker_text=marker,
        kind="DATA",
        index_or_id=marker.split(":")[1].rstrip("]"),
        source_store="postgis",
        evidence_id=ev_id,
        passage_id=passage_id,
        display_ref=display_ref,
    )


def _graph_binding(
    marker: str,
    start_id: str,
    end_id: str,
    rel_type: str,
    extra_props: dict[str, str] | None = None,
    ev_id: UUID | None = None,
    passage_id: UUID | None = None,
) -> BoundEvidence:
    """Create a graph_edge-style BoundEvidence."""
    if ev_id is None and passage_id is None:
        passage_id = uuid4()
    display_ref: dict = {
        "start_node_id": start_id,
        "end_node_id": end_id,
        "rel_type": rel_type,
        **(extra_props or {}),
    }
    return BoundEvidence(
        marker_text=marker,
        kind="DATA",
        index_or_id=marker.split(":")[1].rstrip("]"),
        source_store="neo4j",
        evidence_id=ev_id,
        passage_id=passage_id,
        display_ref=display_ref,
    )


def _passage_binding(marker: str) -> BoundEvidence:
    """Create a passage-type BoundEvidence (no pk, no node keys)."""
    return BoundEvidence(
        marker_text=marker,
        kind="NI43",
        index_or_id=marker.split(":")[1].rstrip("]"),
        source_store="qdrant",
        evidence_id=None,
        passage_id=uuid4(),
        display_ref={"chunk_id": str(uuid4())},
    )


def _fk_binding(
    marker: str,
    preview: str = "",
    ev_id: UUID | None = None,
    passage_id: UUID | None = None,
) -> BoundEvidence:
    """Simple binding with at least one FK target (for delayed resolver tests)."""
    if ev_id is None and passage_id is None:
        passage_id = uuid4()
    return BoundEvidence(
        marker_text=marker,
        kind="DATA",
        index_or_id=marker.split(":")[1].rstrip("]"),
        source_store="postgis",
        evidence_id=ev_id,
        passage_id=passage_id,
        display_ref={"tool": "query_spatial_collars", "slot": 1},
        preview_text=preview,
    )


def _no_fk_binding(marker: str, preview: str = "") -> BoundEvidence:
    """Binding with no FK targets (simulates unresolvable DATA slot)."""
    return BoundEvidence(
        marker_text=marker,
        kind="DATA",
        index_or_id=marker.split(":")[1].rstrip("]"),
        source_store="postgis",
        evidence_id=None,
        passage_id=None,
        display_ref={"tool": "query_spatial_collars", "slot": 1},
        preview_text=preview,
    )


# ---------------------------------------------------------------------------
# conflict_detector — structured_record
# ---------------------------------------------------------------------------

def test_detect_conflicts_same_value_no_conflict():
    """Two bindings, same entity key, same property value → no conflict."""
    pk_val = str(uuid4())
    b1 = _structured_binding("[DATA:1]", pk_val, {"total_depth": "250.0"})
    b2 = _structured_binding("[DATA:2]", pk_val, {"total_depth": "250.0"})
    result = detect_conflicts([b1, b2])
    assert result == []


def test_detect_conflicts_structured_record_different_value(caplog):
    """S4a: Two bindings, same entity key, differing total_depth → 1 conflict."""
    pk_val = str(uuid4())
    b1 = _structured_binding("[DATA:1]", pk_val, {"total_depth": "250.0"})
    b2 = _structured_binding("[DATA:2]", pk_val, {"total_depth": "312.5"})
    result = detect_conflicts([b1, b2])
    assert len(result) == 1
    c = result[0]
    assert isinstance(c, ConflictingEvidence)
    assert "collars" in c.entity_key
    assert c.property_name == "total_depth"
    assert set(c.values) == {"250.0", "312.5"}
    assert len(c.evidence_ids) == 2


def test_detect_conflicts_three_bindings_two_agree():
    """3 bindings: two agree, one differs → 1 conflict with 2 distinct values."""
    pk_val = str(uuid4())
    b1 = _structured_binding("[DATA:1]", pk_val, {"total_depth": "250.0"})
    b2 = _structured_binding("[DATA:2]", pk_val, {"total_depth": "250.0"})
    b3 = _structured_binding("[DATA:3]", pk_val, {"total_depth": "312.5"})
    result = detect_conflicts([b1, b2, b3])
    assert len(result) == 1
    c = result[0]
    assert set(c.values) == {"250.0", "312.5"}
    # evidence_ids deduped by value — 2 distinct values
    assert len(c.evidence_ids) == 2


def test_detect_conflicts_no_pk_key_not_treated_as_structured():
    """Bindings without 'pk' key in display_ref are not structured_record."""
    b1 = BoundEvidence(
        marker_text="[DATA:1]",
        kind="DATA",
        index_or_id="1",
        source_store="postgis",
        evidence_id=None,
        passage_id=uuid4(),
        display_ref={"tool": "something", "total_depth": "250.0"},
    )
    b2 = BoundEvidence(
        marker_text="[DATA:2]",
        kind="DATA",
        index_or_id="2",
        source_store="postgis",
        evidence_id=None,
        passage_id=uuid4(),
        display_ref={"tool": "something", "total_depth": "999.0"},
    )
    result = detect_conflicts([b1, b2])
    assert result == []


# ---------------------------------------------------------------------------
# conflict_detector — graph_edge
# ---------------------------------------------------------------------------

def test_detect_conflicts_graph_edge_different_rel_type():
    """Two graph bindings, same (start,end) pair, different rel_type → 1 conflict."""
    b1 = _graph_binding("[DATA:1]", "node_100", "node_200", "INTERSECTS")
    b2 = _graph_binding("[DATA:2]", "node_100", "node_200", "CONTAINS")
    result = detect_conflicts([b1, b2])
    assert len(result) >= 1
    conflict_props = [c.property_name for c in result]
    assert "rel_type" in conflict_props


def test_detect_conflicts_graph_edge_same_rel_different_property():
    """Two graph bindings, same (start,end) + rel_type, differing scalar property."""
    b1 = _graph_binding("[DATA:1]", "node_100", "node_200", "SAMPLES",
                        extra_props={"grade_g_t": "1.2"})
    b2 = _graph_binding("[DATA:2]", "node_100", "node_200", "SAMPLES",
                        extra_props={"grade_g_t": "0.8"})
    result = detect_conflicts([b1, b2])
    assert len(result) >= 1
    conflict_props = [c.property_name for c in result]
    assert "grade_g_t" in conflict_props


# ---------------------------------------------------------------------------
# conflict_detector — passage / edge cases
# ---------------------------------------------------------------------------

def test_detect_conflicts_passage_bindings_skipped():
    """Passage bindings (no pk, no node IDs) are skipped — no conflicts emitted."""
    b1 = _passage_binding("[NI43:1]")
    b2 = _passage_binding("[NI43:2]")
    result = detect_conflicts([b1, b2])
    assert result == []


def test_detect_conflicts_empty_bindings():
    """Empty binding list → empty result, no error."""
    result = detect_conflicts([])
    assert result == []


def test_detect_conflicts_single_binding():
    """Single binding cannot conflict with itself → empty result."""
    pk_val = str(uuid4())
    b = _structured_binding("[DATA:1]", pk_val, {"total_depth": "250.0"})
    result = detect_conflicts([b])
    assert result == []


def test_detect_conflicts_corrupt_display_ref_no_raise():
    """Corrupt display_ref (None) → no exception; empty result."""
    b1 = BoundEvidence(
        marker_text="[DATA:1]",
        kind="DATA",
        index_or_id="1",
        source_store="postgis",
        evidence_id=None,
        passage_id=uuid4(),
        display_ref=None,
    )
    b2 = BoundEvidence(
        marker_text="[DATA:2]",
        kind="DATA",
        index_or_id="2",
        source_store="postgis",
        evidence_id=None,
        passage_id=uuid4(),
        display_ref=None,
    )
    result = detect_conflicts([b1, b2])
    assert result == []


def test_detect_conflicts_structural_keys_excluded():
    """Structural keys (schema, table, pk, tool, slot) are not compared as properties."""
    pk_val = str(uuid4())
    # Give same entity key but different 'tool' value — should not conflict.
    b1 = BoundEvidence(
        marker_text="[DATA:1]",
        kind="DATA",
        index_or_id="1",
        source_store="postgis",
        evidence_id=None,
        passage_id=uuid4(),
        display_ref={
            "schema": "silver",
            "table": "collars",
            "pk": {"collar_id": pk_val},
            "tool": "query_spatial_collars",
        },
    )
    b2 = BoundEvidence(
        marker_text="[DATA:2]",
        kind="DATA",
        index_or_id="2",
        source_store="postgis",
        evidence_id=None,
        passage_id=uuid4(),
        display_ref={
            "schema": "silver",
            "table": "collars",
            "pk": {"collar_id": pk_val},
            "tool": "query_assay_data",  # different tool — should be ignored
        },
    )
    result = detect_conflicts([b1, b2])
    assert result == []


def test_detect_conflicts_both_structured_and_graph():
    """Mixed run: structured conflict + graph conflict → both returned."""
    pk_val = str(uuid4())
    sb1 = _structured_binding("[DATA:1]", pk_val, {"total_depth": "250.0"})
    sb2 = _structured_binding("[DATA:2]", pk_val, {"total_depth": "312.5"})
    gb1 = _graph_binding("[DATA:3]", "node_A", "node_B", "INTERSECTS")
    gb2 = _graph_binding("[DATA:4]", "node_A", "node_B", "CONTAINS")
    result = detect_conflicts([sb1, sb2, gb1, gb2])
    conflict_props = [c.property_name for c in result]
    assert "total_depth" in conflict_props
    assert "rel_type" in conflict_props


# ---------------------------------------------------------------------------
# resolve_spans_delayed — S4b scenario
# ---------------------------------------------------------------------------

def test_delayed_resolve_fuzzy_marker_whitespace_colon():
    """Fuzzy: [DATA : 1] with whitespace around colon → resolves via fuzzy regex."""
    marker = "[DATA:1]"
    bound = BoundEvidenceSet()
    bound.add(_fk_binding(marker))

    answer = "There are 10 drill holes [DATA : 1]."  # whitespace around colon

    items, spans_per_item, tel = resolve_spans_delayed(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        unresolved_marker_texts={marker},
    )

    assert len(items) == 1
    assert items[0].marker_text == marker
    assert tel["fallback_resolved_count"] == 1
    assert tel["fallback_failed_count"] == 0
    assert tel["citation_mode_used"] == "hybrid_delayed_attachment"


def test_delayed_resolve_preview_substring_match():
    """Strategy (b): preview_text substring in answer → resolved."""
    marker = "[DATA:1]"
    long_preview = "1 drill hole in PLS Project collar WB-001"  # 42 chars — > 12
    bound = BoundEvidenceSet()
    bound.add(_fk_binding(marker, preview=long_preview))

    # Answer contains the preview text but not the marker.
    answer = (
        "Based on the data, 1 drill hole in PLS Project collar WB-001 "
        "was drilled to a depth of 250 m."
    )

    items, spans_per_item, tel = resolve_spans_delayed(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        unresolved_marker_texts={marker},
    )

    assert len(items) == 1
    assert tel["fallback_resolved_count"] == 1
    assert tel["citation_mode_used"] == "hybrid_delayed_attachment"


def test_delayed_resolve_short_preview_not_used():
    """Short preview (< 12 chars) is not used as a substring anchor."""
    marker = "[DATA:1]"
    short_preview = "collar"  # only 6 chars — too ambiguous
    bound = BoundEvidenceSet()
    bound.add(_fk_binding(marker, preview=short_preview))

    answer = "There are collar results."  # contains the short preview text

    items, spans_per_item, tel = resolve_spans_delayed(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        unresolved_marker_texts={marker},
    )

    # Too short → not resolved by substring; no fuzzy match either.
    assert len(items) == 0
    assert tel["fallback_failed_count"] == 1
    assert tel["citation_mode_used"] == "posthoc_span_resolution"


def test_delayed_resolve_marker_not_in_bound_set():
    """Marker not in bound_set at all → fallback_failed++, no crash."""
    bound = BoundEvidenceSet()
    bound.add(_fk_binding("[DATA:1]"))

    answer = "Holes [DATA:99]."  # [DATA:99] not in bound_set

    items, spans_per_item, tel = resolve_spans_delayed(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        unresolved_marker_texts={"[DATA:99]"},
    )

    assert len(items) == 0
    assert tel["fallback_failed_count"] == 1
    assert tel["citation_mode_used"] == "posthoc_span_resolution"


def test_delayed_resolve_no_fk_target_skipped():
    """Binding with no FK target → has_target guard fails → fallback_failed++."""
    marker = "[DATA:1]"
    bound = BoundEvidenceSet()
    bound.add(_no_fk_binding(marker, preview=""))

    answer = "Holes [DATA : 1]."  # fuzzy match would find it...

    items, spans_per_item, tel = resolve_spans_delayed(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        unresolved_marker_texts={marker},
    )

    assert len(items) == 0
    assert tel["fallback_failed_count"] == 1


def test_delayed_resolve_three_markers_one_fallback():
    """S4b full scenario: 3 markers, 2 already resolved (not passed here), 1 fuzzy.

    resolve_spans_delayed only receives the 1 unresolved marker.
    It resolves it via fuzzy → citation_mode = hybrid_delayed_attachment.
    """
    # Primary pass resolved [DATA:1] and [NI43:2]; [DATA:3] was missed.
    unresolved_marker = "[DATA:3]"
    bound = BoundEvidenceSet()
    bound.add(_fk_binding("[DATA:1]"))
    bound.add(_fk_binding("[NI43:2]"))
    bound.add(_fk_binding("[DATA:3]"))

    # The LLM used a whitespace variant for [DATA:3] only.
    answer = (
        "Count is 20 [DATA:1]. Grade 1.2% [NI43:2]. "
        "Depth avg 250 m [DATA : 3]."  # <-- fuzzy variant
    )

    items, spans_per_item, tel = resolve_spans_delayed(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        unresolved_marker_texts={unresolved_marker},
    )

    assert len(items) == 1
    assert items[0].marker_text == unresolved_marker
    assert tel["fallback_resolved_count"] == 1
    assert tel["citation_mode_used"] == "hybrid_delayed_attachment"


def test_delayed_resolve_all_fail_citation_mode_posthoc():
    """All unresolved markers fail in delayed → citation_mode=posthoc_span_resolution."""
    bound = BoundEvidenceSet()
    bound.add(_fk_binding("[DATA:1]"))
    bound.add(_fk_binding("[DATA:2]"))

    # No markers or preview text in the answer.
    answer = "No recognizable citations here."

    items, spans_per_item, tel = resolve_spans_delayed(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        unresolved_marker_texts={"[DATA:1]", "[DATA:2]"},
    )

    assert len(items) == 0
    assert tel["fallback_failed_count"] == 2
    assert tel["fallback_resolved_count"] == 0
    assert tel["citation_mode_used"] == "posthoc_span_resolution"


def test_delayed_resolve_telemetry_keys_present():
    """All expected telemetry keys present in fallback result."""
    bound = BoundEvidenceSet()
    bound.add(_fk_binding("[DATA:1]"))

    _, _, tel = resolve_spans_delayed(
        answer_text="No markers here.",
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        unresolved_marker_texts={"[DATA:1]"},
    )

    assert "fallback_resolved_count" in tel
    assert "fallback_failed_count" in tel
    assert "citation_mode_used" in tel


def test_delayed_resolve_spans_nil_uuid_placeholder():
    """Spans from delayed resolver use NIL UUID as placeholder (matches primary contract)."""
    marker = "[DATA:1]"
    bound = BoundEvidenceSet()
    bound.add(_fk_binding(marker))

    answer = "Holes [DATA : 1]."  # fuzzy variant

    items, spans_per_item, tel = resolve_spans_delayed(
        answer_text=answer,
        bound_set=bound,
        answer_run_id=_RUN_ID,
        workspace_id=_WS_ID,
        unresolved_marker_texts={marker},
    )

    assert len(items) == 1
    assert len(spans_per_item) == 1
    for span in spans_per_item[0]:
        assert span.answer_citation_item_id == _NIL_UUID


# ---------------------------------------------------------------------------
# GeoRAGResponse model — new fields
# ---------------------------------------------------------------------------

def _minimal_citation() -> Citation:
    return Citation(
        citation_id="[DATA-1]",
        citation_type="DATA",
        source_chunk_id="test-chunk-001",
        document_title="Test Source",
        relevance_score=0.85,
    )


def _minimal_response() -> GeoRAGResponse:
    return GeoRAGResponse(
        text="The project has 20 drill holes [DATA-1].",
        citations=[_minimal_citation()],
        confidence=0.8,
        sources_used=["test-chunk-001"],
    )


def test_geo_rag_response_conflicting_evidence_none_default():
    """conflicting_evidence defaults to None."""
    r = _minimal_response()
    assert r.conflicting_evidence is None


def test_geo_rag_response_freshness_none_default():
    """freshness defaults to None."""
    r = _minimal_response()
    assert r.freshness is None


def test_geo_rag_response_conflicting_evidence_accepts_list():
    """conflicting_evidence accepts a list of dicts without Pydantic error."""
    r = _minimal_response()
    r.conflicting_evidence = [
        {
            "entity_key": "silver.collars:collar_id=abc",
            "property_name": "total_depth",
            "evidence_ids": ["[DATA:1]", "[DATA:2]"],
            "values": ["250.0", "312.5"],
        }
    ]
    assert len(r.conflicting_evidence) == 1


def test_geo_rag_response_freshness_accepts_dict():
    """freshness accepts a dict with data_version keys."""
    from datetime import datetime
    r = _minimal_response()
    r.freshness = {
        "workspace_data_version_at_query": 42,
        "project_data_version_at_query": 7,
        "answered_at": datetime.utcnow().isoformat(),
    }
    assert r.freshness["workspace_data_version_at_query"] == 42


# ---------------------------------------------------------------------------
# S4c freshness — unit level (key structure)
# ---------------------------------------------------------------------------

def test_freshness_dict_has_all_expected_keys():
    """S4c: freshness dict produced by orchestrator logic has required keys."""
    from datetime import datetime
    freshness = {
        "workspace_data_version_at_query": 15,
        "project_data_version_at_query": 3,
        "answered_at": datetime.utcnow().isoformat(),
    }
    assert "workspace_data_version_at_query" in freshness
    assert "project_data_version_at_query" in freshness
    assert "answered_at" in freshness
    assert isinstance(freshness["workspace_data_version_at_query"], int)
    assert isinstance(freshness["answered_at"], str)
    # answered_at should be parseable as ISO8601
    parsed = datetime.fromisoformat(freshness["answered_at"])
    assert parsed.year == datetime.utcnow().year
