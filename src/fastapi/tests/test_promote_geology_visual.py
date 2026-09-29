"""Gold gets lithology it can colour, alteration it can draw, and never breaks.

WHY THIS FILE EXISTS
    promote_silver_to_gold wrote gold.drillhole_intervals_visual lithology bands
    with ``color_hint = COALESCE(colour, rock_code)`` into a VARCHAR(20) that the
    front end uses as a CSS fill. Three defects sat under it:

      * the colour TEXT ("dark grey") or the rock CODE ("GRN") is not a display
        colour, so the strip log filled with an invalid value; and a colour of
        21+ characters ("Dark greenish grey to black") raised
        ``value too long for type character varying(20)`` and failed the INSERT
        for the WHOLE project - no gold at all;
      * two silver.lithology rows over one interval made ``ON CONFLICT DO
        UPDATE`` raise "cannot affect row a second time", same result;
      * a corrected log with different boundaries left the OLD bands beside the
        new ones (an upsert never removes), so the strip log drew two columns.

    And alteration had no gold rows at all: silver.alteration was not read.

    (The statements were also run against a real PostgreSQL with the migrated
    schema; these tests keep the properties that run established from
    regressing without one.)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from app.hatchet_workflows import promote_silver_to_gold as promo

_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parents[3] if len(_HERE.parents) > 3 else _HERE.parents[-1]
GOLD_MIGRATION = (
    REPO_ROOT / "database" / "migrations"
    / "2026_05_13_080000_create_gold_drillhole_intervals_visual.php"
)
SILVER_MIGRATION = (
    REPO_ROOT / "database" / "migrations"
    / "2026_05_20_060400_create_silver_geological_singulars.php"
)
_needs_migrations = pytest.mark.skipif(
    not (GOLD_MIGRATION.exists() and SILVER_MIGRATION.exists()),
    reason="database/migrations is not mounted (container run of src/fastapi only)",
)

WS = "a0000000-0000-0000-0000-00000000feed"
PROJECT = "b1000000-0000-0000-0000-0000000000a0"


class TestLithologyColour:
    def test_a_colour_is_a_display_colour_only_when_it_is_hex(self) -> None:
        sql = promo._INTERVALS_LITHOLOGY
        assert "'^#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$'" in sql
        assert "lower(b.colour)" in sql

    def test_the_rock_code_is_never_written_as_a_colour(self) -> None:
        # The old expression: LEFT(COALESCE(NULLIF(l.colour,''), l.rock_code), 32)
        assert "COALESCE(NULLIF(l.colour" not in promo._INTERVALS_LITHOLOGY
        assert "LEFT(COALESCE(NULLIF" not in promo._INTERVALS_LITHOLOGY

    @_needs_migrations
    def test_the_hint_fits_the_column(self) -> None:
        """A hex colour is at most 7 characters; the column is VARCHAR(20)."""
        assert re.search(r"color_hint\s+VARCHAR\(20\)", GOLD_MIGRATION.read_text())
        # ... and nothing else is ever assigned to it.
        assert promo._INTERVALS_LITHOLOGY.count("color_hint") >= 1
        assert "LEFT(" not in promo._INTERVALS_LITHOLOGY.split("color_hint")[0].splitlines()[-1]

    def test_a_display_colour_already_on_the_row_survives_a_promotion_without_one(self) -> None:
        """Gamma-derived bands carry curated hex colours silver never had."""
        sql = promo._INTERVALS_LITHOLOGY
        conflict = sql[sql.index("ON CONFLICT"):]
        assert "WHEN EXCLUDED.color_hint IS NOT NULL THEN EXCLUDED.color_hint" in conflict
        assert "gold.drillhole_intervals_visual.color_hint" in conflict
        # ... but a non-hex leftover (the old text / rock-code values) is cleared.
        assert conflict.count("~ '^#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$'") == 1


class TestLithologyIsIdempotentAndSafe:
    def test_two_rows_over_one_interval_fold_into_one_band(self) -> None:
        sql = promo._INTERVALS_LITHOLOGY
        assert "SELECT DISTINCT ON (l.collar_id, round(l.from_depth, 3), round(l.to_depth, 3))" in sql
        # Rounded to the gold column's scale (NUMERIC(10,3)) BEFORE the key is
        # taken: two intervals that differ past the third decimal collide there.
        assert "round(l.from_depth, 3) AS depth_from" in sql

    def test_the_fold_is_counted_not_silent(self) -> None:
        assert "lithology_duplicate_intervals" in promo.PromoteSilverToGoldOutput.model_fields
        assert "count(DISTINCT" in promo._LITHOLOGY_DUPLICATES

    def test_the_subselect_is_scoped_to_the_project(self) -> None:
        """Not a scan of every lithology row the workspace has, per project."""
        sql = promo._INTERVALS_LITHOLOGY
        inner = sql[sql.index("SELECT DISTINCT ON"):sql.index(") b")]
        assert "cl.project_id = $1::uuid" in inner

    def test_stale_bands_are_dropped_but_derived_ones_are_left_to_their_owner(self) -> None:
        sql = promo._INTERVALS_LITHOLOGY_STALE
        assert "g.interval_kind = 'lithology'" in sql
        assert "NOT EXISTS" in sql and "silver.lithology l" in sql
        assert "NOT LIKE 'DERIVED-%'" in sql
        assert "g.project_id = $1::uuid" in sql

    def test_the_upsert_and_the_cleanup_are_one_transaction(self) -> None:
        source = Path(promo.__file__).read_text()
        upsert = source.index("await conn.execute(_INTERVALS_LITHOLOGY, project_id)")
        stale = source.index("await conn.execute(_INTERVALS_LITHOLOGY_STALE, project_id)")
        assert upsert < stale
        assert "async with conn.transaction():" in source[upsert - 200:upsert]


class TestAlterationRows:
    def test_alteration_is_a_kind_the_check_constraint_allows(self) -> None:
        assert "'alteration'" in promo._INTERVALS_ALTERATION

    @_needs_migrations
    def test_it_is_in_the_gold_vocabulary_and_uses_only_real_columns(self) -> None:
        gold = GOLD_MIGRATION.read_text()
        assert "'alteration'" in gold
        columns = set(re.findall(r"^\s+([a-z_]+)\s+(?:UUID|NUMERIC|VARCHAR|TEXT|JSONB|TIMESTAMPTZ)", gold, re.MULTILINE | re.IGNORECASE))
        inserted = re.search(
            r"INSERT INTO gold\.drillhole_intervals_visual \((.*?)\)\s*SELECT",
            promo._INTERVALS_ALTERATION, re.DOTALL,
        )
        assert inserted
        names = {c.strip() for c in inserted.group(1).split(",")}
        assert names <= columns, names - columns

    @_needs_migrations
    def test_it_reads_only_real_silver_alteration_columns(self) -> None:
        text = SILVER_MIGRATION.read_text()
        body = re.search(r"CREATE TABLE silver\.alteration \((.*?)\n\s*\)\n\s*SQL", text, re.DOTALL).group(1)
        real = {
            m.group(1) for m in re.finditer(
                r"^\s*([a-z_]+)\s+(?:uuid|numeric|text|timestamptz)", body, re.MULTILINE,
            )
        }
        used = set(re.findall(r"\bx\.([a-z_]+)", promo._INTERVALS_ALTERATION))
        assert used <= real, used - real

    def test_two_alterations_over_one_interval_share_a_row(self) -> None:
        sql = promo._INTERVALS_ALTERATION
        assert "GROUP BY a.collar_id, c.workspace_id, c.project_id, a.depth_from, a.depth_to" in sql
        assert "jsonb_agg(" in sql and "'alterations'" in sql
        for key in ("'type'", "'intensity'", "'minerals'", "'notes'"):
            assert key in sql

    def test_tenancy_comes_from_the_collar_and_scope_from_the_project(self) -> None:
        sql = promo._INTERVALS_ALTERATION
        assert "c.workspace_id, c.project_id" in sql
        assert "x.workspace_id" not in sql
        assert "cx.project_id = $1::uuid" in sql       # the subselect
        assert "c.project_id = $1::uuid" in sql

    def test_it_respects_the_depth_check_before_the_database_does(self) -> None:
        sql = promo._INTERVALS_ALTERATION
        assert "x.from_depth >= 0" in sql
        assert "round(x.to_depth, 3) > round(x.from_depth, 3)" in sql

    def test_alteration_is_rebuilt_not_upserted(self) -> None:
        assert "ON CONFLICT" not in promo._INTERVALS_ALTERATION
        assert "interval_kind = 'alteration'" in promo._INTERVALS_ALTERATION_CLEAR
        assert "project_id = $1::uuid" in promo._INTERVALS_ALTERATION_CLEAR

    def test_there_is_no_mineralization_gold_row(self) -> None:
        """The CHECK has no mineralization kind; bending 'other' is a schema
        decision for Kyle, not this module (see the module docstring)."""
        source = Path(promo.__file__).read_text()
        assert "silver.mineralization" not in source.split("WHAT THE STRIP LOG IS GIVEN")[1].split("IDEMPOTENCY")[1]
        for name in dir(promo):
            if name.startswith("_INTERVALS_"):
                assert "silver.mineralization" not in getattr(promo, name)


class _Conn:
    """Records what the promotion runs, in order."""

    def __init__(self) -> None:
        #: ``(verb, sql, transaction depth when it ran)``, in call order.
        self.calls: list[tuple[str, str, int]] = []
        self._txn_depth = 0

    async def fetch(self, sql: str, *_a: Any) -> list:
        return []

    async def fetchval(self, sql: str, *_a: Any) -> int:
        self.calls.append(("fetchval", sql, self._txn_depth))
        return 2 if "count(DISTINCT" in sql else 0

    async def execute(self, sql: str, *_a: Any) -> str:
        self.calls.append(("execute", sql, self._txn_depth))
        return "INSERT 0 3"

    async def executemany(self, *_a: Any) -> None:
        return None

    async def close(self) -> None:
        return None

    def transaction(self):  # noqa: ANN202
        from contextlib import asynccontextmanager

        conn = self

        @asynccontextmanager
        async def _txn():
            conn._txn_depth += 1
            try:
                yield conn
            finally:
                conn._txn_depth -= 1

        return _txn()


class TestThePromotionRun:
    @pytest.mark.asyncio
    async def test_the_order_and_the_transactions(self, monkeypatch) -> None:
        conn = _Conn()

        async def _connect(*_a: Any, **_k: Any) -> _Conn:
            return conn

        async def _noop(*_a: Any, **_k: Any) -> None:
            return None

        async def _no_traces(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(promo.asyncpg, "connect", _connect)
        monkeypatch.setattr(promo, "build_dsn", lambda *a, **k: "postgres://x/y")
        monkeypatch.setattr(promo, "bind_workspace_scope", _noop)
        monkeypatch.setattr(promo, "_promote_lithology_canonical", _noop)
        monkeypatch.setattr(promo, "_promote_traces", _no_traces)

        out = await promo.promote.fn(
            promo.PromoteSilverToGoldInput(workspace_id=WS, project_id=PROJECT), None,
        )

        ran = [(sql, depth) for verb, sql, depth in conn.calls if verb == "execute"]
        # lithology upsert, stale cleanup, samples, alteration clear + rebuild, structure ...
        kinds = [
            ("lithology" if "'lithology'" in sql and "INSERT" in sql else
             "stale" if "NOT EXISTS" in sql else
             "sample" if "'sample_window'" in sql else
             "alt-clear" if "interval_kind = 'alteration'" in sql and "DELETE" in sql else
             "alt" if "'alteration'" in sql and "INSERT" in sql else "other", depth)
            for sql, depth in ran
        ]
        assert [k for k, _ in kinds][:5] == ["lithology", "stale", "sample", "alt-clear", "alt"]
        # lithology + cleanup share a transaction; so do the alteration clear + rebuild.
        depth_of = dict(kinds)
        assert depth_of["lithology"] == 1 and depth_of["stale"] == 1
        assert depth_of["alt-clear"] == 1 and depth_of["alt"] == 1
        assert depth_of["sample"] == 0
        assert out.alteration_intervals_written == 3
        assert out.lithology_duplicate_intervals == 2
