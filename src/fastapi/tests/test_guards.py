"""Unit tests for plan §4b guard classifier.

Pure-function tests — no I/O, no fixtures beyond plain data.
"""

from __future__ import annotations

import pytest

from app.agent.guards import (
    GuardErrorCode,
    RepairAttempt,
    classify_guards,
    detect_death_loop,
)

# ---------------------------------------------------------------------------
# Enum invariants
# ---------------------------------------------------------------------------


def test_enum_has_all_17_plan_codes():
    """Plan §4b lists 16 quality codes; Z.1 / Appendix C §5 adds
    EGRESS_BLOCKED for external-LLM egress refusals. Lock the
    complete set (17) down."""
    expected = {
        "NO_EVIDENCE_FOUND",
        "ENTITY_NOT_FOUND",
        "AMBIGUOUS_HOLE_ID",
        "AMBIGUOUS_FORMATION_NAME",
        "AMBIGUOUS_PROPERTY_NAME",
        "OVER_FILTERED_QUERY",
        "SPATIAL_QUERY_EMPTY",
        "SPATIAL_CRS_MISMATCH",
        "GRAPH_PATH_NOT_FOUND",
        "NUMERIC_GROUNDING_FAILED",
        "CITATION_INCOMPLETE",
        "CONFLICTING_SOURCES",
        "MISSING_DEPTH_INTERVAL",
        "MISSING_ASSAY_UNITS",
        "SOURCE_SCOPE_VIOLATION",
        "UNSUPPORTED_QUERY_TYPE",
        # Z.1 / Appendix C §5 — external-LLM egress profile gate.
        "EGRESS_BLOCKED",
    }
    actual = {m.value for m in GuardErrorCode}
    assert actual == expected, f"missing={expected - actual}  extra={actual - expected}"


def test_enum_values_match_names():
    """Every enum value string == its name, for stable JSON serialisation."""
    for member in GuardErrorCode:
        assert member.value == member.name


# ---------------------------------------------------------------------------
# Empty / no-fire cases
# ---------------------------------------------------------------------------


def test_empty_inputs_return_empty_list():
    assert classify_guards() == []


def test_clean_run_returns_empty():
    """A successful query with non-empty results and citations should
    produce zero guard codes."""
    codes = classify_guards(
        validation_warnings=[],
        demotion_reasons=[],
        tool_results=[("search_documents", [{"chunk_id": "x"}])],
        response_citations=[{"marker": "[DATA:1]"}],
        citation_lifecycle_state="committed",
        conflicting_evidence_present=False,
    )
    assert codes == []


# ---------------------------------------------------------------------------
# Warning-string mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "warning, expected",
    [
        # Layer 3 — numeric grounding
        (
            "Layer 3: Ungrounded number 2.31 in response — not found in any tool result",
            GuardErrorCode.NUMERIC_GROUNDING_FAILED,
        ),
        (
            "Layer 3 tuple: value 100 reported as 'ppm' but evidence carries it as g/t "
            "(different unit family)",
            GuardErrorCode.MISSING_ASSAY_UNITS,
        ),
        # Layer 4 — entity grounding
        (
            "Layer 4: Drill-hole ID 'ECK-22-001' not found in silver.collars for this project",
            GuardErrorCode.ENTITY_NOT_FOUND,
        ),
        (
            "Layer 4: Commodity 'Au' mentioned but not found in any tool result",
            GuardErrorCode.ENTITY_NOT_FOUND,
        ),
        (
            "Layer 4: Formation/entity name 'Athabasca' could not be resolved in Neo4j",
            GuardErrorCode.ENTITY_NOT_FOUND,
        ),
        # Layer 6 — constraint violation
        (
            "Layer 6: Value 25.0 violates constraint 'au_max_grade'",
            GuardErrorCode.NUMERIC_GROUNDING_FAILED,
        ),
        # Spatial
        (
            "Spatial CRS mismatch between collar (EPSG:26913) and formation (EPSG:4326)",
            GuardErrorCode.SPATIAL_CRS_MISMATCH,
        ),
        (
            "No spatial matches within 500 m of the IP anomaly",
            GuardErrorCode.SPATIAL_QUERY_EMPTY,
        ),
        # Graph
        (
            "No path between entity A and entity B in the knowledge graph",
            GuardErrorCode.GRAPH_PATH_NOT_FOUND,
        ),
        # Ambiguity
        (
            "Hole ID matches multiple drillholes: ECK-22-001, ECK-22-001A",
            GuardErrorCode.AMBIGUOUS_HOLE_ID,
        ),
        (
            "Multiple projects match property name 'Rowan'",
            GuardErrorCode.AMBIGUOUS_PROPERTY_NAME,
        ),
        # Filter / depth / units
        (
            "Query was over-filtered, returned no results; relaxing depth bound",
            GuardErrorCode.OVER_FILTERED_QUERY,
        ),
        (
            "Missing depth interval for assay row",
            GuardErrorCode.MISSING_DEPTH_INTERVAL,
        ),
    ],
)
def test_warning_string_maps_to_expected_code(warning, expected):
    codes = classify_guards(validation_warnings=[warning])
    assert codes == [expected]


def test_unmapped_warning_produces_no_code():
    """Random unmatched strings should not silently map to anything."""
    codes = classify_guards(
        validation_warnings=["some unrelated warning not in our taxonomy"],
    )
    assert codes == []


# ---------------------------------------------------------------------------
# Composite signals
# ---------------------------------------------------------------------------


def test_no_evidence_detection_from_empty_tool_results():
    codes = classify_guards(tool_results=[])
    assert GuardErrorCode.NO_EVIDENCE_FOUND in codes


def test_no_evidence_detection_when_all_payloads_empty():
    codes = classify_guards(
        tool_results=[
            ("search_documents", []),
            ("query_assay_data", []),
        ],
    )
    assert GuardErrorCode.NO_EVIDENCE_FOUND in codes


def test_no_evidence_NOT_fired_when_at_least_one_tool_returned_data():
    codes = classify_guards(
        tool_results=[
            ("search_documents", []),
            ("query_assay_data", [{"hole_id": "ECK-22-001"}]),
        ],
    )
    assert GuardErrorCode.NO_EVIDENCE_FOUND not in codes


def test_no_evidence_NOT_fired_when_tool_results_is_None():
    # None means "we didn't pass tool_results", which is different from
    # "tool_results were empty". Don't infer NO_EVIDENCE_FOUND from absence.
    codes = classify_guards(tool_results=None)
    assert GuardErrorCode.NO_EVIDENCE_FOUND not in codes


def test_citation_incomplete_when_citations_empty():
    codes = classify_guards(response_citations=[])
    assert GuardErrorCode.CITATION_INCOMPLETE in codes


def test_citation_signal_absent_when_response_citations_None():
    """`response_citations=None` means the caller didn't pass it — no
    signal, no code fires."""
    codes = classify_guards(response_citations=None)
    assert GuardErrorCode.CITATION_INCOMPLETE not in codes


def test_citation_incomplete_when_lifecycle_rejected():
    codes = classify_guards(
        response_citations=[{"marker": "[DATA:1]"}],
        citation_lifecycle_state="rejected",
    )
    assert GuardErrorCode.CITATION_INCOMPLETE in codes


def test_citation_complete_when_committed_with_citations():
    codes = classify_guards(
        response_citations=[{"marker": "[DATA:1]"}],
        citation_lifecycle_state="committed",
    )
    assert GuardErrorCode.CITATION_INCOMPLETE not in codes


def test_conflicting_sources_when_flag_set():
    codes = classify_guards(
        response_citations=[{"marker": "[DATA:1]"}],
        citation_lifecycle_state="committed",
        conflicting_evidence_present=True,
    )
    assert GuardErrorCode.CONFLICTING_SOURCES in codes


# ---------------------------------------------------------------------------
# Deduplication + ordering
# ---------------------------------------------------------------------------


def test_duplicate_warnings_dedupe():
    codes = classify_guards(
        validation_warnings=[
            "Layer 3: Ungrounded number A",
            "Layer 3: Ungrounded number B",
            "Layer 3: Ungrounded number C",
        ],
    )
    assert codes == [GuardErrorCode.NUMERIC_GROUNDING_FAILED]


def test_multi_signal_combination():
    """A query that hits multiple guards returns multiple codes in
    insertion order."""
    codes = classify_guards(
        validation_warnings=[
            "Layer 3: Ungrounded number 2.31",
            "Layer 4: Commodity 'Au' mentioned but not found",
        ],
        response_citations=[],
        conflicting_evidence_present=True,
    )
    # Insertion order: NUMERIC_GROUNDING_FAILED (from warning 1),
    # ENTITY_NOT_FOUND (from warning 2), CITATION_INCOMPLETE (from
    # empty citations), CONFLICTING_SOURCES (from flag).
    assert codes == [
        GuardErrorCode.NUMERIC_GROUNDING_FAILED,
        GuardErrorCode.ENTITY_NOT_FOUND,
        GuardErrorCode.CITATION_INCOMPLETE,
        GuardErrorCode.CONFLICTING_SOURCES,
    ]


def test_demotion_reasons_also_classified():
    """demotion_reasons feed the same classifier as validation_warnings."""
    codes = classify_guards(
        demotion_reasons=["Layer 5: source scope violation"],
    )
    assert GuardErrorCode.SOURCE_SCOPE_VIOLATION in codes


# ---------------------------------------------------------------------------
# Plan §4c — death-loop detector tests
# ---------------------------------------------------------------------------


def _attempt(tool: str, filters: dict, count: int) -> RepairAttempt:
    return RepairAttempt(tool_name=tool, filters=filters, result_count=count)


def test_death_loop_empty_history_is_not_a_loop():
    assert detect_death_loop([]) is False


def test_death_loop_single_attempt_is_not_a_loop():
    assert detect_death_loop([_attempt("query_assay_data", {}, 0)]) is False


def test_death_loop_two_identical_empty_attempts_triggers():
    attempts = [
        _attempt("query_assay_data", {"hole_id": "ECK-22-001"}, 0),
        _attempt("query_assay_data", {"hole_id": "ECK-22-001"}, 0),
    ]
    assert detect_death_loop(attempts) is True


def test_death_loop_two_identical_one_result_attempts_triggers():
    """Plan §4c: result_count ≤ 1 counts as 'low-value', not just zero."""
    attempts = [
        _attempt("query_assay_data", {"hole_id": "ECK-22-001"}, 1),
        _attempt("query_assay_data", {"hole_id": "ECK-22-001"}, 1),
    ]
    assert detect_death_loop(attempts) is True


def test_death_loop_NOT_triggered_when_tool_differs():
    attempts = [
        _attempt("query_assay_data", {"hole_id": "X"}, 0),
        _attempt("query_downhole_logs", {"hole_id": "X"}, 0),  # different tool
    ]
    assert detect_death_loop(attempts) is False


def test_death_loop_NOT_triggered_when_filters_differ():
    attempts = [
        _attempt("query_assay_data", {"hole_id": "X"}, 0),
        _attempt("query_assay_data", {"hole_id": "Y"}, 0),  # different filters
    ]
    assert detect_death_loop(attempts) is False


def test_death_loop_NOT_triggered_when_last_returned_data():
    attempts = [
        _attempt("query_assay_data", {"hole_id": "X"}, 0),
        _attempt("query_assay_data", {"hole_id": "X"}, 5),  # found data
    ]
    assert detect_death_loop(attempts) is False


def test_death_loop_NOT_triggered_when_prev_returned_data():
    """A loop requires BOTH attempts to be low-value."""
    attempts = [
        _attempt("query_assay_data", {"hole_id": "X"}, 8),
        _attempt("query_assay_data", {"hole_id": "X"}, 0),  # only last is empty
    ]
    assert detect_death_loop(attempts) is False


def test_death_loop_looks_only_at_last_two_attempts():
    """A longer history with a death loop at the tail still triggers."""
    attempts = [
        _attempt("search_documents", {"q": "foo"}, 12),  # earlier success
        _attempt("traverse_knowledge_graph", {"node": "A"}, 0),
        _attempt("query_assay_data", {"hole_id": "X"}, 0),
        _attempt("query_assay_data", {"hole_id": "X"}, 0),  # ← death loop here
    ]
    assert detect_death_loop(attempts) is True
