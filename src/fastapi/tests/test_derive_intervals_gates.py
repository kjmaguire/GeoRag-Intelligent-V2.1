"""derive_intervals must not manufacture geology where it does not apply.

WHY THIS FILE EXISTS
    ``ingest_zip_archive`` called ``derive_project`` after ANY archive that
    held a LAS, and ``_emit_for_collar`` then classified every hole with a
    GAMMA curve into SST / SHALE / ORE using Wyoming roll-front uranium
    thresholds, assuming feet. On a gold or copper project that writes a
    lithology log that reads exactly like logged geology, sitting beside (or
    replacing the look of) what a geologist actually logged.

    Now: only a uranium project (``silver.projects.commodity`` free text, or
    ``commodity_arr``), and only holes with no LOGGED lithology in
    silver.lithology_logs. Every delete stays scoped to DERIVED rows.

The connection is a recording fake; the classification maths is untouched and
not re-tested here.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.services.ingest import derive_intervals as di
from app.services.ingest.derive_intervals import is_uranium_commodity

_PJ = "b1000000-0000-0000-0000-0000000000a0"
_WS = "a0000000-0000-0000-0000-00000000feed"
_C1 = "c1000000-0000-0000-0000-000000000001"
_C2 = "c1000000-0000-0000-0000-000000000002"
_C3 = "c1000000-0000-0000-0000-000000000003"


@pytest.mark.parametrize(
    "value",
    [
        "uranium", "Uranium", "URANIUM (ISR)", "U3O8", "u3o8", "U", "u",
        "Au, U", "Cu-U", "gold/uranium", "Uranium-Vanadium", "Au and U3O8",
        ["Au", "Uranium"], ("u3o8",),
    ],
)
def test_uranium_commodities_are_recognised(value: Any) -> None:
    assert is_uranium_commodity(value)


@pytest.mark.parametrize(
    "value",
    [None, "", "  ", "gold", "Au", "Copper", "lithium", "Cu-Au", "U.S. porphyry copper",
     "Cu, Mo", [], ["Au", "Cu"], "unknown", "uranus"],
)
def test_other_or_unstated_commodities_are_not(value: Any) -> None:
    assert not is_uranium_commodity(value)


def test_any_of_several_values_is_enough() -> None:
    # commodity says nothing, commodity_arr says uranium.
    assert is_uranium_commodity(None, ["uranium"])
    assert not is_uranium_commodity("gold", ["copper"])


class _Conn:
    def __init__(
        self, *, commodity: Any, commodity_arr: Any = None, logged: tuple[str, ...] = (),
    ) -> None:
        self.row: dict[str, Any] = {"commodity": commodity}
        if commodity_arr is not None:
            self.row["commodity_arr"] = commodity_arr
        self.logged = logged
        self.executed: list[str] = []
        self.fetch_sql: list[str] = []
        self.closed = False

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "to_jsonb" in sql:
            return json.dumps(self.row)
        return _WS  # workspace_id

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, str]]:
        self.fetch_sql.append(" ".join(sql.split()))
        if "FROM silver.collars WHERE" in sql:
            return [
                {"collar_id": _C1, "hole_id": "H1"},
                {"collar_id": _C2, "hole_id": "H2"},
                {"collar_id": _C3, "hole_id": "H3"},
            ]
        if "silver.lithology_logs" in sql:
            return [{"collar_id": c} for c in self.logged]
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append(" ".join(sql.split()) + f" <{args[0] if args else ''}>")
        return "OK"

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the per-hole emitter; record which holes were derived."""
    calls: list[str] = []

    async def fake_emit(conn: Any, *, collar_id: str, hole_id: str, **_: Any) -> dict[str, Any]:
        calls.append(hole_id)
        return {"hole_id": hole_id, "intervals": 4, "samples": 1, "ore_bands": 1}

    async def no_bind(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(di, "_emit_for_collar", fake_emit)
    monkeypatch.setattr(di, "bind_workspace_scope", no_bind)
    return calls


def _connect_returning(monkeypatch: pytest.MonkeyPatch, conn: _Conn) -> None:
    async def fake_connect(*_: Any, **__: Any) -> _Conn:
        return conn

    monkeypatch.setattr(di.asyncpg, "connect", fake_connect)
    monkeypatch.setattr(di, "build_dsn", lambda: "postgresql://unused")


@pytest.mark.asyncio
@pytest.mark.parametrize("commodity", ["gold", "Copper", None, ""])
async def test_a_non_uranium_or_unstated_project_is_skipped_whole(
    monkeypatch: pytest.MonkeyPatch, emitted: list[str], commodity: Any,
) -> None:
    conn = _Conn(commodity=commodity)
    _connect_returning(monkeypatch, conn)

    summary = await di.derive_project(_PJ)

    assert summary["skipped"] is True
    assert summary["skipped_reason"] == "commodity_not_uranium"
    assert summary["collars_emitted"] == 0
    assert emitted == []
    # Not one row read from the collars table, not one delete (the only
    # statement issued is the session GUC bind).
    assert not any(s.startswith("DELETE") for s in conn.executed)
    assert not any(s.startswith("INSERT") for s in conn.executed)
    assert not any("silver.collars" in s for s in conn.fetch_sql)
    assert conn.closed


@pytest.mark.asyncio
async def test_commodity_arr_alone_can_qualify_a_project(
    monkeypatch: pytest.MonkeyPatch, emitted: list[str],
) -> None:
    conn = _Conn(commodity=None, commodity_arr=["Uranium"])
    _connect_returning(monkeypatch, conn)

    summary = await di.derive_project(_PJ)

    assert summary["skipped"] is False
    assert emitted == ["H1", "H2", "H3"]


@pytest.mark.asyncio
async def test_a_uranium_project_derives_every_hole_that_has_no_logged_lithology(
    monkeypatch: pytest.MonkeyPatch, emitted: list[str],
) -> None:
    conn = _Conn(commodity="Uranium")
    _connect_returning(monkeypatch, conn)

    summary = await di.derive_project(_PJ)

    assert emitted == ["H1", "H2", "H3"]
    assert summary["skipped"] is False
    assert summary["collars_emitted"] == 3
    assert summary["collars_skipped_logged_lithology"] == 0
    # GIS-4: nothing is assumed any more — the unit comes off each curve row.
    assert summary["depth_unit_assumed"] is None


@pytest.mark.asyncio
async def test_holes_with_logged_lithology_are_skipped_and_only_their_derived_rows_cleared(
    monkeypatch: pytest.MonkeyPatch, emitted: list[str],
) -> None:
    conn = _Conn(commodity="U3O8", logged=(_C2,))
    _connect_returning(monkeypatch, conn)

    summary = await di.derive_project(_PJ)

    # H2 was logged by a geologist: not derived.
    assert emitted == ["H1", "H3"]
    assert summary["collars_emitted"] == 2
    assert summary["collars_skipped"] == 1
    assert summary["collars_skipped_logged_lithology"] == 1

    # The "logged" test itself excludes DERIVED-% rows.
    logged_sql = next(s for s in conn.fetch_sql if "silver.lithology_logs" in s)
    assert "NOT LIKE 'DERIVED-%'" in logged_sql

    # Stale derived rows on H2 are removed -- and only derived ones.
    cleanup = [s for s in conn.executed if f"<{_C2}>" in s]
    assert len(cleanup) == 3
    assert any("silver.lithology_logs" in s and "LIKE 'DERIVED-%'" in s for s in cleanup)
    assert any("silver.samples" in s and "sample_type = 'derived_composite'" in s for s in cleanup)
    assert any(
        "gold.drillhole_intervals_visual" in s and "lithology_code LIKE 'DERIVED-%'" in s
        for s in cleanup
    )
    # Nothing was touched on the holes that were derived (the emitter is faked).
    assert not any(f"<{_C1}>" in s or f"<{_C3}>" in s for s in conn.executed)


@pytest.mark.asyncio
async def test_a_failing_cleanup_does_not_abort_the_sweep(
    monkeypatch: pytest.MonkeyPatch, emitted: list[str],
) -> None:
    conn = _Conn(commodity="uranium", logged=(_C1,))
    _connect_returning(monkeypatch, conn)

    async def boom(sql: str, *_: Any, **__: Any) -> str:
        if sql.startswith("DELETE"):
            raise RuntimeError("delete failed")
        return "OK"

    monkeypatch.setattr(conn, "execute", boom)

    summary = await di.derive_project(_PJ)

    assert emitted == ["H2", "H3"]
    assert summary["collars_skipped"] == 1


@pytest.mark.asyncio
async def test_every_delete_the_module_issues_is_scoped_to_derived_rows() -> None:
    conn = _Conn(commodity="uranium")
    await di._clear_derived(conn, _C1)  # type: ignore[arg-type]

    assert len(conn.executed) == 3
    for sql in conn.executed:
        assert "DERIVED-%" in sql or "derived_composite" in sql, sql
