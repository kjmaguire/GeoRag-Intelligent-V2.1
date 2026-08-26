"""Behavioural tests for ``ingest_tabular``'s persist layer.

WHY THIS FILE EXISTS
    Nothing exercised parse or persist for the three restored geology
    workflows. ``test_progress_callers_signature.py`` binds their
    ``_progress`` calls against the real signature -- a static check that
    caught the ``error_text=`` bug -- and that was the whole of it.

    The writers here decide what a drill upload actually becomes, and
    three of their decisions are the kind that are wrong quietly:

      * a collar with no coordinates is SKIPPED, not written at 0,0.
        Writing zeroes would put every unlocated hole off West Africa,
        on the map, looking like data.
      * an interval upload REPLACES the intervals of the holes it
        mentions. Appending would double assay intervals, which skews a
        composite grade with nothing visible to notice.
      * an interval whose hole was never uploaded is counted as
        ORPHANED, not dropped. That count is the geologist's only signal
        that their assay file referenced a collar file they forgot.

    None of this needs a database: the writers build parameter tuples and
    hand them to asyncpg. A recording fake proves what would be sent.

WHAT THIS FILE DOES NOT COVER
    That the SQL is valid against the real schema, or that ON CONFLICT
    behaves as written. Those need a live Postgres and belong in the
    integration bucket.
"""
from __future__ import annotations

import importlib.util
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.hatchet_workflows.ingest_tabular import (
    _COLLAR_SQL,
    _INSERT_BATCH,
    _INTERVAL_TABLES,
    _num,
    _resolve_collar,
    _write_collars,
    _write_intervals,
)

#: ``_resolve_collar`` imports ``georag_geoparsers._hole_id`` lazily. That
#: package is a PATH dependency: `uv sync` installs it in CI and the Docker
#: build copies it to /georag_geoparsers, but a bare local checkout has it
#: only as a sibling directory. Rather than stub the canonicaliser -- which
#: would leave the EL-001/EL001 reconciliation, the entire point of these
#: tests, asserted against a fake -- skip and say how to run them properly:
#:
#:   PYTHONPATH=".;../georag_geoparsers" python -m pytest tests/test_ingest_tabular_persist.py
#:
#: The collar writer needs none of this, so the skip is scoped to the two
#: classes that resolve hole IDs.
_HAS_GEOPARSERS = importlib.util.find_spec("georag_geoparsers") is not None
needs_geoparsers = pytest.mark.skipif(
    not _HAS_GEOPARSERS,
    reason=(
        "georag_geoparsers path dependency not importable; run with "
        'PYTHONPATH=".;../georag_geoparsers"'
    ),
)

WS = "11111111-1111-1111-1111-111111111111"
PROJECT = "22222222-2222-2222-2222-222222222222"
COLLAR_A = "aaaaaaaa-0000-0000-0000-000000000001"
COLLAR_B = "bbbbbbbb-0000-0000-0000-000000000002"

pytestmark = pytest.mark.asyncio


class FakeConn:
    """Records what a writer would send, without a database.

    ``fetchval_result`` stands in for the DELETE ... RETURNING count so a
    test can say "there were already 7 rows for these holes".
    """

    def __init__(self, fetchval_result: Any = 0,
                 fetch_result: list | None = None) -> None:
        self.executemany_calls: list[tuple[str, list]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchval_result = fetchval_result
        #: What SELECTs return — the sample path reads
        #: silver.element_reference before writing.
        self.fetch_result = fetch_result or []
        self.transactions = 0

    async def executemany(self, sql: str, rows: list) -> None:
        self.executemany_calls.append((sql, list(rows)))

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.fetchval_calls.append((sql, args))
        return self.fetchval_result

    async def fetch(self, sql: str, *args: Any) -> list:
        self.fetch_calls.append((sql, args))
        return self.fetch_result

    def transaction(self):  # noqa: ANN201 - mirrors asyncpg's sync factory
        conn = self

        @asynccontextmanager
        async def _txn():
            conn.transactions += 1
            yield conn

        return _txn()

    @property
    def rows(self) -> list:
        """Every parameter tuple that reached executemany, flattened."""
        return [row for _sql, rows in self.executemany_calls for row in rows]


def collar(**overrides: Any) -> dict:
    base = {
        "hole_id": "EL-001", "hole_id_canonical": "EL001",
        "easting": 495000.0, "northing": 6220000.0, "elevation": 512.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _num
# ---------------------------------------------------------------------------

class TestNum:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1, 1.0), ("1.5", 1.5), (-90, -90.0), ("-1e3", -1000.0),
            (None, None), ("", None), (" ", None),
            ("n/a", None), ("--", None), ([], None), ({}, None),
        ],
    )
    async def test_coerces_or_returns_none(self, value, expected) -> None:
        assert _num(value) == expected

    async def test_zero_is_a_value_not_a_blank(self) -> None:
        """0.0 is falsy in Python and a legitimate elevation at sea level."""
        assert _num(0) == 0.0
        assert _num("0") == 0.0
        assert _num(0) is not None


# ---------------------------------------------------------------------------
# Hole-ID reconciliation
# ---------------------------------------------------------------------------

@needs_geoparsers
class TestResolveCollar:
    """A survey file writes EL-001 where the collar file wrote EL001."""

    def index(self) -> dict[str, str]:
        return {"EL001": COLLAR_A, "DDH-14": COLLAR_B}

    async def test_exact_match(self) -> None:
        assert _resolve_collar(self.index(), "EL001") == COLLAR_A

    async def test_case_and_whitespace_insensitive(self) -> None:
        assert _resolve_collar(self.index(), "  el001 ") == COLLAR_A

    async def test_canonicalises_a_different_spelling(self) -> None:
        """The reason _hole_id.canonicalize exists."""
        assert _resolve_collar(self.index(), "EL-001") == COLLAR_A

    async def test_unknown_hole_is_none_not_an_exception(self) -> None:
        """It must be COUNTED as orphaned, which needs a return not a raise."""
        assert _resolve_collar(self.index(), "ZZ-999") is None

    @pytest.mark.parametrize("blank", [None, "", 0])
    async def test_blank_hole_id_is_none(self, blank) -> None:
        assert _resolve_collar(self.index(), blank) is None


# ---------------------------------------------------------------------------
# Collars
# ---------------------------------------------------------------------------

class TestWriteCollars:
    async def _write(self, conn: FakeConn, records: list[dict]) -> dict[str, int]:
        return await _write_collars(
            conn, workspace_id=WS, project_id=PROJECT, records=records,
            epsg=32613, georef_method="declared",
        )

    async def test_writes_a_complete_collar(self) -> None:
        conn = FakeConn()
        result = await self._write(conn, [collar()])

        assert result == {"written": 1, "skipped": 0, "orphaned": 0}
        assert conn.executemany_calls[0][0] is _COLLAR_SQL
        row = conn.rows[0]
        assert row[0] == WS and row[1] == PROJECT
        assert row[2] == "EL-001" and row[3] == "EL001"
        assert row[4] == 495000.0 and row[5] == 6220000.0

    @pytest.mark.parametrize(
        "missing", ["easting", "northing", "hole_id"],
    )
    async def test_collar_without_coordinates_is_skipped_not_zeroed(
        self, missing: str,
    ) -> None:
        """A collar at 0,0 is in the Gulf of Guinea, drawn as real data.

        NOT NULL on both columns is only half the reason -- the other half
        is that a hole nobody can place is not a collar.
        """
        conn = FakeConn()
        result = await self._write(conn, [collar(**{missing: None})])

        assert result == {"written": 0, "skipped": 1, "orphaned": 0}
        assert conn.rows == []

    async def test_blank_string_coordinate_is_also_skipped(self) -> None:
        """CSVs deliver an empty cell as "", which float() rejects."""
        conn = FakeConn()
        result = await self._write(conn, [collar(easting="")])
        assert result["skipped"] == 1

    async def test_zero_elevation_survives(self) -> None:
        """Distinguish "no value" from "the value is zero"."""
        conn = FakeConn()
        await self._write(conn, [collar(elevation=0)])
        assert conn.rows[0][6] == 0.0

    async def test_good_and_bad_rows_in_one_file(self) -> None:
        """The mixed case is the normal case for a real drill export."""
        conn = FakeConn()
        result = await self._write(conn, [
            collar(hole_id="EL-001"),
            collar(hole_id="EL-002", northing=None),
            collar(hole_id="EL-003"),
        ])

        assert result == {"written": 2, "skipped": 1, "orphaned": 0}
        assert [r[2] for r in conn.rows] == ["EL-001", "EL-003"]

    async def test_defaults_fill_hole_type_and_status(self) -> None:
        conn = FakeConn()
        await self._write(conn, [collar()])
        row = conn.rows[0]
        assert row[10] is not None, "hole_type default"
        assert row[12] is not None, "status default"

    async def test_explicit_values_beat_the_defaults(self) -> None:
        conn = FakeConn()
        await self._write(conn, [collar(hole_type="RC", status="abandoned")])
        row = conn.rows[0]
        assert row[10] == "RC" and row[12] == "abandoned"

    async def test_epsg_and_georef_method_reach_the_geometry(self) -> None:
        """$15 builds both geom and geom_4326; a wrong EPSG moves the hole."""
        conn = FakeConn()
        await self._write(conn, [collar()])
        row = conn.rows[0]
        assert row[13] == "declared"
        assert row[14] == 32613

    async def test_batching_splits_at_the_insert_batch_size(self) -> None:
        conn = FakeConn()
        records = [collar(hole_id=f"EL-{i:04d}") for i in range(_INSERT_BATCH + 7)]

        result = await self._write(conn, records)

        assert result["written"] == _INSERT_BATCH + 7
        assert [len(rows) for _sql, rows in conn.executemany_calls] == [
            _INSERT_BATCH, 7]

    async def test_empty_input_writes_nothing(self) -> None:
        conn = FakeConn()
        result = await self._write(conn, [])
        assert result == {"written": 0, "skipped": 0, "orphaned": 0}
        assert conn.executemany_calls == []


# ---------------------------------------------------------------------------
# Intervals
# ---------------------------------------------------------------------------

@needs_geoparsers
class TestWriteIntervals:
    INDEX = {"EL001": COLLAR_A, "EL002": COLLAR_B}

    async def _write(self, conn: FakeConn, sheet_type: str,
                     records: list[dict]) -> dict[str, int]:
        return await _write_intervals(
            conn, workspace_id=WS, sheet_type=sheet_type,
            records=records, index=dict(self.INDEX),
        )

    async def test_survey_rows_carry_collar_and_depth(self) -> None:
        conn = FakeConn()
        result = await self._write(conn, "survey", [
            {"hole_id": "EL001", "depth": 50.0, "azimuth": 135.0, "dip": -60.0},
        ])

        assert result["written"] == 1 and result["orphaned"] == 0
        row = conn.rows[0]
        assert row[0] == WS and row[1] == COLLAR_A
        assert row[2] == 50.0 and row[3] == 135.0 and row[4] == -60.0

    async def test_survey_method_defaults_rather_than_nulling(self) -> None:
        conn = FakeConn()
        await self._write(conn, "survey", [{"hole_id": "EL001", "depth": 1}])
        assert conn.rows[0][5] is not None

    async def test_lithology_row_shape(self) -> None:
        conn = FakeConn()
        await self._write(conn, "lithology", [{
            "hole_id": "EL001", "from_depth": 0, "to_depth": 12.5,
            "lithology_code": "GRN", "rqd": 88,
        }])
        row = conn.rows[0]
        assert row[1] == COLLAR_A
        assert row[2] == 0.0 and row[3] == 12.5
        assert row[4] == "GRN"
        assert row[9] == 88.0

    async def test_sample_row_shape(self) -> None:
        conn = FakeConn()
        await self._write(conn, "sample", [{
            "hole_id": "EL002", "from_depth": 10, "to_depth": 11,
            "lab_id": "SGS-771", "qaqc_type": "blank",
        }])
        row = conn.rows[0]
        assert row[1] == COLLAR_B
        assert row[4] is not None, "sample_type default"
        assert row[5] == "SGS-771" and row[6] == "blank"

    async def test_orphaned_interval_is_counted_never_dropped(self) -> None:
        """An assay for a hole nobody uploaded is a completeness gap.

        Silently discarding it is how a geologist ends up trusting a
        composite that is missing a third of its intervals.
        """
        conn = FakeConn()
        result = await self._write(conn, "sample", [
            {"hole_id": "EL001", "from_depth": 0, "to_depth": 1},
            {"hole_id": "MISSING-1", "from_depth": 0, "to_depth": 1},
            {"hole_id": "MISSING-2", "from_depth": 0, "to_depth": 1},
        ])

        assert result["written"] == 1
        assert result["orphaned"] == 2

    async def test_upload_replaces_only_the_holes_it_mentions(self) -> None:
        """Re-uploading a corrected log for EL001 must not empty EL002."""
        conn = FakeConn(fetchval_result=7)
        result = await self._write(conn, "lithology", [
            {"hole_id": "EL001", "from_depth": 0, "to_depth": 5},
        ])

        assert result["replaced"] == 7
        (sql, args), = conn.fetchval_calls
        assert "DELETE FROM" in sql
        assert _INTERVAL_TABLES["lithology"] in sql
        assert args[0] == [COLLAR_A], (
            "the delete must be scoped to the collars in THIS file")

    async def test_delete_and_insert_share_one_transaction(self) -> None:
        """Otherwise a failed insert leaves the holes emptied."""
        conn = FakeConn(fetchval_result=3)
        await self._write(conn, "survey", [{"hole_id": "EL001", "depth": 1}])
        assert conn.transactions == 1

    async def test_nothing_resolvable_deletes_nothing(self) -> None:
        """A file of pure orphans must not empty the table.

        `collar_id = ANY('{}')` matches nothing, but reaching the query at
        all with an empty array is a foot-gun worth pinning shut.
        """
        conn = FakeConn(fetchval_result=999)
        result = await self._write(conn, "survey", [
            {"hole_id": "NOBODY", "depth": 1},
        ])

        assert conn.fetchval_calls == []
        assert result == {
            "written": 0, "skipped": 0, "orphaned": 1, "replaced": 0}

    async def test_replaced_count_is_reported_not_hidden(self) -> None:
        """Splitting one hole across two files loses the first file.

        That is a real workflow and a real cost, so the number is
        surfaced rather than swallowed -- it is the only way the geologist
        finds out.
        """
        conn = FakeConn(fetchval_result=42)
        result = await self._write(conn, "sample", [
            {"hole_id": "EL001", "from_depth": 0, "to_depth": 1},
        ])
        assert result["replaced"] == 42

    async def test_batching_splits_at_the_insert_batch_size(self) -> None:
        conn = FakeConn()
        records = [
            {"hole_id": "EL001", "depth": float(i)}
            for i in range(_INSERT_BATCH + 3)
        ]

        result = await self._write(conn, "survey", records)

        assert result["written"] == _INSERT_BATCH + 3
        assert [len(rows) for _sql, rows in conn.executemany_calls] == [
            _INSERT_BATCH, 3]

    async def test_every_interval_table_is_reachable(self) -> None:
        """Guards the guard: a sheet_type with no table entry KeyErrors."""
        for sheet_type in _INTERVAL_TABLES:
            conn = FakeConn()
            result = await self._write(conn, sheet_type, [
                {"hole_id": "EL001", "depth": 1, "from_depth": 0, "to_depth": 1},
            ])
            assert result["written"] == 1, sheet_type

    async def test_sample_sheet_lands_assays_v2_in_the_same_transaction(self) -> None:
        """The canonical assay table is written WITH the sample, not later.

        Until 2026-08-25 the parsed commodity_assays dict was dropped on
        write and silver.assays_v2 had no live writer at all — every
        assay-side reader ran against a permanently empty table.
        """
        conn = FakeConn(fetchval_result=0)
        result = await self._write(conn, "sample", [{
            "hole_id": "EL001", "sample_id": "S-1", "from_depth": 0.0,
            "to_depth": 1.0, "commodity_assays": {"Au_ppb": 500.0},
        }])

        assert result["assay_rows"] == 1
        assay_calls = [
            (sql, rows) for sql, rows in conn.executemany_calls
            if "silver.assays_v2" in sql
        ]
        assert len(assay_calls) == 1
        assert assay_calls[0][1][0][6:9] == ("Au", 500.0, "ppb")
        assert conn.transactions == 1

    async def test_sample_sheet_replaces_its_holes_assays_too(self) -> None:
        """A corrected file must not double its holes' element rows."""
        conn = FakeConn(fetchval_result=5)
        result = await self._write(conn, "sample", [{
            "hole_id": "EL001", "sample_id": "S-1", "from_depth": 0.0,
            "to_depth": 1.0, "commodity_assays": {"Au_ppb": 500.0},
        }])

        assert result["assay_replaced"] == 5
        deletes = [sql for sql, _ in conn.fetchval_calls]
        assert any("silver.assays_v2" in sql for sql in deletes)

    async def test_sample_row_carries_the_commodity_assays_payload(self) -> None:
        """The parser extracts it; the writer must not drop it again."""
        conn = FakeConn()
        await self._write(conn, "sample", [{
            "hole_id": "EL001", "from_depth": 0.0, "to_depth": 1.0,
            "commodity_assays": {"Au_ppb": 500.0},
            "commodity_assay_flags": {"Au_ppb": {"dl_flag": True}},
        }])
        sample_rows = [
            rows for sql, rows in conn.executemany_calls
            if "silver.samples" in sql
        ]
        row = sample_rows[0][0]
        assert json.loads(row[7]) == {"Au_ppb": 500.0}
        assert json.loads(row[8]) == {"Au_ppb": {"dl_flag": True}}
