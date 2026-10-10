"""Surface geochemistry records where each row came from, and says when a file
replaces another file's samples (audit finding 21).

``silver.geochemistry`` is unique on ``(project_id, sample_id)``. ingest_tabular
upserted into it with no lineage at all, so a second file that reused a sample
number replaced the first file's row and nothing - not the row, not the run -
said so. Migration 2026_10_10_110000 adds ``source_file`` /
``source_file_sha256`` / ``row_index``; the writer fills them and reports the
cross-file replacement.

The live-database half is in test_ingest_tabular_upsert_integration.py.
"""
from __future__ import annotations

import re
from typing import Any

import pytest

from app.hatchet_workflows import ingest_tabular as it

SHAPE = {
    "located": {"sample_id": "Sample", "easting": "X", "northing": "Y"},
    "assays": {"au_ppm": "Au_ppm", "cu_ppm": "Cu_ppm", "as_ppm": "As_ppm"},
}

# Positions in a _GEOCHEM_SQL parameter tuple.
SAMPLE_ID, SOURCE_FILE, SOURCE_SHA, ROW_INDEX = 2, 9, 10, 11


def _row(sample: str | None, x: float = 394000.0, y: float = 6215000.0) -> dict[str, Any]:
    return {"Sample": sample, "X": x, "Y": y, "Au_ppm": 0.1, "Cu_ppm": 20.0, "As_ppm": 5.0}


class _Conn:
    def __init__(self, earlier: list[dict[str, Any]] | None = None) -> None:
        self.earlier = earlier or []
        self.inserted: list[tuple] = []
        self.fetches: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetches.append((sql, args))
        return list(self.earlier)

    async def executemany(self, _sql: str, rows: list) -> None:
        self.inserted.extend(tuple(r) for r in rows)


async def _write(conn: _Conn, rows: list[dict[str, Any]], **kwargs: Any) -> tuple[dict, list[dict]]:
    warnings: list[dict[str, Any]] = []
    stats = await it._write_surface_geochem(
        conn,  # type: ignore[arg-type]
        workspace_id="w", project_id="p", shape=SHAPE, rows=rows, source_epsg=26913,
        warnings_out=warnings, **kwargs,
    )
    return stats, warnings


class TestLineageIsWritten:
    @pytest.mark.asyncio
    async def test_each_row_carries_its_file_hash_and_position(self) -> None:
        conn = _Conn()

        stats, _ = await _write(
            conn, [_row("S1"), _row(None), _row("S3")],     # the blank-sample row is skipped
            source_file="soils.dbf", source_file_sha256="a" * 64,
        )

        assert stats == {"written": 2, "skipped": 1, "orphaned": 0}
        assert [(r[SAMPLE_ID], r[SOURCE_FILE], r[SOURCE_SHA], r[ROW_INDEX]) for r in conn.inserted] == [
            ("S1", "soils.dbf", "a" * 64, 0),
            ("S3", "soils.dbf", "a" * 64, 2),               # the position in the SOURCE, skips included
        ]

    @pytest.mark.asyncio
    async def test_without_a_source_nothing_is_read_and_the_columns_are_null(self) -> None:
        conn = _Conn()

        _, warnings = await _write(conn, [_row("S1")])

        assert conn.fetches == [] and warnings == []
        assert conn.inserted[0][SOURCE_FILE:] == (None, None, 0)

    def test_the_sql_writes_and_refreshes_the_lineage(self) -> None:
        sql = it._GEOCHEM_SQL

        assert re.search(r"source_file, source_file_sha256, row_index\s*\)\s*VALUES", sql)
        assert "$12" in sql and "$13" not in sql
        for column in ("source_file", "source_file_sha256", "row_index"):
            assert f"{column}" in sql.split("DO UPDATE SET", 1)[1]

    def test_the_migration_adds_nullable_columns_and_can_be_reversed(self) -> None:
        from pathlib import Path

        migration = next(
            (Path(__file__).resolve().parents[3] / "database" / "migrations").glob(
                "*add_lineage_to_silver_geochemistry.php",
            ),
        ).read_text(encoding="utf-8")

        for column in ("source_file text", "source_file_sha256 varchar(64)", "row_index integer"):
            assert f"ADD COLUMN IF NOT EXISTS {column}" in migration
        assert "NOT NULL" not in migration
        for column in ("row_index", "source_file_sha256", "source_file"):
            assert f"DROP COLUMN IF EXISTS {column}" in migration


class TestReplacingAnotherFilesSamples:
    @pytest.mark.asyncio
    async def test_a_reused_sample_number_from_another_file_is_reported(self) -> None:
        conn = _Conn(earlier=[
            {"sample_id": "S1", "source_file": "north.dbf"},
            {"sample_id": "S2", "source_file": "north.dbf"},
        ])

        stats, warnings = await _write(
            conn, [_row("S1"), _row("S2"), _row("S3")],
            source_file="south.dbf", source_file_sha256="b" * 64,
        )

        assert stats["written"] == 3                       # the upsert still happens
        (note,) = warnings
        assert note["code"] == "geochemistry_sample_replaced_from_other_file"
        assert "2 sample number(s)" in note["detail"]
        assert "'S1' (from 'north.dbf')" in note["detail"] and "south.dbf" in note["detail"]
        assert note["samples"] == ["S1", "S2"]
        assert note["message"]

    @pytest.mark.asyncio
    async def test_the_same_file_uploaded_again_is_silent(self) -> None:
        conn = _Conn(earlier=[{"sample_id": "S1", "source_file": "Soils.DBF"}])

        _, warnings = await _write(
            conn, [_row("S1")], source_file="soils.dbf", source_file_sha256="c" * 64,
        )

        assert warnings == []

    @pytest.mark.asyncio
    async def test_a_row_with_no_recorded_source_is_not_called_another_file(self) -> None:
        conn = _Conn(earlier=[{"sample_id": "S1", "source_file": None}])

        _, warnings = await _write(
            conn, [_row("S1")], source_file="soils.dbf", source_file_sha256="d" * 64,
        )

        assert warnings == []

    @pytest.mark.asyncio
    async def test_unknown_sources_are_counted_inside_the_warning_when_there_is_one(self) -> None:
        conn = _Conn(earlier=[
            {"sample_id": "S1", "source_file": "north.dbf"},
            {"sample_id": "S2", "source_file": None},
            {"sample_id": "S3", "source_file": None},
        ])

        _, warnings = await _write(
            conn, [_row("S1"), _row("S2"), _row("S3")],
            source_file="south.dbf", source_file_sha256="e" * 64,
        )

        (note,) = warnings
        assert "1 sample number(s)" in note["detail"]
        assert "2 further replaced row(s) were written before the source file was recorded" in note["detail"]

    @pytest.mark.asyncio
    async def test_only_this_files_sample_numbers_are_looked_up(self) -> None:
        conn = _Conn()

        await _write(
            conn, [_row("S2"), _row("S1"), _row("S1")],
            source_file="soils.dbf", source_file_sha256="f" * 64,
        )

        _sql, args = conn.fetches[0]
        assert args == ("p", ["S1", "S2"])
