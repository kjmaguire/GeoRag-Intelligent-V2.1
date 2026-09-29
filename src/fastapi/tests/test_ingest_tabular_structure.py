"""Structure has an ingest path, and it writes exactly silver.structure.

WHY THIS FILE EXISTS
    silver.structure existed, promote_silver_to_gold promoted it to
    gold.structure_measurements_visual, and nothing wrote it - a delivery's
    structure logs had nowhere to go, so the STRUCTURE workspace tab was empty
    by construction. Two things here are contracts rather than behaviour:

      * the INSERT names silver.structure's real COLUMNS (the architecture
        doc's section 04e table and the demo seeder both name a different,
        stale set);
      * the structure_type vocabulary the parser emits is the one the gold
        table's CHECK accepts, and one bad value fails a whole project's
        promotion INSERT.

    (Promotion's own idempotency and CHECK-safety: test_promote_structure_visual.py.)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from app.hatchet_workflows import ingest_tabular as it

WS = "11111111-1111-1111-1111-111111111111"

_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parents[3] if len(_HERE.parents) > 3 else _HERE.parents[-1]
_MIGRATIONS = REPO_ROOT / "database" / "migrations"
SILVER_MIGRATION = _MIGRATIONS / "2026_05_20_060400_create_silver_geological_singulars.php"
GOLD_MIGRATION = _MIGRATIONS / "2026_05_13_080002_create_gold_structure_measurements_visual.php"

_needs_migrations = pytest.mark.skipif(
    not (SILVER_MIGRATION.exists() and GOLD_MIGRATION.exists()),
    reason="database/migrations is not mounted (container run of src/fastapi only)",
)


def _silver_structure_columns() -> set[str]:
    text = SILVER_MIGRATION.read_text()
    body = re.search(
        r"CREATE TABLE silver\.structure \((.*?)\n\s*\)\n\s*SQL", text, re.DOTALL,
    )
    assert body, "silver.structure CREATE TABLE not found in its migration"
    columns = set()
    for line in body.group(1).splitlines():
        match = re.match(r"\s*([a-z_]+)\s+(?:uuid|numeric|text|timestamptz)\b", line)
        if match:
            columns.add(match.group(1))
    return columns


def _gold_type_vocabulary() -> set[str]:
    text = GOLD_MIGRATION.read_text()
    block = re.search(r"structure_type IN \((.*?)\)\)", text, re.DOTALL)
    assert block, "gold structure_type CHECK not found"
    return set(re.findall(r"'([a-z_]+)'", block.group(1)))


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

class TestWriteOrder:
    def test_structure_is_a_write_type_after_the_collars_it_references(self) -> None:
        assert "structure" in it.WRITE_ORDER
        assert it.WRITE_ORDER[0] == "collar"
        assert it.WRITE_ORDER.index("structure") > it.WRITE_ORDER.index("collar")

    def test_structure_replaces_per_collar_like_the_interval_tables(self) -> None:
        assert it._INTERVAL_TABLES["structure"] == "silver.structure"

    def test_the_csv_parser_is_registered(self) -> None:
        from georag_geoparsers import parse_csv_structures

        assert it._csv_parser_for("structure") is parse_csv_structures


@_needs_migrations
class TestSchemaContract:
    def test_the_insert_names_only_real_silver_structure_columns(self) -> None:
        inserted = set(re.search(
            r"INSERT INTO silver\.structure \((.*?)\)\s*VALUES", it._STRUCTURE_SQL,
            re.DOTALL,
        ).group(1).replace("\n", " ").replace(" ", "").split(","))

        real = _silver_structure_columns()
        assert inserted <= real, f"not columns of silver.structure: {inserted - real}"
        # NOT NULL columns without a default must all be supplied.
        assert {"workspace_id", "collar_id", "depth", "structure_type"} <= inserted
        # The names the architecture doc uses do NOT exist.
        assert not inserted & {"depth_m", "alpha", "beta", "dip", "dip_dir", "confidence"}

    def test_the_parser_vocabulary_is_the_gold_check_constraint(self) -> None:
        from georag_geoparsers.csv_structure import VALID_STRUCTURE_TYPES

        assert set(VALID_STRUCTURE_TYPES) == _gold_type_vocabulary()

    def test_every_type_the_parser_can_emit_is_in_the_vocabulary(self) -> None:
        from georag_geoparsers.csv_structure import _TYPE_SYNONYMS, VALID_STRUCTURE_TYPES

        assert set(_TYPE_SYNONYMS.values()) <= set(VALID_STRUCTURE_TYPES)


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
        return 3      # rows the DELETE ... RETURNING replaced

    async def fetch(self, *_a: Any) -> list:
        return []

    def transaction(self):  # noqa: ANN202 - mirrors asyncpg's sync factory
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _txn():
            yield self

        return _txn()


_COLLAR_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class TestWriteIntervalsForStructure:
    @pytest.mark.asyncio
    async def test_a_record_becomes_a_silver_structure_row(self) -> None:
        conn = _Conn()
        stats = await it._write_intervals(
            conn, workspace_id=WS, sheet_type="structure",
            records=[{
                "hole_id": "D-01", "depth": 10.5, "structure_type": "fault",
                "alpha_angle": 45.0, "beta_angle": 120.0,
                "true_dip": 60.0, "true_dip_dir": 210.0,
                "roughness": None, "infill": "chlorite", "notes": "gouge",
            }],
            index={"D-01": _COLLAR_ID},
        )

        assert stats == {"written": 1, "skipped": 0, "orphaned": 0, "replaced": 3}
        (sql, rows), = conn.executemany_calls
        assert sql == it._STRUCTURE_SQL
        assert rows == [(
            WS, _COLLAR_ID, 10.5, "fault", 45.0, 120.0, 60.0, 210.0,
            None, "chlorite", "gouge",
        )]

    @pytest.mark.asyncio
    async def test_it_replaces_the_touched_holes_not_appends(self) -> None:
        """Re-running the same file must not double every measurement."""
        conn = _Conn()
        await it._write_intervals(
            conn, workspace_id=WS, sheet_type="structure",
            records=[{"hole_id": "D1", "depth": 1.0, "structure_type": "joint"}],
            index={"D1": _COLLAR_ID},
        )
        (delete_sql, args), = conn.fetchval_calls
        assert "DELETE FROM silver.structure" in delete_sql
        assert args == ([_COLLAR_ID],)

    @pytest.mark.asyncio
    async def test_unknown_holes_are_orphaned_not_dropped_silently(self) -> None:
        conn = _Conn()
        stats = await it._write_intervals(
            conn, workspace_id=WS, sheet_type="structure",
            records=[{"hole_id": "NOPE", "depth": 1.0, "structure_type": "joint"}],
            index={"D1": _COLLAR_ID},
        )
        assert stats["orphaned"] == 1 and stats["written"] == 0
        assert not conn.executemany_calls

    @pytest.mark.asyncio
    async def test_a_record_with_no_depth_is_counted_as_skipped(self) -> None:
        conn = _Conn()
        stats = await it._write_intervals(
            conn, workspace_id=WS, sheet_type="structure",
            records=[{"hole_id": "D1", "depth": None, "structure_type": "joint"}],
            index={"D1": _COLLAR_ID},
        )
        assert stats["skipped"] == 1 and stats["written"] == 0

    @pytest.mark.asyncio
    async def test_a_missing_type_is_stored_as_other_never_null(self) -> None:
        conn = _Conn()
        await it._write_intervals(
            conn, workspace_id=WS, sheet_type="structure",
            records=[{"hole_id": "D1", "depth": 1.0, "structure_type": None}],
            index={"D1": _COLLAR_ID},
        )
        assert conn.executemany_calls[0][1][0][3] == "other"

    def test_the_angles_are_sent_as_double_precision(self) -> None:
        """silver.structure's columns are numeric; a bare Python float would
        be sent as its exact binary expansion (0.1 -> 0.1000000000000000055)."""
        assert it._STRUCTURE_SQL.count("::double precision") == 5
