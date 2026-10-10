"""Assays without a sample id still land, and no row claims a QA/QC it never had.

WHY THIS FILE EXISTS (audit findings 1 and 12)
    1. ``derive_assay_v2_rows`` dropped every element value of a sample row
       that had no sample id (``silver.assays_v2.sample_id`` is NOT NULL). An
       interval-composite file ("Hole, From, To, Au_ppm") has no sample column
       at all, so ALL its assays stayed out of the one table every assay
       reader queries. The skip count went to ``stats["assay_rows_skipped"]``,
       which nothing read. The row now lands under an id derived from its hole
       and interval, and the run says so (``assay_sample_id_derived``); values
       the table really cannot hold (a negative) are named in
       ``assay_values_skipped``.
    2. ``assays_v2.qaqc_flag`` defaulted to 'pass' and the writer never set it,
       so nl_summaries rendered "QA/QC: pass" for every assay. The writer now
       sets it: NULL unless the sample is a control (Blank / Standard /
       Duplicate), and the default is dropped by a reversible migration.

    No database: ``_Conn`` records what the writer sends.
"""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from app.hatchet_workflows import ingest_tabular as it
from app.services.ingest.silver_row_guard import RowIssues, issue_warnings

WS = "a0000000-0000-0000-0000-000000000001"
COLLAR = "b0000000-0000-0000-0000-000000000002"
INDEX = {"D1": COLLAR}

REPO = Path(__file__).resolve().parents[3]


class _Conn:
    """Records the rows the writer inserts, per table."""

    def __init__(self) -> None:
        self.inserted: dict[str, list[tuple]] = {}

    async def executemany(self, sql: str, rows: list) -> None:
        table = re.search(r"INSERT INTO (silver\.\w+)", sql).group(1)
        self.inserted.setdefault(table, []).extend(tuple(r) for r in rows)

    async def fetchval(self, *_a: Any) -> int:
        return 0

    async def fetch(self, *_a: Any) -> list:
        return []

    def transaction(self):  # noqa: ANN202 - mirrors asyncpg's sync factory
        @asynccontextmanager
        async def _txn():
            yield self

        return _txn()


def _sample(row: int, a: float, b: float, **overrides: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "_source_row": row, "hole_id": "D1", "from_depth": a, "to_depth": b,
        "sample_type": "Core", "lab_id": None, "qaqc_type": "Primary",
        "commodity_assays": {"Au_ppm": 1.5, "Cu_pct": 0.2},
        "commodity_assay_flags": None,
    }
    rec.update(overrides)
    return rec


async def _write(records: list[dict], issues: RowIssues | None = None) -> tuple[_Conn, dict]:
    conn = _Conn()
    stats = await it._write_intervals(
        conn, workspace_id=WS, sheet_type="sample", records=records,
        index=dict(INDEX), issues=issues, source_file="comp.csv",
        source_file_sha256="a" * 64,
    )
    return conn, stats


# Positions in an assays_v2 insert tuple (see _ASSAYS_V2_SQL).
SAMPLE_ID, ELEMENT, QAQC_FLAG = 3, 6, 15


class TestSamplesWithoutAnIdLand:
    @pytest.mark.asyncio
    async def test_an_interval_composite_reaches_assays_v2(self) -> None:
        conn, stats = await _write([_sample(2, 0, 10), _sample(3, 10, 20)])

        rows = conn.inserted["silver.assays_v2"]
        assert len(rows) == 4                       # 2 intervals x (Au, Cu)
        assert stats["assay_rows"] == 4 and stats["assay_rows_skipped"] == 0
        assert {r[SAMPLE_ID] for r in rows} == {
            "D1 0-10 m (no sample id)", "D1 10-20 m (no sample id)",
        }
        assert [r[ELEMENT] for r in rows if r[SAMPLE_ID].startswith("D1 0-")] == ["Au", "Cu"]

    @pytest.mark.asyncio
    async def test_the_derived_id_is_deterministic(self) -> None:
        first, _ = await _write([_sample(2, 0, 10)])
        second, _ = await _write([_sample(2, 0, 10)])

        # Same file, same ids: the row id (a uuid5) and the sample id both.
        assert first.inserted["silver.assays_v2"] == second.inserted["silver.assays_v2"]

    @pytest.mark.asyncio
    async def test_two_rows_for_one_interval_do_not_overwrite_each_other(self) -> None:
        conn, _ = await _write([
            _sample(2, 0, 10, commodity_assays={"Au_ppm": 1.0}),
            _sample(3, 0, 10, commodity_assays={"Au_ppm": 9.0}),
        ])

        rows = conn.inserted["silver.assays_v2"]
        assert [r[SAMPLE_ID] for r in rows] == [
            "D1 0-10 m (no sample id)", "D1 0-10 m (no sample id) #2",
        ]
        assert len({r[0] for r in rows}) == 2          # distinct uuid5 ids
        assert sorted(r[7] for r in rows) == [1.0, 9.0]  # both values kept

    @pytest.mark.asyncio
    async def test_a_real_sample_id_is_never_replaced(self) -> None:
        conn, _ = await _write([_sample(2, 0, 10, sample_id="S-77")])

        assert {r[SAMPLE_ID] for r in conn.inserted["silver.assays_v2"]} == {"S-77"}

    @pytest.mark.asyncio
    async def test_the_run_is_told_with_a_named_info_warning(self) -> None:
        issues = RowIssues()
        await _write([_sample(2, 0, 10), _sample(3, 10, 20, sample_id="S-2")], issues)

        assert len(issues.derived_sample_ids) == 1       # only the row with none
        note = next(
            w for w in issue_warnings(issues, label="comp.csv", table="sample")
            if w["code"] == "assay_sample_id_derived"
        )
        assert note["severity"] == "info"                # keeps the run 'completed'
        assert note["rows"] == 1
        assert "D1 0-10 m (no sample id)" in note["detail"] and "row 2" in note["detail"]
        assert note["message"]

    @pytest.mark.asyncio
    async def test_a_row_with_no_assays_is_not_reported(self) -> None:
        issues = RowIssues()
        conn, _ = await _write(
            [_sample(2, 0, 10, commodity_assays={}, commodity_assay_flags=None)], issues,
        )

        assert "silver.assays_v2" not in conn.inserted
        assert issues.derived_sample_ids == [] and issues.assay_skipped == []


class TestValuesTheTableCannotHoldAreNamed:
    @pytest.mark.asyncio
    async def test_a_negative_value_is_skipped_counted_and_reported(self) -> None:
        issues = RowIssues()
        conn, stats = await _write(
            [_sample(5, 0, 10, sample_id="S-1", commodity_assays={"Au_ppm": -1.0, "Cu_pct": 0.2})],
            issues,
        )

        assert [r[ELEMENT] for r in conn.inserted["silver.assays_v2"]] == ["Cu"]
        assert stats["assay_rows_skipped"] == 1
        note = next(
            w for w in issue_warnings(issues, label="comp.csv", table="sample")
            if w["code"] == "assay_values_skipped"
        )
        assert note["rows"] == 1
        assert "Au_ppm" in note["detail"] and "-1" in note["detail"] and "row 5" in note["detail"]
        assert "silver.samples" in note["detail"]        # where the value still is
        # The sample row itself was written: the primary counts do not move.
        assert stats["written"] == 1 and stats["skipped"] == 0

    @pytest.mark.asyncio
    async def test_nothing_skipped_means_no_warning(self) -> None:
        issues = RowIssues()
        await _write([_sample(2, 0, 10, sample_id="S-1")], issues)

        assert not issues
        assert issue_warnings(issues, label="comp.csv", table="sample") == []


class TestQaqcFlagIsNeverAnInventedPass:
    @pytest.mark.asyncio
    async def test_a_primary_sample_is_not_evaluated(self) -> None:
        conn, _ = await _write([_sample(2, 0, 10, sample_id="S-1")])

        assert {r[QAQC_FLAG] for r in conn.inserted["silver.assays_v2"]} == {None}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("qaqc_type", "expected"), [
        ("Blank", "control_blank"),
        ("Standard", "control_standard"),
        ("Duplicate", "control_duplicate"),
        ("Primary", None),
        (None, None),
    ])
    async def test_control_samples_are_marked_and_none_is_pass(
        self, qaqc_type: str | None, expected: str | None,
    ) -> None:
        conn, _ = await _write([_sample(2, 0, 10, sample_id="S-1", qaqc_type=qaqc_type)])

        flags = {r[QAQC_FLAG] for r in conn.inserted["silver.assays_v2"]}
        assert flags == {expected}
        assert "pass" not in flags

    def test_the_sql_writes_the_column_explicitly_and_it_is_the_row_before_the_source(self) -> None:
        sql = it._ASSAYS_V2_SQL
        columns = re.search(r"\((.*?)\)\s*VALUES", sql, re.S).group(1)

        assert [c.strip() for c in columns.split(",")][-3:] == [
            "qaqc_flag", "source_file", "source_file_sha256",
        ]
        assert "qaqc_flag           = EXCLUDED.qaqc_flag" in sql
        assert "'pass'" not in sql

    def test_the_migration_drops_the_pass_default_and_can_be_reversed(self) -> None:
        migration = next(
            (REPO / "database" / "migrations").glob("*default_assays_v2_qaqc_flag_to_null.php"),
        ).read_text(encoding="utf-8")

        assert "ALTER COLUMN qaqc_flag DROP DEFAULT" in migration
        assert "ALTER COLUMN qaqc_flag SET DEFAULT 'pass'" in migration   # down()
