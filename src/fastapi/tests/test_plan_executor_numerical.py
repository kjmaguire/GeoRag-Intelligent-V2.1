"""Phase H4 — `numerical_aggregation` plan_executor graduation tests."""
from __future__ import annotations

import pytest

from app.agent.plan_executor import (
    _build_agg_sql,
    _column_hash,
    _is_safe_ident,
)


def test_is_safe_ident_accepts_normal_columns() -> None:
    assert _is_safe_ident("hole_id")
    assert _is_safe_ident("a")
    assert _is_safe_ident("collar_id_v2")


def test_is_safe_ident_rejects_quotes_and_dots() -> None:
    assert not _is_safe_ident("hole_id; DROP TABLE")
    assert not _is_safe_ident("silver.collars")
    assert not _is_safe_ident("'hole_id'")
    assert not _is_safe_ident("")
    assert not _is_safe_ident("Hole_Id")  # uppercase not allowed
    assert not _is_safe_ident("9_starts_with_digit")


def test_build_agg_sql_count_star() -> None:
    sql, params = _build_agg_sql(
        operation="count", table="collars", column="*",
        group_by=[], filter_expr=None,
    )
    assert "SELECT count(*) AS agg FROM silver.collars" in sql
    assert params == []


def test_build_agg_sql_avg_with_filter() -> None:
    sql, params = _build_agg_sql(
        operation="avg", table="samples", column="grade_au_g_t",
        group_by=[], filter_expr={"project_id": "p-1"},
    )
    assert "avg(grade_au_g_t) AS agg" in sql
    assert "FROM silver.samples" in sql
    assert "WHERE project_id = $1" in sql
    assert params == ["p-1"]


def test_build_agg_sql_group_by_multi() -> None:
    sql, params = _build_agg_sql(
        operation="sum", table="lithology_logs", column="length_m",
        group_by=["lithology_code", "collar_id"], filter_expr=None,
    )
    assert "GROUP BY lithology_code, collar_id" in sql
    assert "lithology_code, collar_id, sum(length_m) AS agg" in sql


def test_build_agg_sql_filter_list_uses_in() -> None:
    sql, params = _build_agg_sql(
        operation="count", table="collars", column="*",
        group_by=[], filter_expr={"hole_type": ["DDH", "RC", "RAB"]},
    )
    assert "hole_type IN ($1, $2, $3)" in sql
    assert params == ["DDH", "RC", "RAB"]


def test_build_agg_sql_rejects_bad_operation() -> None:
    with pytest.raises(ValueError, match="operation not allowed"):
        _build_agg_sql(
            operation="DROP_TABLE",  # type: ignore[arg-type]
            table="collars", column="*",
            group_by=[], filter_expr=None,
        )


def test_build_agg_sql_rejects_bad_table() -> None:
    with pytest.raises(ValueError, match="table not allowed"):
        _build_agg_sql(
            operation="count", table="pg_catalog.pg_user",
            column="*", group_by=[], filter_expr=None,
        )


def test_build_agg_sql_rejects_unsafe_column() -> None:
    with pytest.raises(ValueError, match="column not a valid identifier"):
        _build_agg_sql(
            operation="avg", table="samples",
            column="grade; DROP TABLE samples;",
            group_by=[], filter_expr=None,
        )


def test_build_agg_sql_rejects_unsafe_filter_key() -> None:
    with pytest.raises(ValueError, match="filter column not a valid identifier"):
        _build_agg_sql(
            operation="count", table="collars", column="*",
            group_by=[], filter_expr={"hole_id OR 1=1": "x"},
        )


def test_column_hash_is_stable() -> None:
    a = _column_hash("grade_au_g_t")
    b = _column_hash("grade_au_g_t")
    c = _column_hash("grade_cu_pct")
    assert a == b
    assert a != c
    assert len(a) == 12
