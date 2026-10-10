"""A hole listed twice in one collar file keeps its FIRST row, and says so.

WHY THIS FILE EXISTS (audit finding 6)
    ``_COLLAR_SQL`` upserts on ``(project_id, hole_id_canonical)``. Two rows
    for one hole in the same file are therefore two upserts onto one collar,
    and the LAST silently replaced the first's position and depth - no
    warning, because ``hole_id_canonical_collision`` only fires for different
    SPELLINGS. Which row is right cannot be known from the file, so the first
    is kept deliberately and each repeat is reported with both rows' positions.

    Two layers, tested separately: the CSV parser (``csv_collar``) reports
    from the file itself; the writer (``_write_collars``) is the choke point
    for every source, including ones that never pass through the parser.
"""
from __future__ import annotations

import io
from contextlib import asynccontextmanager
from typing import Any

from georag_geoparsers.csv_collar import parse_csv_collars

from app.hatchet_workflows.ingest_tabular import _write_collars
from app.services.ingest.silver_row_guard import RowIssues, issue_warnings

WS = "11111111-1111-1111-1111-111111111111"
PROJECT = "22222222-2222-2222-2222-222222222222"

HEADER = "HoleID,Easting,Northing,Elevation,Depth\n"


class _Conn:
    def __init__(self) -> None:
        self.executemany_calls: list[tuple[str, list]] = []

    async def executemany(self, sql: str, rows: list) -> None:
        self.executemany_calls.append((sql, list(rows)))

    async def fetch(self, sql: str, *args: Any) -> list:
        return []

    def transaction(self):  # noqa: ANN201 - mirrors asyncpg
        @asynccontextmanager
        async def _txn():
            yield self

        return _txn()

    @property
    def rows(self) -> list:
        return [row for _sql, rows in self.executemany_calls for row in rows]


def _rec(hole: str, row: int, easting: float, northing: float, **extra: Any) -> dict:
    return {
        "hole_id": hole, "easting": easting, "northing": northing,
        "elevation": 300.0, "total_depth": 150.0, "_source_row": row, **extra,
    }


class TestTheCsvParser:
    CONFLICTING = (
        HEADER
        + "DH-1,500000,6000000,300,150\n"
        + "DH-2,500100,6000100,310,150\n"
        + "DH-1,500999,6000999,305,160\n"
    )

    def test_the_first_row_is_kept_and_the_repeat_is_not_valid(self) -> None:
        result = parse_csv_collars(io.StringIO(self.CONFLICTING))

        assert [(r["hole_id"], r["easting"]) for r in result.records] == [
            ("DH-1", 500000.0), ("DH-2", 500100.0),
        ]
        assert (result.total_rows, result.valid_rows, result.skipped_rows) == (3, 2, 1)

    def test_the_skip_names_the_rows_and_the_coordinates(self) -> None:
        result = parse_csv_collars(io.StringIO(self.CONFLICTING))

        (skip,) = result.skipped_details
        assert skip["code"] == "duplicate_hole_id"
        assert skip["row"] == 4 and skip["actual"] == {"first_row": 2}
        assert skip["raw"] == {"hole_id": "DH-1", "easting": 500999.0, "northing": 6000999.0}

    def test_the_warning_quotes_both_positions_and_is_blocking(self) -> None:
        result = parse_csv_collars(io.StringIO(self.CONFLICTING))

        warning = next(w for w in result.warnings if w["code"] == "duplicate_hole_id")
        assert "E 500999, N 6000999" in warning["detail"]
        assert "E 500000, N 6000000" in warning["detail"]
        assert "differs" in warning["detail"]
        assert warning["context"]["conflicting"] == 1
        assert warning.get("severity") != "info"

    def test_an_exact_repeat_is_reported_as_information_only(self) -> None:
        text = HEADER + "DH-1,500000,6000000,300,150\nDH-1,500000,6000000,300,150\n"
        result = parse_csv_collars(io.StringIO(text))

        assert result.valid_rows == 1
        warning = next(w for w in result.warnings if w["code"] == "duplicate_hole_id")
        assert warning["severity"] == "info"
        assert "nothing was lost" in warning["detail"]

    def test_a_different_spelling_of_the_same_hole_is_a_repeat_too(self) -> None:
        text = HEADER + "DH-1,500000,6000000,300,150\ndh_1,500999,6000999,305,160\n"
        result = parse_csv_collars(io.StringIO(text))

        assert [r["hole_id"] for r in result.records] == ["DH-1"]
        assert result.skipped_details[0]["code"] == "duplicate_hole_id"

    def test_distinct_holes_are_untouched(self) -> None:
        text = HEADER + "DH-1,500000,6000000,300,150\nDH-2,500100,6000100,310,150\n"
        result = parse_csv_collars(io.StringIO(text))

        assert result.valid_rows == 2
        assert "duplicate_hole_id" not in {w["code"] for w in result.warnings}

    def test_a_repeat_of_a_row_the_parser_rejected_does_not_count(self) -> None:
        """The first DH-1 has no easting and was rejected; the second is the
        only usable row for the hole and must survive."""
        text = HEADER + "DH-1,,6000000,300,150\nDH-1,500000,6000000,300,150\n"
        result = parse_csv_collars(io.StringIO(text))

        assert [r["easting"] for r in result.records] == [500000.0]
        assert "duplicate_hole_id" not in {w["code"] for w in result.warnings}


class TestTheWriter:
    async def test_the_second_row_for_a_hole_is_not_upserted(self) -> None:
        conn = _Conn()
        issues = RowIssues()

        stats = await _write_collars(
            conn, workspace_id=WS, project_id=PROJECT, epsg=32613,
            georef_method="declared", issues=issues,
            records=[
                _rec("DH-1", 2, 500000.0, 6000000.0),
                _rec("DH-2", 3, 500100.0, 6000100.0),
                _rec("DH-1", 4, 500999.0, 6000999.0),
            ],
        )

        sent = {row[2]: row for row in conn.rows}
        assert sorted(sent) == ["DH-1", "DH-2"]
        assert sent["DH-1"][4] == 500000.0          # the FIRST row's easting
        assert stats["written"] == 2 and stats["skipped"] == 1

    async def test_it_is_reported_with_both_rows_in_the_run_warnings(self) -> None:
        issues = RowIssues()
        await _write_collars(
            _Conn(), workspace_id=WS, project_id=PROJECT, epsg=32613,
            georef_method="declared", issues=issues,
            records=[
                _rec("DH-1", 2, 500000.0, 6000000.0),
                _rec("dh 1", 5, 500999.0, 6000999.0),
            ],
        )

        (dup,) = issues.duplicates
        assert (dup["row"], dup["first_row"], dup["identical"]) == (5, 2, False)
        warnings = issue_warnings(issues, label="collars.csv", table="collar")
        (note,) = [w for w in warnings if w["code"] == "duplicate_hole_id"]
        assert "row 5" in note["detail"] and "repeats row 2" in note["detail"]
        assert "collars.csv" in note["message"]
        # And it is folded into the parser-style skipped details, so the
        # rows_rejected summary counts it.
        assert [d["code"] for d in issues.skipped_details()] == ["duplicate_hole_id"]

    async def test_a_hole_whose_first_row_the_guard_rejected_is_written_from_the_second(self) -> None:
        conn = _Conn()
        issues = RowIssues()
        no_easting = _rec("DH-1", 2, 0.0, 6000000.0)
        no_easting["easting"] = None

        stats = await _write_collars(
            conn, workspace_id=WS, project_id=PROJECT, epsg=32613,
            georef_method="declared", issues=issues,
            records=[no_easting, _rec("DH-1", 3, 500000.0, 6000000.0)],
        )

        assert stats["written"] == 1
        assert conn.rows[0][4] == 500000.0
        assert issues.duplicates == []

    async def test_clean_files_report_nothing(self) -> None:
        issues = RowIssues()
        await _write_collars(
            _Conn(), workspace_id=WS, project_id=PROJECT, epsg=32613,
            georef_method="declared", issues=issues,
            records=[_rec("DH-1", 2, 1.0, 1.0), _rec("DH-2", 3, 2.0, 2.0)],
        )
        assert issues.duplicates == [] and not issues
        assert issue_warnings(issues, label="x", table="collar") == []
