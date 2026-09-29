"""derive_intervals must not delete the geologist's logged lithology from gold.

`_emit_for_collar` re-derives roll-front lithology from a hole's GAMMA curve
and wipes "prior derived rows" so it is re-runnable. Silver was scoped to
`lithology_code LIKE 'DERIVED-%'`; the gold delete was scoped only by
`interval_kind = 'lithology'`, so it also removed every LOGGED lithology band
promote_silver_to_gold had written for the same collar. The ZIP workflow runs
the derivation after any archive containing a LAS, i.e. exactly when a
project's hole logs and its LAS curves arrive together (RedStar).
"""

from __future__ import annotations

from app.services.ingest import derive_intervals as di

_COLLAR = "d3000000-0000-0000-0000-0000000000c0"


class _Conn:
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, sql: str, *args):
        self.executed.append(" ".join(sql.split()))
        return "OK"


async def test_gold_lithology_delete_is_limited_to_derived_rows(monkeypatch):
    pack = di.CurvePack(
        depths_m=[8.0, 9.0, 10.0, 11.0],
        gamma=[10.0, 10.0, 10.0, 10.0],
        grade=None,
        res=[100.0, 100.0, 100.0, 100.0],
        sp=None,
        null_value=-999.25,
    )

    async def _fake_pack(conn, collar_id):
        return pack

    monkeypatch.setattr(di, "_fetch_curve_pack", _fake_pack)
    conn = _Conn()

    await di._emit_for_collar(
        conn,
        workspace_id="a0000000-0000-0000-0000-00000000feed",
        project_id="b1000000-0000-0000-0000-0000000000a0",
        collar_id=_COLLAR,
        hole_id="TR002",
    )

    gold_deletes = [
        s for s in conn.executed
        if s.startswith("DELETE FROM gold.drillhole_intervals_visual")
    ]
    assert len(gold_deletes) == 1
    assert "lithology_code LIKE 'DERIVED-%'" in gold_deletes[0]
    silver_deletes = [
        s for s in conn.executed if s.startswith("DELETE FROM silver.lithology_logs")
    ]
    assert silver_deletes and "DERIVED-%" in silver_deletes[0]

