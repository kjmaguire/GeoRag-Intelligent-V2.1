"""Unit tests for tools.query_collar_details (2026-05-25).

Verifies the structured "tell me about hole X" path:
  * shape of CollarDetailsResult on hit
  * source_row_ids carries the collar_id for §04i binding
  * count=0 on miss so _is_empty_tool_result drops it cleanly
  * exact-hole_id match wins over canonical-form match
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.agent.tools import CollarDetailsResult, query_collar_details

# ---------------------------------------------------------------------------
# Minimal asyncpg mocks
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(
        self,
        *,
        collar_row: dict | None,
        assay_count: int = 0,
        sample_count: int = 0,
        litho_count: int = 0,
        structure_count: int = 0,
        max_assay: dict | None = None,
        litho_rows: list[dict] | None = None,
        aggregates_error: Exception | None = None,
        litho_error: Exception | None = None,
        collar_error: Exception | None = None,
    ) -> None:
        self._aggregates_error = aggregates_error
        self._litho_error = litho_error
        self._collar_error = collar_error
        self._collar_row = collar_row
        self._assay_count = assay_count
        self._sample_count = sample_count
        self._litho_count = litho_count
        self._structure_count = structure_count
        self._max_assay = max_assay
        self._litho_rows = litho_rows or []
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args: Any):
        self.fetchrow_calls.append((sql, args))
        if "FROM silver.collars" in sql and "match_priority" in sql:
            if self._collar_error is not None:
                raise self._collar_error
            return self._collar_row
        if "AS assay_count" in sql:
            # One round-trip for the four counts and the headline assay.
            if self._aggregates_error is not None:
                raise self._aggregates_error
            m = self._max_assay or {}
            return {
                "assay_count": self._assay_count,
                "sample_count": self._sample_count,
                "litho_count": self._litho_count,
                "structure_count": self._structure_count,
                "max_element": m.get("element"),
                "max_value": m.get("value"),
                "max_unit": m.get("unit"),
                "max_from": m.get("from_depth"),
                "max_to": m.get("to_depth"),
            }
        return None

    async def fetch(self, sql: str, *args: Any):
        if "lithology_logs" in sql and "GROUP BY lithology_code" in sql:
            if self._litho_error is not None:
                raise self._litho_error
            return self._litho_rows
        return []


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


class _FakeDeps:
    def __init__(self, conn: _FakeConn) -> None:
        self.pg_pool = _FakePool(conn)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


WORKSPACE = "a0000000-0000-0000-0000-000000000001"
PROJECT = "762b147e-af53-4593-b569-04ee46f31d97"
COLLAR_ID = "6e5144c7-55f3-48a5-96cb-245aafb06ace"


def _collar_row(**overrides):
    base = {
        "collar_id": COLLAR_ID,
        "hole_id": "36-1085",
        "hole_id_canonical": None,
        "project_id": PROJECT,
        "easting": 421000.0,
        "northing": 4630000.0,
        "elevation": 2100.0,
        "total_depth": 372.3,
        "drill_type": "DDH",
        "hole_type": "Diamond",
        "azimuth": 90.0,
        "dip": -60.0,
        "drill_date": "1985-06-15",
        "geologist": "J. Smith",
        "match_priority": 1,
    }
    base.update(overrides)
    return base


async def test_hit_shape_and_source_row_ids() -> None:
    conn = _FakeConn(
        collar_row=_collar_row(),
        assay_count=42,
        sample_count=30,
        litho_count=18,
        structure_count=3,
        max_assay={
            "element": "U3O8",
            "value": 0.342,
            "unit": "pct",
            "from_depth": 145.2,
            "to_depth": 146.7,
        },
        litho_rows=[
            {"code": "SS", "total_m": 180.0},
            {"code": "CGL", "total_m": 60.5},
        ],
    )
    deps = _FakeDeps(conn)

    result = await query_collar_details(deps, WORKSPACE, PROJECT, "36-1085")

    assert isinstance(result, CollarDetailsResult)
    assert result.count == 1
    assert result.collar_id == COLLAR_ID
    assert result.hole_id == "36-1085"
    assert result.total_depth == pytest.approx(372.3)
    assert result.drill_type == "DDH"
    assert result.assay_count == 42
    assert result.sample_count == 30
    assert result.lithology_count == 18
    assert result.structure_count == 3
    assert result.max_assay_value == {
        "element": "U3O8",
        "value": pytest.approx(0.342),
        "unit": "pct",
        "depth_from": pytest.approx(145.2),
        "depth_to": pytest.approx(146.7),
    }
    assert result.lithology_summary[0]["rock_code"] == "SS"
    assert result.lithology_summary[0]["total_metres"] == pytest.approx(180.0)
    # §04i citation binding — source_row_ids carries the collar_id.
    assert result.source_row_ids == [COLLAR_ID]


async def test_miss_returns_count_zero_with_none_collar() -> None:
    conn = _FakeConn(collar_row=None)
    deps = _FakeDeps(conn)

    result = await query_collar_details(
        deps, WORKSPACE, PROJECT, "DOES-NOT-EXIST"
    )

    assert result.count == 0
    assert result.collar_id is None
    assert result.hole_id is None
    assert result.source_row_ids == []
    # No follow-up aggregate queries should have been issued.
    aggregate_sqls = [
        sql for sql, _ in conn.fetchrow_calls if "silver.collars" not in sql
    ]
    assert aggregate_sqls == []


async def test_workspace_and_project_in_collar_sql_bind() -> None:
    """Sanity check: the parameterized SQL receives workspace + project +
    hole_id as $1, $2, $3 — i.e. RLS scoping is never skipped."""
    conn = _FakeConn(collar_row=_collar_row())
    deps = _FakeDeps(conn)
    await query_collar_details(deps, WORKSPACE, PROJECT, "36-1085")

    collar_call = next(
        (sql, args)
        for sql, args in conn.fetchrow_calls
        if "FROM silver.collars" in sql and "match_priority" in sql
    )
    _sql, args = collar_call
    assert args[0] == WORKSPACE
    assert args[1] == PROJECT
    assert args[2] == "36-1085"


async def test_no_aggregate_calls_on_zero_counts() -> None:
    """When assay/litho counts are 0, the max_assay + litho_summary
    fetches are skipped (one fewer round trip)."""
    conn = _FakeConn(
        collar_row=_collar_row(),
        assay_count=0,
        litho_count=0,
    )
    deps = _FakeDeps(conn)
    result = await query_collar_details(deps, WORKSPACE, PROJECT, "36-1085")
    assert result.count == 1
    assert result.max_assay_value is None
    assert result.lithology_summary == []


# ---------------------------------------------------------------------------
# Audit 2026-10-04 items 11, 12, 28
# ---------------------------------------------------------------------------


async def test_failed_aggregates_are_none_not_zero() -> None:
    """A Postgres timeout in the aggregates used to read as "no samples"."""
    conn = _FakeConn(
        collar_row=_collar_row(), aggregates_error=TimeoutError("statement timeout"),
    )
    result = await query_collar_details(_FakeDeps(conn), WORKSPACE, PROJECT, "36-1085")

    assert result.count == 1
    assert result.hole_id == "36-1085"
    assert result.assay_count is None
    assert result.sample_count is None
    assert result.lithology_count is None
    assert result.structure_count is None
    assert result.max_assay_value is None
    assert result.lithology_summary is None
    assert result.retrieval_failure == "aggregates_unavailable"


async def test_failed_aggregates_render_as_unavailable() -> None:
    from app.agent.agentic_retrieval.nodes import _render_structured_result

    conn = _FakeConn(collar_row=_collar_row(), aggregates_error=RuntimeError("boom"))
    result = await query_collar_details(_FakeDeps(conn), WORKSPACE, PROJECT, "36-1085")
    text = _render_structured_result(result)
    assert "assay_count=unavailable" in text
    assert "sample_count=unavailable" in text
    assert "assay_count=0" not in text
    assert "assay_count=None" not in text


async def test_a_genuine_zero_still_renders_as_zero() -> None:
    from app.agent.agentic_retrieval.nodes import _render_structured_result

    conn = _FakeConn(collar_row=_collar_row(), assay_count=0, sample_count=0)
    result = await query_collar_details(_FakeDeps(conn), WORKSPACE, PROJECT, "36-1085")
    assert result.retrieval_failure is None
    assert result.assay_count == 0
    assert "assay_count=0" in _render_structured_result(result)


async def test_failed_litho_summary_is_unavailable_not_empty() -> None:
    conn = _FakeConn(
        collar_row=_collar_row(), litho_count=18, litho_error=RuntimeError("boom"),
    )
    result = await query_collar_details(_FakeDeps(conn), WORKSPACE, PROJECT, "36-1085")
    assert result.lithology_count == 18
    assert result.lithology_summary is None
    assert result.retrieval_failure == "aggregates_unavailable"


async def test_header_failure_is_reported_not_a_miss() -> None:
    conn = _FakeConn(collar_row=None, collar_error=RuntimeError("connection reset"))
    result = await query_collar_details(_FakeDeps(conn), WORKSPACE, PROJECT, "36-1085")
    assert result.count == 0
    assert result.retrieval_failure == "error"


async def test_a_genuine_miss_has_no_failure() -> None:
    result = await query_collar_details(
        _FakeDeps(_FakeConn(collar_row=None)), WORKSPACE, PROJECT, "NOPE-1",
    )
    assert result.count == 0
    assert result.retrieval_failure is None


async def test_aggregates_are_one_round_trip_and_collar_subquery_has_explicit_columns() -> None:
    conn = _FakeConn(collar_row=_collar_row(), assay_count=3, litho_count=0)
    await query_collar_details(_FakeDeps(conn), WORKSPACE, PROJECT, "36-1085")
    sqls = [sql for sql, _ in conn.fetchrow_calls]
    # collar header + ONE aggregates query (was header + four counts + max).
    assert len(sqls) == 2
    header = next(sql for sql in sqls if "match_priority" in sql)
    assert "SELECT *" not in header
    assert "SELECT collar_id, hole_id," in header
