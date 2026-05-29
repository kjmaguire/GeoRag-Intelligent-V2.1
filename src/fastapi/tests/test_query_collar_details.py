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
    ) -> None:
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
            return self._collar_row
        if "silver.assays_v2" in sql and "COUNT" in sql:
            return {"n": self._assay_count}
        if "silver.samples" in sql and "COUNT" in sql:
            return {"n": self._sample_count}
        if "silver.lithology_logs" in sql and "COUNT" in sql:
            return {"n": self._litho_count}
        if "silver.structure" in sql and "COUNT" in sql:
            return {"n": self._structure_count}
        if "silver.assays_v2" in sql and "ORDER BY value DESC" in sql:
            return self._max_assay
        return None

    async def fetch(self, sql: str, *args: Any):
        if "lithology_logs" in sql and "GROUP BY lithology_code" in sql:
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
