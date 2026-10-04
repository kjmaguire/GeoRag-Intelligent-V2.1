"""dBASE / MapInfo DAT / Access tables reach the typed silver tables.

WHY THIS FILE EXISTS
    Typed drill classification (``classify_sheet_type`` -> the collar / survey
    / lithology / sample / structure parsers -> ``_write_collars`` /
    ``_write_intervals``) was reachable only from the CSV and Excel branches of
    ``run_ingest_tabular``. The ``.dbf`` / ``.dat`` branch and the ``.mdb``
    branch read their rows and wrote them ONLY as JSONB into
    silver.attribute_tables. So a MapInfo delivery's Hole Lithology table, its
    collar table and its structure log landed as opaque attribute rows, and the
    Workspace's SECTION / 3D / STRUCTURE / LOGS tabs stayed empty.

    What these tests pin:

      * a table whose headers classify as a drill type is routed through the
        SAME parsers and writers, in WRITE_ORDER (collars before intervals,
        across the tables of one .mdb);
      * the attribute_tables copy is STILL written (the lossless record), and
        the run's headline does not count the same rows twice;
      * a table that classifies as nothing behaves exactly as before;
      * a Discover trace export or a surface-geochemistry table keeps its
        dedicated writer instead of being re-read as survey / sample rows;
      * a typed-write failure on one table never loses the attribute copy.

WHAT RUNS WHERE
    No database and no mdbtools: the connection, the storage client and the
    table readers are replaced. Access is faked at the reader
    (``access_mdb.list_tables`` / ``read_table``), which is where the
    workflow imports it from at call time; the process contract of those two
    functions is covered by georag_geoparsers' own test_access_mdb.py.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

WS = "a0000000-0000-0000-0000-00000000feed"
PROJECT = "b1000000-0000-0000-0000-0000000000a0"
RUN = "c2000000-0000-0000-0000-000000000003"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

class _Conn:
    """Records writes; answers the two reads the writers make."""

    def __init__(self) -> None:
        #: (label, sql, rows) in call order. `label` is the SQL's table.
        self.writes: list[tuple[str, str, list]] = []
        #: silver.collars as `_collar_index` reads it. Collars written by the
        #: run become resolvable, as they would in the database.
        self.collars: list[dict[str, str]] = []
        self.fail_on: str | None = None
        self.closed = False

    def preload_collar(self, hole_id: str) -> str:
        collar_id = str(uuid.uuid4())
        self.collars.append({
            "collar_id": collar_id, "hole_id": hole_id,
            "hole_id_canonical": "".join(c for c in hole_id if c.isalnum()).upper(),
        })
        return collar_id

    @staticmethod
    def _label(sql: str) -> str:
        for name in (
            "silver.collars", "silver.surveys", "silver.lithology_logs",
            "silver.samples", "silver.structure", "silver.attribute_tables",
            "silver.geochemistry", "silver.assays_v2",
            "silver.alteration", "silver.mineralization",
        ):
            if f"INTO {name}" in sql:
                return name
        return "?"

    async def executemany(self, sql: str, rows: list) -> None:
        label = self._label(sql)
        if self.fail_on == label:
            raise RuntimeError(f"simulated failure writing {label}")
        rows = list(rows)
        self.writes.append((label, sql, rows))
        if label == "silver.collars":
            for params in rows:
                self.collars.append({
                    "collar_id": str(uuid.uuid4()),
                    "hole_id": params[2], "hole_id_canonical": params[3],
                })

    async def fetch(self, sql: str, *_args: Any) -> list:
        if "FROM silver.collars" in sql:
            return list(self.collars)
        return []       # silver.element_reference

    async def fetchval(self, sql: str, *_a: Any) -> int | None:
        if "crs_epsg" in sql:
            return None     # the project declares no CRS: the default is a guess
        return 0            # the interval DELETE ... RETURNING count

    async def fetchrow(self, *_a: Any) -> None:
        return None

    def transaction(self):  # noqa: ANN202 - mirrors asyncpg's sync factory
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _txn():
            yield self

        return _txn()

    async def close(self) -> None:
        self.closed = True

    # -- assertions ----------------------------------------------------
    def rows_for(self, label: str) -> list:
        return [r for lbl, _sql, rows in self.writes if lbl == label for r in rows]

    def order(self) -> list[str]:
        """Table labels in the order they were first written."""
        seen: list[str] = []
        for label, _sql, _rows in self.writes:
            if label not in seen:
                seen.append(label)
        return seen


class _Store:
    def get_file(self, _bucket: Any, _key: str, local: str) -> None:
        Path(local).write_bytes(b"stub")


class _Env:
    def __init__(self, it: Any) -> None:
        self.it = it
        self.conn = _Conn()
        self.completed: list[dict] = []
        self.failed: list[dict] = []

    async def run(self, filename: str, *, sheet_type: str | None = None,
                  column_map: dict | None = None) -> Any:
        payload = self.it.IngestTabularInput(
            workspace_id=WS, project_id=PROJECT,
            minio_key=f"bronze/{PROJECT}/{filename}", run_id=RUN,
            sheet_type=sheet_type, column_map=column_map,
        )
        return await self.it.run_ingest_tabular.fn(payload, object())

    @property
    def warnings(self) -> list[dict]:
        assert self.completed, "the run never reached its terminal write"
        return self.completed[-1]["warnings"]

    def warning(self, code: str) -> dict:
        found = [w for w in self.warnings if w.get("code") == code]
        assert found, f"no {code!r}; got {[w.get('code') for w in self.warnings]}"
        return found[0]

    def codes(self) -> list[str]:
        return [w.get("code") for w in self.warnings]

    @property
    def rows_written(self) -> int:
        return self.completed[-1]["rows_written"]


@pytest.fixture
def env(monkeypatch):
    from app.hatchet_workflows import ingest_tabular as it

    fixture = _Env(it)
    monkeypatch.setattr(it, "get_storage_client", lambda: _Store())
    monkeypatch.setattr(it, "_build_dsn", lambda *a, **kw: "postgres://x/y")

    async def _connect(_dsn):
        return fixture.conn

    monkeypatch.setattr(it.asyncpg, "connect", _connect)

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(it, "bind_workspace_scope", _noop)

    async def _start_run(**kw):
        return RUN

    async def _completed(**kw):
        fixture.completed.append(kw)
        return True

    async def _failed(**kw):
        fixture.failed.append(kw)

    monkeypatch.setattr(it._progress, "start_run", _start_run)
    monkeypatch.setattr(it._progress, "mark_stage_started", _noop)
    monkeypatch.setattr(it._progress, "mark_completed_by_run", _completed)
    monkeypatch.setattr(it._progress, "broadcast_terminal", _noop)
    monkeypatch.setattr(it._progress, "mark_failed_by_run", _failed)
    return fixture


def _dbf(monkeypatch, env: _Env, rows: list[dict]) -> None:
    monkeypatch.setattr(env.it, "_read_dbf_table", lambda _path: list(rows))


def _dat(monkeypatch, env: _Env, rows: list[dict]) -> None:
    monkeypatch.setattr(env.it, "_read_mapinfo_dat_table", lambda _path: list(rows))


def _mdb(monkeypatch, tables: dict[str, list[dict]]) -> None:
    """Fake the Access reader: what the workflow imports at call time."""
    from georag_geoparsers import access_mdb

    monkeypatch.setattr(access_mdb, "list_tables", lambda _path: list(tables))
    monkeypatch.setattr(
        access_mdb, "read_table", lambda _path, name, **_kw: list(tables[name]),
    )


COLLARS = [
    {"HoleID": "TR-01", "Easting": 394240.0, "Northing": 6215000.0,
     "Elevation": 12.0, "Azimuth": 322.0, "Dip": -5.0, "Depth": 61.5},
    {"HoleID": "TR-02", "Easting": 394300.0, "Northing": 6215050.0,
     "Elevation": 14.0, "Azimuth": 300.0, "Dip": -5.0, "Depth": 40.0},
]

LITHOLOGY = [
    {"HoleID": "TR-01", "From": 0.0, "To": 5.0, "Lithology": "Andesite",
     "Texture": "Fine"},
    {"HoleID": "TR-01", "From": 5.0, "To": 9.0, "Lithology": "Tuff",
     "Texture": "porphyritic"},          # outside the vocabulary: blanked
    {"HoleID": "TR-01", "From": 9.0, "To": 4.0, "Lithology": "Andesite",
     "Texture": "Fine"},                 # inverted: still rejected
]

STRUCTURE = [
    {"HoleID": "TR-01", "Depth": 10.5, "Struct_Type": "Fault", "Alpha": 45.0,
     "Beta": 120.0, "Dip": 60.0, "DipDir": 210.0, "Comments": "gouge"},
    {"HoleID": "TR-01", "Depth": 12.0, "Struct_Type": "Quartz Vein",
     "Alpha": 30.0, "Beta": 10.0, "Dip": 70.0, "DipDir": 100.0,
     "Comments": None},
]

LEGEND = [
    {"Code": "A", "Description": "Andesite"},
    {"Code": "T", "Description": "Tuff"},
]


# ---------------------------------------------------------------------------
# .dbf
# ---------------------------------------------------------------------------

class TestDbfReachesTheTypedTables:
    pytestmark = pytest.mark.asyncio

    async def test_a_collar_table_lands_as_collars_and_as_attribute_rows(
        self, env, monkeypatch, promotion_dispatch_spy,
    ) -> None:
        _dbf(monkeypatch, env, COLLARS)

        out = await env.run("Collars.dbf")

        assert out.written["collar"]["written"] == 2
        assert out.written["attribute_table"]["written"] == 2   # lossless copy kept
        assert {r[2] for r in env.conn.rows_for("silver.collars")} == {"TR-01", "TR-02"}
        assert len(env.conn.rows_for("silver.attribute_tables")) == 2
        # The typed sheet is listed beside the attribute one.
        assert {(s["type"], s["rows"]) for s in out.sheets} == {
            ("attribute_table", 2), ("collar", 2),
        }
        # Same rows, counted once: 2, not 4.
        assert env.rows_written == 2
        # The CRS was a guess; the run says so, as it does for a CSV.
        assert "collar_crs_assumed" in env.codes()
        # And the promotion that builds the gold views was requested.
        assert promotion_dispatch_spy

    async def test_a_lithology_table_lands_typed_and_keeps_the_row_with_a_blank(
        self, env, monkeypatch,
    ) -> None:
        env.conn.preload_collar("TR-01")
        _dbf(monkeypatch, env, LITHOLOGY)

        out = await env.run("Hole Lithology.dbf")

        rows = env.conn.rows_for("silver.lithology_logs")
        assert out.written["lithology"]["written"] == 2
        assert len(rows) == 2
        # (ws, collar, from, to, code, desc, grain_size, ...)
        assert [r[6] for r in rows] == ["Fine", None]
        assert [r[4] for r in rows] == ["Andesite", "Tuff"]
        # Kyle's rule, surfaced the same way rows_rejected is:
        blanked = env.warning("optional_values_blanked")
        assert blanked["fields"]["grain_size"]["examples"] == ["porphyritic"]
        rejected = env.warning("rows_rejected")
        assert "1 of 3 lithology row(s)" in rejected["message"]
        assert "Hole Lithology" in rejected["message"]
        assert env.rows_written == 3       # the attribute copy; typed not re-counted

    async def test_a_structure_table_lands_in_silver_structure(
        self, env, monkeypatch, promotion_dispatch_spy,
    ) -> None:
        collar_id = env.conn.preload_collar("TR-01")
        _dbf(monkeypatch, env, STRUCTURE)

        out = await env.run("Structure.dbf")

        assert out.written["structure"]["written"] == 2
        rows = env.conn.rows_for("silver.structure")
        # (ws, collar, depth, type, alpha, beta, dip, dip_dir, rough, infill, notes)
        # ... then source_file / source_file_sha256 (the run's lineage).
        assert rows[0][:11] == (
            WS, collar_id, 10.5, "fault", 45.0, 120.0, 60.0, 210.0,
            None, None, "gouge",
        )
        assert rows[0][11] == "Structure.dbf"
        assert rows[1][3] == "vein"
        assert "structure" in {s["type"] for s in out.sheets}
        assert promotion_dispatch_spy

    async def test_structure_rows_for_an_unknown_hole_are_reported_as_orphans(
        self, env, monkeypatch,
    ) -> None:
        _dbf(monkeypatch, env, STRUCTURE)       # no collars in the project

        out = await env.run("Structure.dbf")

        assert out.written["structure"]["written"] == 0
        assert out.written["structure"]["orphaned"] == 2
        assert "orphaned_intervals" in env.codes()

    async def test_a_mapinfo_dat_takes_the_same_route(self, env, monkeypatch) -> None:
        env.conn.preload_collar("TR-01")
        _dat(monkeypatch, env, LITHOLOGY[:1])

        out = await env.run("Hole_Lithology.DAT")

        assert out.written["lithology"]["written"] == 1
        assert out.written["attribute_table"]["written"] == 1

    async def test_a_survey_shaped_table_is_a_survey_not_a_structure(
        self, env, monkeypatch,
    ) -> None:
        """The classification ambiguity, end to end."""
        env.conn.preload_collar("TR-01")
        _dbf(monkeypatch, env, [
            {"HoleID": "TR-01", "Depth": 0.0, "Azimuth": 322.0, "Dip": -5.0},
            {"HoleID": "TR-01", "Depth": 30.0, "Azimuth": 323.0, "Dip": -6.0},
        ])

        out = await env.run("Downhole.dbf")

        assert out.written["survey"]["written"] == 2
        assert "structure" not in out.written
        assert not env.conn.rows_for("silver.structure")

    async def test_an_unclassified_table_behaves_exactly_as_before(
        self, env, monkeypatch,
    ) -> None:
        _dbf(monkeypatch, env, LEGEND)

        out = await env.run("Legend.dbf")

        assert set(out.written) == {"attribute_table"}
        assert env.conn.order() == ["silver.attribute_tables"]
        assert env.codes() == []          # no new warning for a plain legend
        assert env.rows_written == 2
        assert {s["type"] for s in out.sheets} == {"attribute_table"}

    async def test_a_trace_export_keeps_its_dedicated_writer(
        self, env, monkeypatch,
    ) -> None:
        """hole/depth/azimuth/dip would classify as a survey, but the rows are
        segment MIDPOINTS: fed to the generic survey path they would bend every
        hole off its collar. The trace writer collapses them first."""
        trace = [
            {"CollarID_d": "TR-01", "Depth_db": 0.0, "Azimuth_db": 322.0,
             "Dip_db": 0.0, "MidX_db": 500.0, "MidY_db": 6000.0,
             "MidZ_db": 0.0, "SegmentLen": 0.0},
            {"CollarID_d": "TR-01", "Depth_db": 61.0, "Azimuth_db": 322.0,
             "Dip_db": 0.0, "MidX_db": 519.0, "MidY_db": 6024.0,
             "MidZ_db": 0.0, "SegmentLen": 30.5},
        ]
        _dbf(monkeypatch, env, trace)

        out = await env.run("Sitka_trD.DAT".replace(".DAT", ".dbf"))

        collars = env.conn.rows_for("silver.collars")
        assert len(collars) == 1
        assert collars[0][4:6] == (500.0, 6000.0)        # the depth-0 row
        surveys = env.conn.rows_for("silver.surveys")
        assert len(surveys) == 2 and {r[5] for r in surveys} == {"desurveyed_trace"}
        # Written once each - not once by the trace path and again by the
        # generic route.
        assert out.written["survey"]["written"] == 2
        assert out.written["collar"]["written"] == 1

    async def test_surface_geochemistry_keeps_its_dedicated_writer(
        self, env, monkeypatch,
    ) -> None:
        _dbf(monkeypatch, env, [
            {"Sample": f"S{i}", "X": 394000.0 + i, "Y": 6215000.0 + i,
             "Au_ppm": 0.1, "Cu_ppm": 20.0, "As_ppm": 5.0}
            for i in range(3)
        ])

        out = await env.run("Soils.dbf")

        assert "geochemistry" in out.written
        assert not env.conn.rows_for("silver.samples")
        assert not env.conn.rows_for("silver.collars")

    async def test_a_typed_failure_does_not_lose_the_attribute_copy(
        self, env, monkeypatch,
    ) -> None:
        env.conn.fail_on = "silver.collars"
        _dbf(monkeypatch, env, COLLARS)

        out = await env.run("Collars.dbf")

        assert env.failed == []                      # the run did NOT fail
        assert out.written["attribute_table"]["written"] == 2
        assert len(env.conn.rows_for("silver.attribute_tables")) == 2
        failed = env.warning("typed_table_write_failed")
        assert "simulated failure" in failed["detail"]
        assert "attribute table" in failed["detail"]

    async def test_a_refused_typed_table_does_not_reread_the_binary_as_text(
        self, env, monkeypatch,
    ) -> None:
        """A collar-classified table whose rows all lack coordinates writes
        nothing. For CSV/Excel that sends the file to the text fallback, which
        re-reads it as delimited text; for a dBASE table that would be a
        binary file read as text, so it must not happen."""
        blank = [{"HoleID": "TR-01", "Easting": None, "Northing": None}]
        _dbf(monkeypatch, env, blank)

        async def _boom(*a, **kw):
            raise AssertionError("the text fallback ran over a dBASE table")

        monkeypatch.setattr(env.it, "_land_unclassified_as_text", _boom)

        out = await env.run("Collars.dbf", sheet_type="collar")

        refusal = env.warning("classified_but_nothing_written")
        assert "kept whole as a data table" in refusal["detail"]
        assert "searchable text" not in refusal["detail"]
        assert out.unclassified == []
        assert out.written["attribute_table"]["written"] == 1


# ---------------------------------------------------------------------------
# .mdb
# ---------------------------------------------------------------------------

class TestMdbReachesTheTypedTables:
    pytestmark = pytest.mark.asyncio

    async def test_intervals_follow_the_collars_of_the_same_file(
        self, env, monkeypatch,
    ) -> None:
        """WRITE_ORDER across the tables of one .mdb. The tables are listed
        with the intervals FIRST, and no collar exists in the project yet:
        the lithology and structure rows only resolve because the collars
        table is written before them."""
        _mdb(monkeypatch, {
            "Hole Lithology": LITHOLOGY[:2],
            "Structure": STRUCTURE,
            "Legend": LEGEND,
            "Collars": COLLARS,
        })

        out = await env.run("Redstar.mdb")

        assert out.written["collar"]["written"] == 2
        assert out.written["lithology"]["written"] == 2
        assert out.written["structure"]["written"] == 2
        assert not any(v.get("orphaned") for v in out.written.values())

        typed = [t for t in env.conn.order() if t != "silver.attribute_tables"]
        assert typed[0] == "silver.collars"
        assert set(typed) == {
            "silver.collars", "silver.structure", "silver.lithology_logs",
        }

    async def test_every_table_still_lands_as_an_attribute_layer(
        self, env, monkeypatch,
    ) -> None:
        _mdb(monkeypatch, {
            "Collars": COLLARS, "Legend": LEGEND, "Structure": STRUCTURE,
        })

        out = await env.run("Redstar.mdb")

        layers = {r[4] for r in env.conn.rows_for("silver.attribute_tables")}
        assert layers == {"Collars", "Legend", "Structure"}
        assert out.written["attribute_table"]["written"] == 2 + 2 + 2
        # 6 attribute rows; the 4 typed ones are the same rows, not new ones.
        assert env.rows_written == 6

    async def test_unclassified_tables_get_no_typed_write_and_no_new_warning(
        self, env, monkeypatch,
    ) -> None:
        _mdb(monkeypatch, {"Legend": LEGEND, "Codes": LEGEND})

        out = await env.run("Redstar.mdb")

        assert env.conn.order() == ["silver.attribute_tables"]
        assert set(out.written) == {"attribute_table"}
        # The pre-existing advice for a file with no drill table is unchanged.
        assert env.codes() == ["nothing_classified"]

    async def test_null_keys_in_the_first_row_do_not_hide_a_column(
        self, env, monkeypatch,
    ) -> None:
        """mdb-json omits a key whose value is NULL in that row, so row 0 of
        a structure table can lack Alpha/Beta/DipDir that every later row has.
        Headers are the union over rows, not row 0's keys."""
        env.conn.preload_collar("TR-01")
        first = {"HoleID": "TR-01", "Depth": 3.0, "Struct_Type": "joint"}
        _mdb(monkeypatch, {"Structure": [first, *STRUCTURE]})

        out = await env.run("Redstar.mdb")

        assert out.written["structure"]["written"] == 3

    async def test_a_survey_table_next_to_a_structure_table_stay_distinct(
        self, env, monkeypatch,
    ) -> None:
        env.conn.preload_collar("TR-01")
        _mdb(monkeypatch, {
            "Downhole": [
                {"HoleID": "TR-01", "Depth": 0.0, "Azimuth": 322.0, "Dip": -5.0},
            ],
            "Structure": STRUCTURE,
        })

        out = await env.run("Redstar.mdb")

        assert out.written["survey"]["written"] == 1
        assert out.written["structure"]["written"] == 2

    async def test_one_unreadable_table_does_not_stop_the_others(
        self, env, monkeypatch,
    ) -> None:
        from georag_geoparsers import access_mdb

        monkeypatch.setattr(access_mdb, "list_tables", lambda _p: ["Bad", "Collars"])

        def _read(_path, name, **_kw):
            if name == "Bad":
                raise RuntimeError("corrupt table")
            return list(COLLARS)

        monkeypatch.setattr(access_mdb, "read_table", _read)

        out = await env.run("Redstar.mdb")

        assert out.written["collar"]["written"] == 2
        assert "access_table_unreadable" in env.codes()


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------

class TestRoutingHelpers:
    @pytest.mark.parametrize(("raw", "expected"), [
        ("Trench", "channel"), ("trench channel", "channel"), ("C", "channel"),
        ("S", "soil"), ("mystery", "other"), ("", "other"),
    ])
    def test_surface_sample_types(self, raw: str, expected: str) -> None:
        """§04e, SME-approved 2026-09-29: a trench sample is a channel sample."""
        from app.hatchet_workflows.ingest_tabular import _sample_type_of

        assert _sample_type_of(raw) == expected

    def test_columns_are_the_union_over_rows_in_first_seen_order(self) -> None:
        from app.hatchet_workflows.ingest_tabular import _table_columns

        rows = [{"a": 1}, {"a": 2, "b": 3}, {"c": 4, "a": 5}]
        assert _table_columns(rows) == ["a", "b", "c"]

    def test_rows_round_trip_through_the_csv_parsers(self) -> None:
        from app.hatchet_workflows.ingest_tabular import _parse_rows

        result = _parse_rows(
            [{"HoleID": "A-1", "From": 0, "To": 2.5, "Lithology": "Tuff",
              "Texture": None}],
            "lithology",
        )
        assert result.valid_rows == 1
        assert result.records[0]["hole_id_canonical"] == "A1"
        assert result.records[0]["to_depth"] == 2.5

    def test_a_user_column_map_reaches_the_row_parser(self) -> None:
        from app.hatchet_workflows.ingest_tabular import _parse_rows

        result = _parse_rows(
            [{"Hole": "A-1", "Where": 5.0, "Kind": "fault", "Dip": 40.0,
              "DipDir": 90.0}],
            "structure",
            {"structure": {"depth": "Where", "structure_type": "Kind"}},
        )
        assert result.records[0]["depth"] == 5.0
        assert result.records[0]["structure_type"] == "fault"

    def test_verdicts_for_the_shapes_that_matter(self) -> None:
        from app.hatchet_workflows.ingest_tabular import _typed_verdict_for_table

        def verdict(row: dict, **kw: Any) -> str:
            return _typed_verdict_for_table([row], **kw)[0]

        assert verdict(COLLARS[0]) == "collar"
        assert verdict(LITHOLOGY[0]) == "lithology"
        assert verdict(STRUCTURE[0]) == "structure"
        assert verdict(LEGEND[0]) == "unknown"
        assert _typed_verdict_for_table([]) == ("unknown", 0.0)
