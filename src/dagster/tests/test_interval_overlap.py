"""Tests for interval_overlap detector — CC-01 Item 1 Slice 3.

Pure unit tests around the SQL builder + OverlapPair adapter. The
SQL-execution path is exercised by integration tests in the
georag_test postgres database; this file stays DB-free so the fast
suite catches regressions in the predicate logic itself.
"""

from __future__ import annotations

import pytest

from georag_dagster.checks.interval_overlap import (
    INTERVAL_OVERLAP_RULE_VERSION,
    OverlapPair,
    find_overlaps_sql,
    summarize_overlaps_as_dq_flags,
)


# ---------------------------------------------------------------------------
# OverlapPair shape
# ---------------------------------------------------------------------------

def test_overlap_pair_as_flag_value_matches_review_queue_contract():
    p = OverlapPair(
        collar_id="00000000-0000-0000-0000-000000000001",
        a_id="aaaa",
        a_from=10.0,
        a_to=20.0,
        b_id="bbbb",
        b_from=15.0,
        b_to=25.0,
    )

    flag = p.as_flag_value()

    # Shape documented in the brief: {a: {from, to}, b: {from, to}}.
    assert set(flag) == {"a", "b"}
    assert flag["a"] == {"id": "aaaa", "from": 10.0, "to": 20.0}
    assert flag["b"] == {"id": "bbbb", "from": 15.0, "to": 25.0}


# ---------------------------------------------------------------------------
# SQL builder
# ---------------------------------------------------------------------------

def test_find_overlaps_sql_uses_strict_overlap_predicate():
    sql = find_overlaps_sql("silver.lithology")

    # Half-open intervals — adjacency (a.to == b.from) must NOT match.
    assert "a.from_depth < b.to_depth" in sql
    assert "b.from_depth < a.to_depth" in sql


def test_find_overlaps_sql_filters_to_one_canonical_pair_per_overlap():
    sql = find_overlaps_sql("silver.assays_v2")
    # a.id < b.id deduplicates (a,b) vs (b,a).
    assert "a.id < b.id" in sql


def test_find_overlaps_sql_optional_collar_filter_is_null_safe():
    sql = find_overlaps_sql("silver.lithology")
    # When collar_ids param is NULL the WHERE clause must short-circuit
    # so a whole-table sweep works.
    assert "IS NULL" in sql
    assert "ANY" in sql


def test_find_overlaps_sql_rejects_unsupported_table():
    with pytest.raises(ValueError) as exc:
        find_overlaps_sql("public.user_uploads")

    assert "supported set" in str(exc.value)


@pytest.mark.parametrize("table", ["silver.lithology", "silver.assays_v2"])
def test_find_overlaps_sql_supports_both_drill_tables(table):
    sql = find_overlaps_sql(table)
    assert table in sql


# ---------------------------------------------------------------------------
# Plan §6a — summarize_overlaps_as_dq_flags
# ---------------------------------------------------------------------------

_WS = "00000000-0000-0000-0000-00000000ffff"
_PROJ = "00000000-0000-0000-0000-00000000aaaa"
_COLLAR_A = "00000000-0000-0000-0000-000000000001"
_COLLAR_B = "00000000-0000-0000-0000-000000000002"


def _pair(collar_id=_COLLAR_A, a_id="a", a_from=10.0, a_to=20.0,
          b_id="b", b_from=15.0, b_to=25.0):
    return OverlapPair(
        collar_id=collar_id,
        a_id=a_id, a_from=a_from, a_to=a_to,
        b_id=b_id, b_from=b_from, b_to=b_to,
    )


def test_summarize_overlaps_empty_input_returns_no_flags():
    flags = summarize_overlaps_as_dq_flags(
        pairs=[],
        collar_map={_COLLAR_A: (_WS, _PROJ)},
        source_table="silver.lithology",
    )
    assert flags == []


def test_summarize_overlaps_one_pair_emits_one_summary_flag():
    flags = summarize_overlaps_as_dq_flags(
        pairs=[_pair()],
        collar_map={_COLLAR_A: (_WS, _PROJ)},
        source_table="silver.lithology",
        hole_id_map={_COLLAR_A: "ECK-22-001"},
    )

    assert len(flags) == 1
    f = flags[0]
    assert f.record_type == "collar"
    assert f.record_id == _COLLAR_A
    assert f.workspace_id == _WS
    assert f.project_id == _PROJ
    assert f.severity == "WARNING"
    assert f.flag_type == "lithology.interval_overlap"
    assert f.rule_id == "lithology.interval_overlap"
    assert f.rule_version == INTERVAL_OVERLAP_RULE_VERSION
    assert "ECK-22-001" in f.description
    assert "1 overlapping interval pair" in f.description


def test_summarize_overlaps_multiple_pairs_one_collar_collapse_to_one_flag():
    """Fan-IN behaviour — N pairs on one collar = 1 flag (NOT N flags)."""
    pairs = [
        _pair(a_from=10.0, a_to=20.0, b_from=15.0, b_to=25.0),
        _pair(a_id="c", a_from=30.0, a_to=40.0,
              b_id="d", b_from=35.0, b_to=45.0),
        _pair(a_id="e", a_from=50.0, a_to=60.0,
              b_id="f", b_from=55.0, b_to=65.0),
    ]
    flags = summarize_overlaps_as_dq_flags(
        pairs=pairs,
        collar_map={_COLLAR_A: (_WS, _PROJ)},
        source_table="silver.lithology",
    )

    assert len(flags) == 1
    f = flags[0]
    assert "3 overlapping interval pairs" in f.description
    assert "+2 more on this collar" in f.description
    # threshold_payload carries the full pair list for drill-down.
    assert f.threshold_payload["pair_count"] == 3
    assert len(f.threshold_payload["pairs"]) == 3


def test_summarize_overlaps_distinct_collars_emit_distinct_flags():
    pairs = [
        _pair(collar_id=_COLLAR_A),
        _pair(collar_id=_COLLAR_B),
    ]
    flags = summarize_overlaps_as_dq_flags(
        pairs=pairs,
        collar_map={
            _COLLAR_A: (_WS, _PROJ),
            _COLLAR_B: (_WS, _PROJ),
        },
        source_table="silver.assays_v2",
    )

    assert len(flags) == 2
    collar_ids = {f.record_id for f in flags}
    assert collar_ids == {_COLLAR_A, _COLLAR_B}
    # assays_v2 picks the assay.* flag_type discriminator.
    assert all(f.flag_type == "assay.interval_overlap" for f in flags)


def test_summarize_overlaps_lithology_vs_assay_uses_distinct_flag_types():
    """Different source_table → different flag_type so the badge UI
    doesn't collapse them into a single row."""
    lith = summarize_overlaps_as_dq_flags(
        pairs=[_pair()],
        collar_map={_COLLAR_A: (_WS, _PROJ)},
        source_table="silver.lithology",
    )
    assay = summarize_overlaps_as_dq_flags(
        pairs=[_pair()],
        collar_map={_COLLAR_A: (_WS, _PROJ)},
        source_table="silver.assays_v2",
    )
    assert lith[0].flag_type == "lithology.interval_overlap"
    assert assay[0].flag_type == "assay.interval_overlap"
    assert lith[0].flag_type != assay[0].flag_type


def test_summarize_overlaps_unknown_collar_is_skipped_not_raised():
    """When collar_map is missing an entry, the orphan pair must NOT
    blow up — RLS would reject the write anyway, and the pair still
    lands in review_queue via the sibling path."""
    flags = summarize_overlaps_as_dq_flags(
        pairs=[_pair(collar_id="missing-from-map")],
        collar_map={_COLLAR_A: (_WS, _PROJ)},
        source_table="silver.lithology",
    )
    assert flags == []


def test_summarize_overlaps_rejects_unknown_source_table():
    with pytest.raises(ValueError) as exc:
        summarize_overlaps_as_dq_flags(
            pairs=[_pair()],
            collar_map={_COLLAR_A: (_WS, _PROJ)},
            source_table="public.user_uploads",
        )
    assert "_TABLE_TO_FLAG_TYPE" in str(exc.value)


def test_summarize_overlaps_falls_back_to_collar_prefix_when_hole_id_missing():
    flags = summarize_overlaps_as_dq_flags(
        pairs=[_pair()],
        collar_map={_COLLAR_A: (_WS, _PROJ)},
        source_table="silver.lithology",
        # hole_id_map omitted entirely
    )
    # Falls back to collar_id[:8].
    assert _COLLAR_A[:8] in flags[0].description


def test_summarize_overlaps_idempotency_key_is_stable_across_reruns():
    """Two identical runs must produce flags with the same idempotency
    key tuple (workspace, record_type, record_id, flag_type, rule_version)
    so the upsert is a no-op on re-runs."""
    pairs = [_pair()]
    run_1 = summarize_overlaps_as_dq_flags(
        pairs=pairs,
        collar_map={_COLLAR_A: (_WS, _PROJ)},
        source_table="silver.lithology",
    )
    run_2 = summarize_overlaps_as_dq_flags(
        pairs=pairs,
        collar_map={_COLLAR_A: (_WS, _PROJ)},
        source_table="silver.lithology",
    )

    def _key(f):
        return (f.workspace_id, f.record_type, f.record_id,
                f.flag_type, f.rule_version)

    assert _key(run_1[0]) == _key(run_2[0])
