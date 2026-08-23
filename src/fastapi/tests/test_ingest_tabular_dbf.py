"""Standalone dBASE (``.dbf``) tables are a first-class tabular ingest.

WHY THIS FILE EXISTS
    A GIS delivery is not only shapefiles. Five ``.dbf`` files in the
    RedStar delivery have no same-stem ``.shp`` beside them -- legend
    tables, a survey-point register, a comment log -- and until this
    change there was nowhere for them to go. The spatial path died with
    ``AttributeError: 'DataFrame' object has no attribute 'crs'`` and the
    tabular path refused the extension BEFORE start_run, so the progress
    row never existed and only the on_failure hook ever closed the run.

    Three of the decisions in the new branch are the quiet kind:

      * a ``.dbf`` beside its ``.shp`` is a SIDECAR. GDAL resolves the
        stem and hands back the shapefile, geometry included, so the two
        cases are indistinguishable AFTER the open -- which is why the
        discrimination is a sibling stat taken before it.
      * the rows land WHOLE, as JSONB. A dBASE table's columns are
        whatever its author typed; mapping them onto silver.collars would
        be inventing structure.
      * the write is idempotent on (project_id, source_file_sha256,
        source_layer, row_index). Without that key a re-upload doubles
        every row -- the failure the interval tables' whole
        replace-don't-append apparatus exists to prevent.

WHAT RUNS WHERE
    Everything except the last class runs anywhere. The last class opens
    the real RedStar ``.dbf`` files with pyogrio and SKIPS when either
    pyogrio or the delivery is absent -- which is the case on the Windows
    workstation this was written on. A skip there is not a pass: the
    fidelity claims (42 rows, embedded CRLF surviving intact) are proven
    in CI or inside georag/fastapi:latest, not here.
"""
from __future__ import annotations

import ast
import datetime as _dt
import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from app.hatchet_workflows.ingest_tabular import (
    _ATTRIBUTE_TABLE_SQL,
    _INSERT_BATCH,
    DBF_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    _assert_standalone_dbf,
    _jsonable,
    _read_dbf_table,
    _sha256_file,
    _write_attribute_rows,
)

WS = "11111111-1111-1111-1111-111111111111"
PROJECT = "22222222-2222-2222-2222-222222222222"
SHA = "a" * 64

_HERE = Path(__file__).resolve()
#: ``src/fastapi/tests`` -> the repo root, computed defensively. A bare
#: ``parents[3]`` at module scope raises IndexError when only
#: ``src/fastapi`` is mounted into a container, and a module-scope
#: IndexError aborts the WHOLE collection instead of skipping one file --
#: which is how test_feature_type_classification.py:40 takes a container
#: run down. Here it degrades to a missing-migration failure in one test.
REPO_ROOT = _HERE.parents[3] if len(_HERE.parents) > 3 else _HERE.parents[-1]
MIGRATION = (
    REPO_ROOT / "database" / "migrations"
    / "2026_08_23_020000_create_silver_attribute_tables.php"
)

#: The real delivery. Absent on CI runners and on every workstation but
#: one, so the fixture class skips rather than failing. ``GEORAG_REDSTAR_DIR``
#: points it at a mount so the same tests can be run for real inside
#: georag/fastapi:latest, which is where pyogrio actually lives.
REDSTAR = Path(
    os.environ.get("GEORAG_REDSTAR_DIR", "C:/Users/GeoRAG/Desktop/RedStar"),
)
REAL_DBFS: dict[str, int] = {
    "Apollo Sitka/UG Workings/Apollo-Sitka maps/apollo no1 shaft 400l.dbf": 8,
    "Apollo Sitka/UG Workings/Apollo-Sitka maps/"
    "sitka 150l approx schippers 1984.dbf": 8,
    "Unga Regional (inc)/Geology/Digital Data/"
    "Drobeck_Unga Silicification.dbf": 26,
    "Unga Regional (inc)/Geology/2005/MiscPoints_2005.dbf": 42,
}

_HAS_PYOGRIO = importlib.util.find_spec("pyogrio") is not None

#: `/* … */`, `//…` and `#…`, in that order. Not a PHP parser -- enough to
#: stop a docblock that DESCRIBES a banned construct from being read as
#: one, which is the whole reason the stripping exists.
_PHP_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*|(?<!\$)#[^\n]*", re.DOTALL)


def _php_without_comments(source: str) -> str:
    return _PHP_COMMENT.sub("", source)


class FakeConn:
    """Records the parameter tuples a writer would send, without a DB."""

    def __init__(self) -> None:
        self.executemany_calls: list[tuple[str, list]] = []

    async def executemany(self, sql: str, rows: list) -> None:
        self.executemany_calls.append((sql, list(rows)))

    @property
    def rows(self) -> list:
        return [row for _sql, rows in self.executemany_calls for row in rows]


# ---------------------------------------------------------------------------
# The extension gate
# ---------------------------------------------------------------------------

class TestTheExtensionGate:
    """``run_ingest_tabular`` raises on an unsupported suffix BEFORE
    start_run, so an omission here is not "the file is refused" -- it is
    "no progress row exists and nothing but on_failure closes the run".
    """

    def test_dbf_is_supported(self) -> None:
        assert ".dbf" in SUPPORTED_EXTENSIONS

    def test_dbf_extensions_is_part_of_the_supported_set(self) -> None:
        assert DBF_EXTENSIONS <= SUPPORTED_EXTENSIONS

    def test_the_gate_is_lowercase_suffix_matching(self) -> None:
        """The workflow lowercases before it looks, so the set must be
        lowercase or an ALL-CAPS delivery name misses it."""
        assert all(ext == ext.lower() for ext in SUPPORTED_EXTENSIONS)


# ---------------------------------------------------------------------------
# Sidecar vs standalone -- the routing decision, made before the open
# ---------------------------------------------------------------------------

class TestSidecarDiscrimination:
    def test_a_lone_dbf_is_a_table(self, tmp_path: Path) -> None:
        dbf = tmp_path / "MiscPoints_2005.dbf"
        dbf.write_bytes(b"")
        _assert_standalone_dbf(str(dbf))   # does not raise

    def test_a_dbf_beside_its_shp_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "faults.shp").write_bytes(b"")
        (tmp_path / "faults.dbf").write_bytes(b"")

        with pytest.raises(ValueError, match="attribute sidecar"):
            _assert_standalone_dbf(str(tmp_path / "faults.dbf"))

    def test_the_refusal_names_the_workflow_that_should_have_it(
        self, tmp_path: Path,
    ) -> None:
        """A geologist reading this needs to know what to do, not merely
        that something was wrong."""
        (tmp_path / "faults.shp").write_bytes(b"")
        (tmp_path / "faults.dbf").write_bytes(b"")

        with pytest.raises(ValueError) as excinfo:
            _assert_standalone_dbf(str(tmp_path / "faults.dbf"))

        assert "ingest_spatial" in str(excinfo.value)

    def test_the_sibling_match_ignores_case(self, tmp_path: Path) -> None:
        """The real delivery ships ``drobeck_shumagin_veins.shp`` beside
        ``Drobeck_Shumagin_Veins.prj``. A case-sensitive sibling check
        would call half of a dataset a standalone table."""
        (tmp_path / "Drobeck_Shumagin_Veins.SHP").write_bytes(b"")
        (tmp_path / "drobeck_shumagin_veins.dbf").write_bytes(b"")

        with pytest.raises(ValueError, match="attribute sidecar"):
            _assert_standalone_dbf(
                str(tmp_path / "drobeck_shumagin_veins.dbf"),
            )

    def test_a_different_stem_is_not_a_sibling(self, tmp_path: Path) -> None:
        """Two unrelated files in one directory must not make each other
        unreadable."""
        (tmp_path / "faults.shp").write_bytes(b"")
        (tmp_path / "legend.dbf").write_bytes(b"")
        _assert_standalone_dbf(str(tmp_path / "legend.dbf"))


# ---------------------------------------------------------------------------
# Cell coercion
# ---------------------------------------------------------------------------

class TestJsonable:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            ("Unga Island", "Unga Island"),
            ("", ""),
            (0, 0),
            (-6, -6),
            (29024.30078125, 29024.30078125),
            (True, True),
            (b"caf\xc3\xa9", "caf\u00e9"),
        ],
    )
    def test_passes_plain_values_through(self, value, expected) -> None:
        assert _jsonable(value) == expected

    def test_nan_becomes_null_not_the_string_nan(self) -> None:
        """A dBASE numeric with nothing in it is missing data. The text
        'nan' in JSONB is indistinguishable from somebody typing it."""
        assert _jsonable(float("nan")) is None

    def test_dates_become_iso_strings(self) -> None:
        assert _jsonable(_dt.date(1984, 6, 1)) == "1984-06-01"
        assert _jsonable(
            _dt.datetime(2005, 7, 14, 9, 30),
        ) == "2005-07-14T09:30:00"

    def test_embedded_newlines_survive_untouched(self) -> None:
        """MiscPoints_2005's C250 Comments column carries embedded CRLF.
        Normalising it silently rewrites the geologist's note."""
        note = "first line\r\nsecond line"
        assert _jsonable(note) == note

    def test_everything_it_returns_is_json_serialisable(self) -> None:
        values = [
            None, "x", 0, -6, 1.5, True, b"y",
            float("nan"), _dt.date(1984, 6, 1),
            _dt.datetime(2005, 7, 14, 9, 30), _dt.time(9, 30),
        ]
        json.dumps([_jsonable(v) for v in values])

    def test_numpy_scalars_are_unwrapped(self) -> None:
        """pyogrio's raw reader yields numpy scalars, which json.dumps
        rejects. Faked rather than skipped: numpy is not guaranteed on
        this interpreter and the contract under test is the duck type."""

        class FakeNpInt:
            dtype = "int32"

            def item(self) -> int:
                return 42

        assert _jsonable(FakeNpInt()) == 42
        json.dumps(_jsonable(FakeNpInt()))

    def test_an_unrecognised_object_degrades_to_text(self) -> None:
        """Landing str(value) keeps the row. Raising would lose the whole
        table over one cell."""

        class Odd:
            def __str__(self) -> str:
                return "odd"

        assert _jsonable(Odd()) == "odd"


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------

class TestWriteAttributeRows:
    async def test_one_parameter_tuple_per_row_in_file_order(self) -> None:
        conn = FakeConn()
        rows = [{"Id": 1}, {"Id": 2}, {"Id": 3}]

        stats = await _write_attribute_rows(
            conn, workspace_id=WS, project_id=PROJECT,
            source_file="legend.dbf", source_file_sha256=SHA,
            source_layer="legend", rows=rows,
        )

        assert stats == {"written": 3, "skipped": 0, "orphaned": 0}
        assert [row[5] for row in conn.rows] == [0, 1, 2]

    async def test_the_row_index_is_the_position_not_a_dbase_field(
        self,
    ) -> None:
        """A dBASE ``Id`` column is the author's, is frequently constant,
        and is not a key -- three of the four real fixtures have exactly
        such a column. Keying on it would collapse 26 rows into one."""
        conn = FakeConn()

        await _write_attribute_rows(
            conn, workspace_id=WS, project_id=PROJECT,
            source_file="Drobeck_Unga Silicification.dbf",
            source_file_sha256=SHA, source_layer="x",
            rows=[{"Id": 0}, {"Id": 0}, {"Id": 0}],
        )

        assert [row[5] for row in conn.rows] == [0, 1, 2]

    async def test_lineage_is_bound_on_every_row(self) -> None:
        conn = FakeConn()
        await _write_attribute_rows(
            conn, workspace_id=WS, project_id=PROJECT,
            source_file="MiscPoints_2005.dbf", source_file_sha256=SHA,
            source_layer="MiscPoints_2005", rows=[{"a": 1}, {"a": 2}],
        )

        for row in conn.rows:
            assert row[0] == WS
            assert row[1] == PROJECT
            assert row[2] == "MiscPoints_2005.dbf"
            assert row[3] == SHA
            assert row[4] == "MiscPoints_2005"

    async def test_attributes_are_json_text_not_a_dict(self) -> None:
        """asyncpg binds ``$7::jsonb`` from text; handing it a dict raises
        at execute time, and on a batched executemany that loses the whole
        table rather than one row."""
        conn = FakeConn()
        await _write_attribute_rows(
            conn, workspace_id=WS, project_id=PROJECT,
            source_file="f.dbf", source_file_sha256=SHA,
            source_layer="f", rows=[{"Comment": "a\r\nb", "N": 1.5}],
        )

        payload = conn.rows[0][6]
        assert isinstance(payload, str)
        assert json.loads(payload) == {"Comment": "a\r\nb", "N": 1.5}

    async def test_an_empty_table_writes_nothing(self) -> None:
        conn = FakeConn()
        stats = await _write_attribute_rows(
            conn, workspace_id=WS, project_id=PROJECT,
            source_file="f.dbf", source_file_sha256=SHA,
            source_layer="f", rows=[],
        )

        assert stats["written"] == 0
        assert conn.executemany_calls == []

    async def test_it_batches_rather_than_sending_one_statement(self) -> None:
        conn = FakeConn()
        rows = [{"i": i} for i in range(_INSERT_BATCH + 7)]

        stats = await _write_attribute_rows(
            conn, workspace_id=WS, project_id=PROJECT,
            source_file="f.dbf", source_file_sha256=SHA,
            source_layer="f", rows=rows,
        )

        assert stats["written"] == _INSERT_BATCH + 7
        assert len(conn.executemany_calls) == 2
        assert [row[5] for row in conn.rows] == list(
            range(_INSERT_BATCH + 7),
        )


# ---------------------------------------------------------------------------
# The INSERT must agree with the table the migration creates
# ---------------------------------------------------------------------------

class TestTheInsertMatchesTheMigration:
    """A cross-language parity check, in the spirit of the one pinning the
    zip-bomb caps: the ON CONFLICT target is only idempotent if it IS the
    migration's UNIQUE constraint, and nothing else compares the two.
    """

    def test_the_migration_exists(self) -> None:
        assert MIGRATION.is_file(), (
            f"{MIGRATION} is missing; silver.attribute_tables then has no "
            f"CREATE TABLE and every standalone-.dbf ingest fails with "
            f"'relation does not exist'"
        )

    def test_the_on_conflict_target_is_the_unique_constraint(self) -> None:
        php = MIGRATION.read_text(encoding="utf-8")
        unique = re.search(
            r"CONSTRAINT\s+uq_attribute_tables_source_row\s*"
            r"UNIQUE\s*\(([^)]*)\)",
            php,
        )
        assert unique, "no uq_attribute_tables_source_row UNIQUE in migration"
        declared = [column.strip() for column in unique.group(1).split(",")]

        conflict = re.search(
            r"ON CONFLICT\s*\(([^)]*)\)", _ATTRIBUTE_TABLE_SQL,
        )
        assert conflict, "the INSERT lost its ON CONFLICT clause"
        targeted = [column.strip() for column in conflict.group(1).split(",")]

        assert targeted == declared, (
            "the INSERT's ON CONFLICT target and the migration's UNIQUE "
            f"constraint disagree ({targeted} vs {declared}). Depending on "
            "which way they drifted a re-upload either raises "
            "UniqueViolation or doubles every row."
        )

    def test_every_column_the_insert_binds_exists_in_the_migration(
        self,
    ) -> None:
        php = MIGRATION.read_text(encoding="utf-8")
        columns = re.search(
            r"INSERT INTO silver\.attribute_tables \(([^)]*)\)",
            _ATTRIBUTE_TABLE_SQL,
        )
        assert columns, "the INSERT is no longer a recognisable column list"

        for column in (c.strip() for c in columns.group(1).split(",")):
            assert re.search(
                rf"^\s+{re.escape(column)}\s", php, re.MULTILINE,
            ), f"the INSERT binds {column}, the migration never declares it"

    def test_the_table_is_rls_protected_in_the_migration(self) -> None:
        """A workspace_id column with no policy is a cross-tenant leak.
        WorkspaceRlsCoverageTest catches it, but only against Postgres --
        which this suite never has, and which the SQLite fast suite skips.

        Scanned with the comments stripped. The migration's own docblock
        names both banned constructs while explaining why it avoids them,
        and a raw substring scan matches that explanation -- the failure
        mode where a check agrees with the prose about the code instead
        of with the code.
        """
        php = _php_without_comments(MIGRATION.read_text(encoding="utf-8"))
        assert "ENABLE ROW LEVEL SECURITY" in php
        assert "CREATE POLICY" in php
        assert "app.workspace_id" in php
        assert "georag.workspace_id" not in php, "legacy fail-open GUC"
        assert "chr(0)" not in php, "the PG18 fail-closed sentinel bug"


# ---------------------------------------------------------------------------
# The branch is actually wired into the workflow body
# ---------------------------------------------------------------------------

class TestTheBranchIsReachable:
    """AST, not a substring scan. Grepping the module text for '.dbf'
    would match the docstring that explains the branch, which is how a
    source check comes to pass against code somebody deleted.
    """

    @staticmethod
    def _uses() -> dict[str, int]:
        """Every name run_ingest_tabular mentions -> the line it first does.

        Names, not calls. Two of the three helpers are handed to
        ``asyncio.to_thread(_read_dbf_table, local)`` -- blocking GDAL and
        a streaming hash must not run on the event loop -- so they appear
        as arguments, and a scan for ``Call(func=Name)`` would report the
        branch missing while it works perfectly.
        """
        from app.hatchet_workflows import ingest_tabular as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "run_ingest_tabular"
            ):
                found: dict[str, int] = {}
                for name in ast.walk(node):
                    if isinstance(name, ast.Name):
                        found.setdefault(name.id, name.lineno)
                return found
        raise AssertionError("run_ingest_tabular not found")

    def test_the_workflow_guards_reads_and_writes(self) -> None:
        uses = self._uses()
        for name in (
            "_assert_standalone_dbf", "_read_dbf_table",
            "_sha256_file", "_write_attribute_rows",
        ):
            assert name in uses, f"{name} is never reached by the workflow"

    def test_the_guard_runs_before_the_reader(self) -> None:
        """After the open there is nothing left to discriminate on: GDAL
        has already returned the shapefile."""
        uses = self._uses()
        assert uses["_assert_standalone_dbf"] < uses["_read_dbf_table"]

    def test_the_read_runs_before_the_write(self) -> None:
        uses = self._uses()
        assert uses["_read_dbf_table"] < uses["_write_attribute_rows"]


# ---------------------------------------------------------------------------
# The real files
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not _HAS_PYOGRIO or not REDSTAR.is_dir(),
    reason=(
        "needs pyogrio AND the RedStar delivery. Neither is present on the "
        "Windows workstation; run inside georag/fastapi:latest with the "
        "delivery mounted. A SKIP here proves nothing."
    ),
)
class TestTheRealDeliveryReads:
    @pytest.mark.parametrize(
        ("relative", "expected_rows"), sorted(REAL_DBFS.items()),
    )
    def test_it_reads_every_row(
        self, relative: str, expected_rows: int,
    ) -> None:
        rows = _read_dbf_table(str(REDSTAR / relative))
        assert len(rows) == expected_rows

    @pytest.mark.parametrize("relative", sorted(REAL_DBFS))
    def test_every_row_is_json_serialisable(self, relative: str) -> None:
        """The write binds ``$7::jsonb`` from ``json.dumps`` output, so a
        cell json cannot encode loses the file, not the cell."""
        for row in _read_dbf_table(str(REDSTAR / relative)):
            json.dumps(row)

    @pytest.mark.parametrize("relative", sorted(REAL_DBFS))
    def test_a_constant_column_is_still_ingested(self, relative: str) -> None:
        """Three of the four fixtures carry a single constant ``Id``
        column. A "looks useless, drop it" heuristic would discard real
        deliveries the product owner asked for by name."""
        rows = _read_dbf_table(str(REDSTAR / relative))
        assert rows
        assert all(row for row in rows)

    def test_embedded_crlf_in_a_comment_survives(self) -> None:
        path = REDSTAR / "Unga Regional (inc)/Geology/2005/MiscPoints_2005.dbf"
        text = [
            value
            for row in _read_dbf_table(str(path))
            for value in row.values()
            if isinstance(value, str)
        ]
        assert any("\r\n" in value for value in text), (
            "MiscPoints_2005 carries embedded CRLF in its C250 Comments "
            "column; losing it rewrites the geologist's note"
        )

    def test_the_sha256_is_hex_and_stable(self) -> None:
        path = REDSTAR / "Unga Regional (inc)/Geology/2005/MiscPoints_2005.dbf"
        digest = _sha256_file(str(path))
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert digest == _sha256_file(str(path))

    async def test_a_real_file_lands_as_parameter_tuples(self) -> None:
        path = REDSTAR / "Unga Regional (inc)/Geology/2005/MiscPoints_2005.dbf"
        conn = FakeConn()
        rows: list[dict[str, Any]] = _read_dbf_table(str(path))

        stats = await _write_attribute_rows(
            conn, workspace_id=WS, project_id=PROJECT,
            source_file=path.name,
            source_file_sha256=_sha256_file(str(path)),
            source_layer=path.stem, rows=rows,
        )

        assert stats["written"] == 42
        assert [row[5] for row in conn.rows] == list(range(42))
