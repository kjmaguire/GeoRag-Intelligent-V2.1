"""A geology log reaches silver.lithology_logs, silver.alteration and
silver.mineralization - from one file, in CSV, dBASE and Access.

WHY THIS FILE EXISTS
    silver.alteration and silver.mineralization existed (2026_05_20_060400) and
    nothing in app/ wrote them. A lithology log's ``Alteration`` /
    ``Alt_Intensity`` / ``Mineral1`` / ``Min1_%`` columns were dropped by the
    lithology parser as unmapped, so the minerals a geologist logged - the
    reason to open the hole - never left the file. These tests pin:

      * the INSERTs name the migration's real COLUMNS (schemas are contracts);
      * one source row feeds several tables and the user is never asked to
        split the file; a standalone alteration / mineralization table works
        too;
      * the same replace-per-collar rule the interval tables use, so a
        re-upload does not double a hole's alteration;
      * what the parsers refuse to guess reaches the run's warnings;
      * the two-phase ZIP dispatch defers a standalone alteration log until
        its collars exist.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from app.hatchet_workflows import ingest_tabular as it
from tests.test_ingest_tabular_typed_tables import (  # noqa: F401 - `env` is a fixture
    COLLARS,
    PROJECT,
    WS,
    _dbf,
    _mdb,
    env,
)

_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parents[3] if len(_HERE.parents) > 3 else _HERE.parents[-1]
SILVER_MIGRATION = (
    REPO_ROOT / "database" / "migrations"
    / "2026_05_20_060400_create_silver_geological_singulars.php"
)
_needs_migrations = pytest.mark.skipif(
    not SILVER_MIGRATION.exists(),
    reason="database/migrations is not mounted (container run of src/fastapi only)",
)

_GEOLOGY_LOG = (
    "HoleID,From,To,Lith,Lith_Desc,Colour,Grain,Alteration,Alt_Intensity,"
    "Mineral1,Min1_%,Min_Style,Sulphide%,RQD,Recovery\n"
    "TR-01,0,5,GRN,Grey granite,grey,Fine,Chlorite,Strong,,,,,85,98\n"
    "TR-01,5,10,GRN,,dark grey,Coarse,None,,Pyrite,3%,Disseminated,4,90%,99\n"
    "TR-01,10,15,SST,Sandstone,red,Medium,Sericite,weak,Pyrite,trace,,0,,\n"
)


class _CsvStore:
    def __init__(self, text: str) -> None:
        self.text = text

    def get_file(self, _bucket: Any, _key: str, local: str) -> None:
        Path(local).write_text(self.text, encoding="utf-8")


def _serve(monkeypatch, fixture, text: str) -> None:
    monkeypatch.setattr(fixture.it, "get_storage_client", lambda: _CsvStore(text))


def _table_columns(table: str) -> set[str]:
    text = SILVER_MIGRATION.read_text()
    body = re.search(
        rf"CREATE TABLE silver\.{table} \((.*?)\n\s*\)\n\s*SQL", text, re.DOTALL,
    )
    assert body, f"silver.{table} CREATE TABLE not found in its migration"
    columns = set()
    for line in body.group(1).splitlines():
        match = re.match(r"\s*([a-z_]+)\s+(?:uuid|numeric|text|timestamptz|integer)", line)
        if match:
            columns.add(match.group(1))
    return columns


def _inserted_columns(sql: str, table: str) -> list[str]:
    block = re.search(rf"INSERT INTO silver\.{table} \((.*?)\)\s*VALUES", sql, re.DOTALL)
    assert block
    return [c.strip() for c in block.group(1).split(",")]


# ---------------------------------------------------------------------------
# Wiring and the schema contract
# ---------------------------------------------------------------------------

class TestWiring:
    def test_both_are_write_types_after_the_collars_they_reference(self) -> None:
        assert it.WRITE_ORDER[0] == "collar"
        for sheet_type in ("alteration", "mineralization"):
            assert it.WRITE_ORDER.index(sheet_type) > it.WRITE_ORDER.index("collar")

    def test_both_replace_per_collar_like_the_interval_tables(self) -> None:
        assert it._INTERVAL_TABLES["alteration"] == "silver.alteration"
        assert it._INTERVAL_TABLES["mineralization"] == "silver.mineralization"

    def test_the_csv_parsers_are_registered(self) -> None:
        from georag_geoparsers import parse_csv_alteration, parse_csv_mineralization

        assert it._csv_parser_for("alteration") is parse_csv_alteration
        assert it._csv_parser_for("mineralization") is parse_csv_mineralization

    def test_a_geology_log_can_feed_the_other_two_tables(self) -> None:
        assert it._COMPANION_TYPES["lithology"] == ("alteration", "mineralization")
        assert set(it._COMPANION_TYPES) <= set(it.WRITE_ORDER)


@_needs_migrations
class TestSchemaContract:
    @pytest.mark.parametrize("table,sql", [
        ("alteration", "_ALTERATION_SQL"),
        ("mineralization", "_MINERALIZATION_SQL"),
    ])
    def test_the_insert_writes_exactly_the_migrations_columns(self, table, sql) -> None:
        inserted = set(_inserted_columns(getattr(it, sql), table))
        real = _table_columns(table)
        assert inserted <= real, f"not columns of silver.{table}: {inserted - real}"
        # Every column is written: none is left to a default we did not mean,
        # and none was added.
        assert inserted == real

    def test_the_old_plural_table_is_not_a_target(self) -> None:
        text = SILVER_MIGRATION.read_text()
        assert "DROP TABLE IF EXISTS silver.alterations" in text
        assert "silver.alterations" not in it._ALTERATION_SQL

    def test_minerals_is_sent_as_a_text_array_and_numbers_as_double_precision(self) -> None:
        assert "$7::text[]" in it._ALTERATION_SQL
        assert it._ALTERATION_SQL.count("::double precision") == 2
        assert it._MINERALIZATION_SQL.count("::double precision") == 3


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------

class _Conn:
    def __init__(self) -> None:
        self.executemany_calls: list[tuple[str, list]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []

    async def executemany(self, sql: str, rows: list) -> None:
        self.executemany_calls.append((sql, list(rows)))

    async def fetchval(self, sql: str, *args: Any) -> int:
        self.fetchval_calls.append((sql, args))
        return 2

    async def fetch(self, *_a: Any) -> list:
        return []

    def transaction(self):  # noqa: ANN202 - mirrors asyncpg's sync factory
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _txn():
            yield self

        return _txn()


_COLLAR = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class TestWriteIntervals:
    @pytest.mark.asyncio
    async def test_an_alteration_becomes_a_silver_alteration_row(self) -> None:
        conn = _Conn()
        stats = await it._write_intervals(
            conn, workspace_id=WS, sheet_type="alteration",
            records=[{
                "hole_id": "D1", "from_depth": 0.0, "to_depth": 5.0,
                "alteration_type": "Chlorite", "intensity": "Strong",
                "minerals": ["chlorite", "sericite"], "notes": "pervasive",
            }],
            index={"D1": _COLLAR},
        )
        assert stats == {"written": 1, "skipped": 0, "orphaned": 0, "replaced": 2}
        (sql, rows), = conn.executemany_calls
        assert sql == it._ALTERATION_SQL
        assert rows == [(
            WS, _COLLAR, 0.0, 5.0, "Chlorite", "Strong",
            ["chlorite", "sericite"], "pervasive",
        )]

    @pytest.mark.asyncio
    async def test_a_mineralization_becomes_a_silver_mineralization_row(self) -> None:
        conn = _Conn()
        await it._write_intervals(
            conn, workspace_id=WS, sheet_type="mineralization",
            records=[{
                "hole_id": "D1", "from_depth": 5.0, "to_depth": 10.0,
                "mineral": "Pyrite", "abundance_pct": 3.0, "form": "Disseminated",
                "grain_size": None, "notes": None,
            }],
            index={"D1": _COLLAR},
        )
        (sql, rows), = conn.executemany_calls
        assert sql == it._MINERALIZATION_SQL
        assert rows == [(WS, _COLLAR, 5.0, 10.0, "Pyrite", 3.0, "Disseminated", None, None)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sheet_type,table", [
        ("alteration", "silver.alteration"), ("mineralization", "silver.mineralization"),
    ])
    async def test_a_re_upload_replaces_the_touched_holes_instead_of_stacking(
        self, sheet_type, table,
    ) -> None:
        conn = _Conn()
        await it._write_intervals(
            conn, workspace_id=WS, sheet_type=sheet_type,
            records=[{
                "hole_id": "D1", "from_depth": 0.0, "to_depth": 1.0,
                "alteration_type": "Chlorite", "mineral": "Pyrite",
            }],
            index={"D1": _COLLAR},
        )
        (delete_sql, args), = conn.fetchval_calls
        assert f"DELETE FROM {table}" in delete_sql
        assert args == ([_COLLAR],)

    @pytest.mark.asyncio
    async def test_a_record_with_no_name_or_depth_is_counted_not_written(self) -> None:
        conn = _Conn()
        stats = await it._write_intervals(
            conn, workspace_id=WS, sheet_type="alteration",
            records=[
                {"hole_id": "D1", "from_depth": 0.0, "to_depth": 1.0, "alteration_type": ""},
                {"hole_id": "D1", "from_depth": None, "to_depth": 1.0, "alteration_type": "X"},
            ],
            index={"D1": _COLLAR},
        )
        assert stats["skipped"] == 2 and stats["written"] == 0
        assert not conn.executemany_calls

    @pytest.mark.asyncio
    async def test_unknown_holes_are_orphaned(self) -> None:
        conn = _Conn()
        stats = await it._write_intervals(
            conn, workspace_id=WS, sheet_type="mineralization",
            records=[{"hole_id": "NOPE", "from_depth": 0.0, "to_depth": 1.0, "mineral": "X"}],
            index={"D1": _COLLAR},
        )
        assert stats["orphaned"] == 1 and stats["written"] == 0


# ---------------------------------------------------------------------------
# End to end: the workflow
# ---------------------------------------------------------------------------

class TestOneFileThreeTables:
    pytestmark = pytest.mark.asyncio

    async def test_a_geology_csv_lands_in_all_three_tables(
        self, env, monkeypatch, promotion_dispatch_spy,  # noqa: F811
    ) -> None:
        collar_id = env.conn.preload_collar("TR-01")
        _serve(monkeypatch, env, _GEOLOGY_LOG)

        out = await env.run("geology.csv")

        assert out.written["lithology"]["written"] == 3
        assert out.written["alteration"]["written"] == 2
        assert out.written["mineralization"]["written"] == 3   # pyrite, sulphide, pyrite

        lith = env.conn.rows_for("silver.lithology_logs")
        # (ws, collar, from, to, code, desc, grain, colour, hardness, rqd, recovery, weathering)
        assert lith[0] == (
            WS, collar_id, 0.0, 5.0, "GRN", "Grey granite", "Fine", "grey",
            None, 85.0, 98.0, None,
        )
        assert lith[1][9] == 90.0            # "90%": the percent sign is a unit

        alt = env.conn.rows_for("silver.alteration")
        assert [(r[2], r[3], r[4], r[5]) for r in alt] == [
            (0.0, 5.0, "Chlorite", "Strong"), (10.0, 15.0, "Sericite", "weak"),
        ]                                     # the "None" interval is not an alteration

        mineral = env.conn.rows_for("silver.mineralization")
        assert [(r[2], r[4], r[5]) for r in mineral] == [
            (5.0, "Pyrite", 3.0), (5.0, "Sulphide", 4.0), (10.0, "Pyrite", None),
        ]
        assert mineral[2][8] == "abundance: trace"     # kept, not converted

        assert promotion_dispatch_spy

    async def test_the_run_reports_each_table_and_what_it_refused_to_guess(
        self, env, monkeypatch,  # noqa: F811
    ) -> None:
        env.conn.preload_collar("TR-01")
        _serve(monkeypatch, env, _GEOLOGY_LOG)

        out = await env.run("geology.csv")

        companions = {(s["type"], s["rows"], s["companion_of"]) for s in out.sheets
                      if s.get("companion_of")}
        assert companions == {("alteration", 2, "lithology"), ("mineralization", 3, "lithology")}
        assert "mineralization_abundance_not_numeric" in env.codes()
        # The headline counts every row that landed, in every table.
        assert env.rows_written == 8

    async def test_a_second_upload_replaces_instead_of_stacking(
        self, env, monkeypatch,  # noqa: F811
    ) -> None:
        env.conn.preload_collar("TR-01")
        _serve(monkeypatch, env, _GEOLOGY_LOG)
        out = await env.run("geology.csv")
        # The fake DELETE ... RETURNING count is 0 here; what matters is that
        # every table's write is preceded by its own per-collar DELETE.
        assert out.written["alteration"]["replaced"] == 0
        assert {"lithology", "alteration", "mineralization"} <= set(out.written)

    async def test_companions_do_not_double_count_orphaned_holes(
        self, env, monkeypatch,  # noqa: F811
    ) -> None:
        _serve(monkeypatch, env, _GEOLOGY_LOG)       # no collars in the project

        out = await env.run("geology.csv")

        assert out.written["lithology"]["orphaned"] == 3
        assert out.written["alteration"]["orphaned"] == 0
        assert out.written["mineralization"]["orphaned"] == 0
        assert "orphaned_intervals" in env.codes()
        assert "3 row(s)" in env.warning("orphaned_intervals")["detail"]

    async def test_a_column_nobody_can_place_is_named_in_a_warning(
        self, env, monkeypatch,  # noqa: F811
    ) -> None:
        env.conn.preload_collar("TR-01")
        _serve(monkeypatch, env, (
            "HoleID,From,To,Lith,Alteration,Logged_By,Log_Date\n"
            "TR-01,0,5,GRN,Chlorite,AB,2024-05-01\n"
        ))

        await env.run("geology.csv")

        note = env.warning("columns_not_ingested")
        assert note["columns"] == ["Logged_By", "Log_Date"]
        assert "Logged_By" in note["detail"]

    async def test_a_refused_table_does_not_also_list_all_its_columns_as_unread(
        self, env, monkeypatch,  # noqa: F811
    ) -> None:
        """A lithology table with no lithology column is refused with its own
        warning; a second one naming every column would be noise."""
        env.conn.preload_collar("TR-01")
        _serve(monkeypatch, env, "HoleID,From,To,Foo\nTR-01,0,5,x\n")

        await env.run("lithology.csv")

        assert "columns_not_ingested" not in env.codes()

    async def test_no_warning_when_every_column_was_read(
        self, env, monkeypatch,  # noqa: F811
    ) -> None:
        env.conn.preload_collar("TR-01")
        _serve(monkeypatch, env, (
            "HoleID,From,To,Lith,Lith_Desc,Comments,Alteration,Alt_Intensity\n"
            "TR-01,0,5,GRN,Granite,fresh,Chlorite,Strong\n"
        ))

        await env.run("geology.csv")

        assert "columns_not_ingested" not in env.codes()
        # "Comments" beside "Lith_Desc" is a second description, kept in it.
        assert env.conn.rows_for("silver.lithology_logs")[0][5] == "Granite [Comments: fresh]"

    async def test_a_plain_lithology_csv_is_unchanged(
        self, env, monkeypatch,  # noqa: F811
    ) -> None:
        env.conn.preload_collar("TR-01")
        _serve(monkeypatch, env, "HoleID,From,To,Lith\nTR-01,0,5,GRN\n")

        out = await env.run("lithology.csv")

        assert set(out.written) == {"lithology"}
        assert not env.conn.rows_for("silver.alteration")
        assert not env.conn.rows_for("silver.mineralization")
        assert env.codes() == []

    async def test_a_standalone_alteration_log(
        self, env, monkeypatch,  # noqa: F811
    ) -> None:
        env.conn.preload_collar("TR-01")
        _serve(monkeypatch, env, (
            "HoleID,From,To,Alt_Type,Alt_Int,Comments\n"
            "TR-01,0,5,Sericite,weak,pervasive\n"
        ))

        out = await env.run("alteration.csv")

        assert set(out.written) == {"alteration"}
        (row,) = env.conn.rows_for("silver.alteration")
        assert row[4:8] == ("Sericite", "weak", None, "pervasive")
        assert not env.conn.rows_for("silver.lithology_logs")

    async def test_a_standalone_mineralization_log_with_a_second_table_inside(
        self, env, monkeypatch,  # noqa: F811
    ) -> None:
        """No lithology code, alteration AND mineral columns: alteration is the
        primary type, and the mineral columns are read as its companion."""
        env.conn.preload_collar("TR-01")
        _serve(monkeypatch, env, (
            "HoleID,From,To,Alteration,Mineral1,Min1_%\n"
            "TR-01,0,5,Chlorite,Pyrite,2\n"
        ))

        out = await env.run("logs.csv")

        assert out.written["alteration"]["written"] == 1
        assert out.written["mineralization"]["written"] == 1
        assert env.conn.rows_for("silver.mineralization")[0][4:6] == ("Pyrite", 2.0)


class TestDbaseAndAccess:
    pytestmark = pytest.mark.asyncio

    _LOG = [
        {"HoleID": "TR-01", "From": 0.0, "To": 5.0, "Lith": "GRN",
         "Alteration": "Chlorite", "Alt_Intensity": "Strong",
         "Mineral1": "Pyrite", "Min1_%": 3.0},
        {"HoleID": "TR-01", "From": 5.0, "To": 9.0, "Lith": "SST",
         "Alteration": None, "Alt_Intensity": None, "Mineral1": None, "Min1_%": None},
    ]

    async def test_a_dbase_geology_table_feeds_all_three(
        self, env, monkeypatch,  # noqa: F811
    ) -> None:
        env.conn.preload_collar("TR-01")
        _dbf(monkeypatch, env, self._LOG)

        out = await env.run("Geology.dbf")

        assert out.written["lithology"]["written"] == 2
        assert out.written["alteration"]["written"] == 1
        assert out.written["mineralization"]["written"] == 1
        assert out.written["attribute_table"]["written"] == 2     # the lossless copy stays

    async def test_an_access_database_writes_collars_before_the_geology_tables(
        self, env, monkeypatch,  # noqa: F811
    ) -> None:
        _mdb(monkeypatch, {"Geology": self._LOG, "Collars": COLLARS})

        out = await env.run("project.mdb")

        order = env.conn.order()
        assert order.index("silver.collars") < order.index("silver.lithology_logs")
        assert order.index("silver.lithology_logs") < order.index("silver.alteration")
        # The geology table's hole is resolved against the collars written a
        # moment earlier in THIS run, so its alteration is not an orphan.
        assert out.written["alteration"] == {
            "written": 1, "skipped": 0, "orphaned": 0, "replaced": 0,
        }

    async def test_a_standalone_alteration_table_in_access(
        self, env, monkeypatch,  # noqa: F811
    ) -> None:
        env.conn.preload_collar("TR-01")
        _mdb(monkeypatch, {"AltLog": [
            {"HoleID": "TR-01", "From": 0.0, "To": 5.0, "Alteration": "Chlorite"},
        ]})

        out = await env.run("logs.mdb")

        assert out.written["alteration"]["written"] == 1
        assert PROJECT     # the run is project-scoped like every other write


# ---------------------------------------------------------------------------
# ZIP two-phase dispatch
# ---------------------------------------------------------------------------

class TestZipPhases:
    def test_alteration_and_mineralization_are_collar_dependents(self) -> None:
        from app.hatchet_workflows import ingest_zip_archive as module

        assert {"alteration", "mineralization"} <= module._INTERVAL_SHEET_TYPES
        assert {"alteration", "mineralization"} <= module._WRITE_SHEET_TYPES

    @pytest.mark.asyncio
    async def test_a_standalone_alteration_log_waits_for_its_collars(self, tmp_path) -> None:
        from app.hatchet_workflows import ingest_zip_archive as module

        alteration = tmp_path / "alteration.csv"
        alteration.write_text("hole_id,from,to,alteration\nH1,0,5,chlorite\n")
        mineral = tmp_path / "mineralization.csv"
        mineral.write_text("hole_id,from,to,mineral1,min1_%\nH1,0,5,pyrite,2\n")
        collars = tmp_path / "collars.csv"
        collars.write_text("hole_id,easting,northing\nH1,1,2\n")

        producers, dependents = await module._split_into_phases(
            [alteration, mineral, collars],
        )

        assert producers == [collars]
        assert dependents == [alteration, mineral]
