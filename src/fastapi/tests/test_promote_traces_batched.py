"""promote_silver_to_gold._promote_traces: batched reads and writes.

Database audit 2026-10: the desurvey loop issued one ``SELECT ... FROM
silver.surveys WHERE collar_id = $1`` and one ``_TRACE_UPSERT`` execute per
collar, i.e. 10-40k round trips for a large project. It now reads the
surveys of a batch of collars in one ``collar_id = ANY($1::uuid[])`` query,
groups them in Python, and writes the batch with ``executemany``.

The results must be identical to the per-hole path, so these tests give each
hole a DIFFERENT survey and check every hole's trace came out of its own
stations, not a neighbour's -- the failure mode a grouping bug would have.
"""
from __future__ import annotations

import pytest

from app.hatchet_workflows import promote_silver_to_gold as m


class _BatchConn:
    def __init__(self, n_collars: int) -> None:
        self.n = n_collars
        self.survey_calls: list[tuple[str, list[object]]] = []
        self.batches: list[list[tuple[object, ...]]] = []
        self.execute_calls = 0

    @staticmethod
    def _az(i: int) -> float:
        # Alternate due east / due west so the toe's sign identifies the hole.
        return 90.0 if i % 2 == 0 else 270.0

    async def fetchrow(self, sql: str, *args: object) -> dict | None:
        return {"orientation_reference": None, "magnetic_declination": None,
                "crs_epsg": None}

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        if "FROM silver.collars" in sql:
            return [
                {"collar_id": f"c{i}", "elevation": 100.0, "total_depth": 100.0,
                 "azimuth": 0.0, "dip": -45.0, "lon": -102.1, "lat": 58.0,
                 "existing_hash": None}
                for i in range(self.n)
            ]
        ids = list(args[0])  # type: ignore[call-overload]
        self.survey_calls.append((sql, ids))
        rows: list[dict] = []
        for cid in ids:
            i = int(str(cid)[1:])
            for depth in (0.0, 50.0, 100.0):
                rows.append({"collar_id": cid, "depth": depth, "azimuth": self._az(i),
                             "dip": -45.0, "azimuth_reference": None})
        # The real query is ORDER BY collar_id, depth.
        rows.sort(key=lambda r: (r["collar_id"], r["depth"]))
        return rows

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls += 1
        return "OK"

    async def executemany(self, sql: str, args_list: list[tuple[object, ...]]) -> None:
        assert sql == m._TRACE_UPSERT
        self.batches.append(list(args_list))


def _toe_east(wkt: str) -> float:
    return float(wkt.split("(")[1].rstrip(")").split(",")[-1].split()[0])


async def _run(n: int) -> tuple[_BatchConn, m.PromoteSilverToGoldOutput]:
    conn = _BatchConn(n)
    out = m.PromoteSilverToGoldOutput()
    await m._promote_traces(conn, workspace_id="w", project_id="p", out=out)  # type: ignore[arg-type]
    return conn, out


async def test_one_survey_read_and_one_write_per_batch_not_per_hole() -> None:
    n = m._TRACE_COLLAR_BATCH * 2 + 5
    conn, out = await _run(n)

    assert len(conn.survey_calls) == 3, "one survey read per batch of collars"
    assert [len(ids) for _, ids in conn.survey_calls] == [
        m._TRACE_COLLAR_BATCH, m._TRACE_COLLAR_BATCH, 5,
    ]
    assert len(conn.batches) == 3, "one executemany per batch"
    assert conn.execute_calls == 0, "no per-hole execute is left"
    assert out.traces_written == n
    assert sum(len(b) for b in conn.batches) == n


async def test_survey_read_is_a_single_any_array_query() -> None:
    conn, _ = await _run(3)
    sql = conn.survey_calls[0][0]
    assert "ANY($1::uuid[])" in sql
    assert "ORDER BY s.collar_id, s.depth" in sql
    assert "azimuth_reference" in sql


async def test_each_hole_is_traced_from_its_own_stations() -> None:
    n = 40
    conn, _ = await _run(n)
    rows = [args for batch in conn.batches for args in batch]
    assert len(rows) == n
    for args in rows:
        i = int(str(args[0])[1:])
        east = _toe_east(str(args[3]))
        assert (east > 0) == (i % 2 == 0), f"hole c{i} was traced from another hole's stations"


async def test_upsert_arguments_are_unchanged() -> None:
    conn, _ = await _run(1)
    (args,) = conn.batches[0]
    collar_id, ws, project, wkt, utm, lon, lat, digest, dogleg, quality = args
    assert (collar_id, ws, project) == ("c0", "w", "p")
    assert wkt.startswith("LINESTRING Z (")
    assert utm == m._collar_local_utm(-102.1, 58.0)
    assert (lon, lat) == (-102.1, 58.0)
    assert len(str(digest)) == 64
    assert quality == "ok"


async def test_a_hole_with_no_stations_falls_back_per_hole_inside_a_batch() -> None:
    class _Bare(_BatchConn):
        async def fetch(self, sql: str, *args: object) -> list[dict]:
            if "FROM silver.collars" in sql:
                return await super().fetch(sql, *args)
            return []  # no surveys anywhere: every hole uses its collar orientation

    conn = _Bare(3)
    out = m.PromoteSilverToGoldOutput()
    await m._promote_traces(conn, workspace_id="w", project_id="p", out=out)  # type: ignore[arg-type]
    assert out.traces_written == 3
    assert len(conn.batches) == 1 and len(conn.batches[0]) == 3
    # azimuth 0 / dip -45 straight line: no east offset.
    assert all(_toe_east(str(a[3])) == pytest.approx(0.0, abs=1e-6) for a in conn.batches[0])
    assert all(a[9] == "single_survey_vertical" for a in conn.batches[0])


async def test_no_collars_issues_no_survey_read_and_no_write() -> None:
    conn, out = await _run(0)
    assert conn.survey_calls == [] and conn.batches == []
    assert out.traces_written == 0


async def test_survey_read_prefers_the_most_recently_written_source_file() -> None:
    """Two survey files for one hole must not be merged into one trace."""
    conn, _ = await _run(1)
    sql = conn.survey_calls[0][0]
    assert "max(created_at) AS written_at" in sql
    assert "ORDER BY collar_id, written_at DESC" in sql
    assert "s.source_file IS NOT DISTINCT FROM l.source_file" in sql
    assert "n_sources" in sql


async def test_a_hole_with_stations_from_two_files_is_warned_not_silent(caplog) -> None:
    class _Mixed(_BatchConn):
        async def fetch(self, sql: str, *args: object) -> list[dict]:
            rows = await super().fetch(sql, *args)
            if "FROM silver.collars" in sql:
                return rows
            # c1 has two source files on record; c0 has one.
            return [{**r, "n_sources": 2 if r["collar_id"] == "c1" else 1} for r in rows]

    conn = _Mixed(2)
    out = m.PromoteSilverToGoldOutput()
    with caplog.at_level("WARNING"):
        await m._promote_traces(conn, workspace_id="w", project_id="p", out=out)  # type: ignore[arg-type]
    assert out.survey_sources_mixed_holes == 1
    assert any("survey_sources_mixed" in r.getMessage() for r in caplog.records)
    assert out.traces_written == 2, "the mixed hole is still traced, from the latest file"


async def test_single_source_holes_raise_no_mixed_warning(caplog) -> None:
    with caplog.at_level("WARNING"):
        _, out = await _run(3)
    assert out.survey_sources_mixed_holes == 0
    assert not any("survey_sources_mixed" in r.getMessage() for r in caplog.records)
