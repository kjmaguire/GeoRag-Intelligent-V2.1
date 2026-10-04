"""A second upload for the same hole no longer deletes the first upload's rows.

WHY THIS FILE EXISTS
    ``_write_intervals`` used to ``DELETE ... WHERE collar_id = ANY(...)`` for
    every hole a file mentioned, whatever file wrote the rows. ``Au.csv`` and
    ``Cu.csv`` of one ZIP (members run concurrently) or a 3-row correction
    file therefore deleted each other's rows, and the count only reached the
    run's output stats. Now:

      * every row carries ``source_file`` (the LOGICAL upload name, without
        the timestamp prefix) and ``source_file_sha256``;
      * the replace deletes only rows from the SAME logical file, plus rows
        with no source (written before the column existed, not attributable);
      * the replace is a run WARNING (``intervals_replaced`` /
        ``samples_replaced``) with counts;
      * the parser's ``unit_ambiguity`` flags are a run warning
        (``assay_unit_ambiguous``).

    No database: ``_StatefulConn`` interprets exactly the four statement
    shapes the writer issues, against in-memory rows.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

from app.hatchet_workflows import ingest_tabular as it
from tests.test_ingest_tabular_geology import _serve
from tests.test_ingest_tabular_typed_tables import WS, env  # noqa: F401 - `env` is a fixture

COLLAR_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
COLLAR_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
INDEX = {"D1": COLLAR_A, "D2": COLLAR_B}


class _StatefulConn:
    """In-memory silver tables behind the writer's four statement shapes."""

    #: Column positions of collar_id in each insert's parameter tuple.
    _COLLAR_AT = {"silver.assays_v2": 2}

    def __init__(self) -> None:
        #: (table, collar_id, source_file, source_sha)
        self.rows: list[tuple[str, str, str | None, str | None]] = []

    def seed_legacy(self, table: str, collar_id: str, n: int = 1) -> None:
        self.rows.extend((table, collar_id, None, None) for _ in range(n))

    def of(self, table: str, source_file: str | None = "*") -> list[tuple]:
        return [
            r for r in self.rows
            if r[0] == table and (source_file == "*" or r[2] == source_file)
        ]

    async def executemany(self, sql: str, rows: list) -> None:
        table = re.search(r"INSERT INTO (silver\.\w+)", sql).group(1)
        at = self._COLLAR_AT.get(table, 1)
        for params in rows:
            self.rows.append((table, params[at], params[-2], params[-1]))

    async def fetchval(self, sql: str, *args: Any) -> int:
        table = re.search(r"FROM (silver\.\w+)", sql).group(1)
        collars = set(args[0])
        if sql.startswith("SELECT count(*)"):
            assert "source_file IS NULL" in sql
            return sum(
                1 for r in self.rows
                if r[0] == table and r[1] in collars and r[2] is None
            )
        assert sql.startswith("WITH d AS (DELETE FROM")
        if "source_file = $2" in sql:
            deletes = lambda r: r[2] == args[1] or r[2] is None  # noqa: E731
        else:
            assert len(args) == 1, "an unscoped delete takes no source argument"
            deletes = lambda r: True  # noqa: E731
        hit = [
            r[0] == table and r[1] in collars and deletes(r) for r in self.rows
        ]
        self.rows = [r for r, h in zip(self.rows, hit, strict=True) if not h]
        return sum(hit)

    async def fetch(self, *_a: Any) -> list:
        return []

    def transaction(self):  # noqa: ANN202 - mirrors asyncpg's sync factory
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _txn():
            yield self

        return _txn()


def _lith(hole: str, a: float, b: float) -> dict:
    return {"hole_id": hole, "from_depth": a, "to_depth": b, "lithology_code": "GRN"}


async def _write(conn, records, *, source_file, sha="a" * 64, sheet_type="lithology"):
    return await it._write_intervals(
        conn, workspace_id=WS, sheet_type=sheet_type, records=records,
        index=dict(INDEX), source_file=source_file, source_file_sha256=sha,
    )


class TestReplaceIsScopedToTheSameSourceFile:
    @pytest.mark.asyncio
    async def test_a_second_file_for_the_same_hole_keeps_the_first_files_rows(self) -> None:
        conn = _StatefulConn()
        first = await _write(conn, [_lith("D1", 0, 5), _lith("D1", 5, 10)], source_file="Au.csv")
        second = await _write(conn, [_lith("D1", 0, 5)], source_file="Cu.csv", sha="b" * 64)

        assert first["replaced"] == 0 and second["replaced"] == 0
        assert len(conn.of("silver.lithology_logs", "Au.csv")) == 2
        assert len(conn.of("silver.lithology_logs", "Cu.csv")) == 1

    @pytest.mark.asyncio
    async def test_the_same_file_uploaded_again_replaces_only_its_own_rows(self) -> None:
        conn = _StatefulConn()
        await _write(conn, [_lith("D1", 0, 5), _lith("D1", 5, 10)], source_file="lith.csv")
        await _write(conn, [_lith("D1", 0, 5)], source_file="other.csv")

        stats = await _write(
            conn, [_lith("D1", 0, 4), _lith("D1", 4, 8), _lith("D1", 8, 9)],
            source_file="lith.csv", sha="c" * 64,
        )

        assert stats["replaced"] == 2 and stats["replaced_legacy"] == 0
        assert len(conn.of("silver.lithology_logs", "lith.csv")) == 3
        assert len(conn.of("silver.lithology_logs", "other.csv")) == 1
        # the new bytes' hash is the lineage, the file name is the key
        assert {r[3] for r in conn.of("silver.lithology_logs", "lith.csv")} == {"c" * 64}

    @pytest.mark.asyncio
    async def test_rows_written_before_the_column_existed_are_replaced_and_counted(self) -> None:
        conn = _StatefulConn()
        conn.seed_legacy("silver.lithology_logs", COLLAR_A, 4)

        stats = await _write(conn, [_lith("D1", 0, 5)], source_file="lith.csv")

        assert stats["replaced"] == 4 and stats["replaced_legacy"] == 4
        assert conn.of("silver.lithology_logs", None) == []

    @pytest.mark.asyncio
    async def test_other_holes_are_untouched(self) -> None:
        conn = _StatefulConn()
        await _write(conn, [_lith("D2", 0, 5)], source_file="lith.csv")
        stats = await _write(conn, [_lith("D1", 0, 5)], source_file="lith.csv")
        assert stats["replaced"] == 0
        assert len(conn.of("silver.lithology_logs")) == 2

    @pytest.mark.asyncio
    async def test_au_and_cu_sample_files_both_keep_their_samples_and_assays(self) -> None:
        conn = _StatefulConn()
        for name, element in (("Au.csv", "Au_ppm"), ("Cu.csv", "Cu_pct")):
            stats = await _write(
                conn,
                [{"hole_id": "D1", "from_depth": 0.0, "to_depth": 1.0,
                  "sample_type": "Core", "sample_id": "S-1",
                  "commodity_assays": {element: 1.0}}],
                source_file=name, sheet_type="sample",
            )
            assert stats["replaced"] == 0 and stats["assay_replaced"] == 0
        assert len(conn.of("silver.samples")) == 2
        assert {r[2] for r in conn.of("silver.assays_v2")} == {"Au.csv", "Cu.csv"}

    @pytest.mark.asyncio
    async def test_a_sample_re_upload_replaces_its_assay_rows_too(self) -> None:
        conn = _StatefulConn()
        rec = {"hole_id": "D1", "from_depth": 0.0, "to_depth": 1.0,
               "sample_type": "Core", "sample_id": "S-1",
               "commodity_assays": {"Au_ppm": 1.0}}
        await _write(conn, [rec], source_file="Au.csv", sheet_type="sample")
        stats = await _write(conn, [rec], source_file="Au.csv", sheet_type="sample")
        assert stats["replaced"] == 1 and stats["assay_replaced"] == 1
        assert len(conn.of("silver.samples")) == 1
        assert len(conn.of("silver.assays_v2")) == 1

    @pytest.mark.asyncio
    async def test_without_a_source_file_the_legacy_unscoped_replace_is_kept(self) -> None:
        """Only callers outside a run (tests) pass None; the delete stays per collar."""
        conn = _StatefulConn()
        await _write(conn, [_lith("D1", 0, 5)], source_file="Au.csv")
        stats = await it._write_intervals(
            conn, workspace_id=WS, sheet_type="lithology",
            records=[_lith("D1", 0, 3)], index=dict(INDEX),
        )
        assert stats["replaced"] == 1
        assert "replaced_legacy" not in stats

    def test_every_call_site_in_the_workflow_passes_the_source_file(self) -> None:
        """A forgotten call site would quietly restore the cross-file delete."""
        source = Path(it.__file__).read_text(encoding="utf-8")
        calls = [
            n for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "_write_intervals"
        ]
        assert len(calls) == 3
        for call in calls:
            names = {k.arg for k in call.keywords}
            assert {"source_file", "source_file_sha256"} <= names, ast.unparse(call)[:80]


class TestSourceNameAndWarning:
    @pytest.mark.parametrize(("name", "logical"), [
        ("20260924_101112_lith.csv", "lith.csv"),
        ("20260924_101112_123456_Au.csv", "Au.csv"),
        ("lith.csv", "lith.csv"),
        ("20260924_101112_", "20260924_101112_"),
        ("2026_lith.csv", "2026_lith.csv"),
    ])
    def test_logical_source_name_drops_only_the_upload_stamp(self, name, logical) -> None:
        assert it._logical_source_name(name) == logical

    def test_no_replacement_no_warning(self) -> None:
        assert it._replaced_warning(
            sheet_type="lithology", label="x.csv", stats={"replaced": 0},
        ) is None

    def test_interval_warning_carries_counts(self) -> None:
        w = it._replaced_warning(
            sheet_type="lithology", label="lith.csv",
            stats={"replaced": 7, "replaced_legacy": 0},
        )
        assert w["code"] == "intervals_replaced"
        assert w["replaced"] == 7 and "7 lithology row(s)" in w["detail"]
        assert "before uploads recorded" not in w["detail"]

    def test_sample_warning_counts_assays_and_names_legacy_rows(self) -> None:
        w = it._replaced_warning(
            sheet_type="sample", label="Au.csv",
            stats={"replaced": 3, "assay_replaced": 9, "replaced_legacy": 2,
                   "assay_replaced_legacy": 1},
        )
        assert w["code"] == "samples_replaced"
        assert w["replaced"] == 3 and w["assay_replaced"] == 9
        assert "9 element assay row(s)" in w["detail"]
        assert w["replaced_legacy"] == 3
        assert "before uploads recorded their source file" in w["detail"]


_LITHOLOGY_CSV = "HoleID,From,To,Lithology\nTR-01,0,5,Andesite\nTR-01,5,9,Tuff\n"
_SAMPLES_BARE_CSV = (
    "Hole_ID,Sample_ID,From,To,Sample_Type,Au,Cu\n"
    "TR-01,S1,0,1,Core,1.5,5000\nTR-01,S2,1,2,Core,0.2,4000\n"
)


def _spy_deletes(conn) -> list[tuple[str, tuple]]:
    calls: list[tuple[str, tuple]] = []

    async def fetchval(sql: str, *args: Any) -> int | None:
        if "crs_epsg" in sql:
            return None
        calls.append((sql, args))
        return 5 if sql.startswith("WITH d AS (DELETE") else 2

    conn.fetchval = fetchval
    return calls


class TestRunLevel:
    pytestmark = pytest.mark.asyncio

    async def test_run_scopes_the_delete_to_the_logical_file_and_warns(
        self, env, monkeypatch,  # noqa: F811
    ) -> None:
        env.conn.preload_collar("TR-01")
        calls = _spy_deletes(env.conn)
        _serve(monkeypatch, env, _LITHOLOGY_CSV)

        out = await env.run("20260924_101112_123456_lith.csv")

        deletes = [(s, a) for s, a in calls if s.startswith("WITH d AS (DELETE")]
        assert deletes, "the writer never replaced"
        sql, args = deletes[0]
        assert "source_file = $2" in sql and args[1] == "lith.csv"
        warning = env.warning("intervals_replaced")
        assert warning["replaced"] == 5 and warning["replaced_legacy"] == 2
        assert out.written["lithology"]["replaced"] == 5
        rows = env.conn.rows_for("silver.lithology_logs")
        assert {r[-2] for r in rows} == {"lith.csv"}      # logical name, no stamp
        assert len(rows[0][-1]) == 64                     # sha256 of the bytes

    async def test_no_warning_when_nothing_was_replaced(self, env, monkeypatch) -> None:  # noqa: F811
        env.conn.preload_collar("TR-01")
        _serve(monkeypatch, env, _LITHOLOGY_CSV)     # the plain fake DELETEs 0
        await env.run("lith.csv")
        assert "intervals_replaced" not in env.codes()

    async def test_sample_run_warns_replaced_and_ambiguous_units(
        self, env, monkeypatch,  # noqa: F811
    ) -> None:
        env.conn.preload_collar("TR-01")
        _spy_deletes(env.conn)
        _serve(monkeypatch, env, _SAMPLES_BARE_CSV)

        await env.run("Au.csv")

        assert env.warning("samples_replaced")["replaced"] == 5
        amb = env.warning("assay_unit_ambiguous")
        assert set(amb["columns"]) == {"Au", "Cu"}
        by_col = {a["column"]: a for a in amb["ambiguities"]}
        assert (by_col["Au"]["inferred_unit"], by_col["Au"]["alternative_unit"]) == ("ppm", "g/t")
        assert (by_col["Cu"]["inferred_unit"], by_col["Cu"]["alternative_unit"]) == ("ppm", "pct")
        assert "Au" in amb["detail"] and "ppm" in amb["detail"]
        # the existing wide-format warning is untouched
        assert "assay_unit_assumed" in env.codes()

    async def test_a_clean_sample_file_has_no_ambiguity_warning(
        self, env, monkeypatch,  # noqa: F811
    ) -> None:
        env.conn.preload_collar("TR-01")
        _serve(monkeypatch, env, (
            "Hole_ID,Sample_ID,From,To,Sample_Type,Au_ppm,Cu_pct\n"
            "TR-01,S1,0,1,Core,1.5,0.5\n"
        ))
        await env.run("clean.csv")
        assert "assay_unit_ambiguous" not in env.codes()
