"""The Discover-trace write, run against a real Postgres.

WHY THIS FILE EXISTS
    Everything about this path was tested EXCEPT the part that touches a
    database, and that is where the defects were.

    A recording fake accepts any SQL, so `silver.collars.geom` being
    declared ``geometry(POINT, 32613)`` went unnoticed: _COLLAR_SQL handed
    PostGIS the project's SOURCE srid, and PostGIS refuses anything else --

        InvalidParameterValueError: Geometry SRID (26904) does not match
        column SRID (32613)

    -- so the tabular collar write had never worked for ANY project outside
    UTM zone 13N. Not a trace bug: the whole CSV/Excel collar path. It was
    invisible because every corpus until then was Athabasca, which IS 32613.
    RedStar's Sitka trenches are EPSG:26904, NAD83 / UTM 4N, Alaska.

    The other three things only a live database can answer, all exercised
    here:

      * the survey ON CONFLICT actually REPLACES rather than appending. An
        append doubles every station on a re-ingest and bends the
        trajectory, with nothing visible to notice.
      * a station resolves to the collar written moments earlier. The index
        is rebuilt after the collar write because those collars did not
        exist when any earlier index was taken; get that wrong and every
        station orphans, which is COUNTED, not raised.
      * geom_4326 -- the value the map uses -- lands in the right country.

WHAT THIS DOES NOT COVER
    The Hatchet task wrapper, bronze storage, or progress reporting. This is
    the persist chain: parse -> collapse -> write collars -> resolve ->
    write surveys.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest

# Live-Postgres test — same routing as test_orphan_sweep.py and its
# siblings. Registered in tests/integration_ci_manifest.txt so CI actually
# runs it; the marker alone would deselect it on PR CI.
pytestmark = pytest.mark.integration

if not os.environ.get("POSTGRES_USER"):
    pytest.skip("postgres env not configured", allow_module_level=True)

from app.db import bind_workspace_scope  # noqa: E402
from app.db.dsn import build_dsn  # noqa: E402
from app.hatchet_workflows.ingest_tabular import (  # noqa: E402
    _collapse_discover_traces,
    _collar_index,
    _discover_trace_columns,
    _read_mapinfo_dat_table,
    _trace_survey_stations,
    _write_collars,
    _write_intervals,
)

#: The real delivery. Absent on CI runners, so the file-backed tests skip;
#: the synthetic ones below carry the same claims without it.
REDSTAR = Path(
    os.environ.get("GEORAG_REDSTAR_DIR", "C:/Users/GeoRAG/Desktop/RedStar"),
)
REAL_TRACE = REDSTAR / "Apollo Sitka" / "Trench" / "Sitka_tr" / "Sitka_trD.DAT"

#: NAD83 / UTM zone 4N. Deliberately NOT 32613 — a test that used the
#: Athabasca zone would pass against the bug this file exists to catch.
SITKA_EPSG = 26904

TRACE_COLUMNS = [
    "CollarID_d", "Depth_db", "Azimuth_db", "Dip_db",
    "MidX_db", "MidY_db", "MidZ_db", "SegmentLen",
]


def _segment(hole: str, depth: float, x: float, y: float) -> dict[str, object]:
    return {
        "CollarID_d": hole, "Depth_db": depth, "Azimuth_db": 322.8,
        "Dip_db": 0.0, "MidX_db": x, "MidY_db": y, "MidZ_db": 0.0,
        "SegmentLen": depth,
    }


#: Two trenches, collar + end-of-hole each, in real Sitka coordinates.
SYNTHETIC_ROWS = [
    _segment("TR002-Sitka", 0.0, 400807.0, 6117291.0),
    _segment("TR002-Sitka", 61.5, 400830.0, 6117320.0),
    _segment("TR003-Sitka", 0.0, 400601.0, 6117146.0),
    _segment("TR003-Sitka", 32.0, 400585.0, 6117160.0),
]


class _Fixture:
    def __init__(self, conn: asyncpg.Connection, ws: str, project: str) -> None:
        self.conn = conn
        self.workspace_id = ws
        self.project_id = project


@pytest.fixture
async def scoped_project():
    conn = await asyncpg.connect(build_dsn(), statement_cache_size=0)
    ws = str(uuid.uuid4())
    project = str(uuid.uuid4())
    try:
        await conn.execute(
            "INSERT INTO silver.workspaces (workspace_id, name, slug) "
            "VALUES ($1::uuid, $2, $3) ON CONFLICT DO NOTHING",
            ws, "trace-integration", f"trace-it-{ws[:8]}",
        )
        # is_local=False: this is a dedicated connection closed at teardown,
        # not a pooled one, so a session GUC is correct. SET LOCAL outside a
        # transaction would be discarded and every query would run unscoped —
        # bind_workspace_scope raises BareConnectionError rather than let that
        # happen silently.
        await bind_workspace_scope(conn, workspace_id=ws, is_local=False)
        await conn.execute(
            "INSERT INTO silver.projects (project_id, project_name, workspace_id, "
            "  crs_datum, orientation_reference, slug) "
            "VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6) ON CONFLICT DO NOTHING",
            project, "trace-integration", ws,
            f"EPSG:{SITKA_EPSG}", "BOH", f"trace-it-{project[:8]}",
        )
        yield _Fixture(conn, ws, project)
    finally:
        await conn.execute(
            "DELETE FROM silver.surveys WHERE collar_id IN "
            "(SELECT collar_id FROM silver.collars WHERE project_id = $1::uuid)",
            project,
        )
        await conn.execute(
            "DELETE FROM silver.collars WHERE project_id = $1::uuid", project)
        await conn.execute(
            "DELETE FROM silver.projects WHERE project_id = $1::uuid", project)
        await conn.execute(
            "DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid", ws)
        await conn.close()


async def _ingest(fx: _Fixture, rows: list[dict], shape: dict) -> tuple[dict, dict]:
    collars = _collapse_discover_traces(rows, shape)
    written = await _write_collars(
        fx.conn, workspace_id=fx.workspace_id, project_id=fx.project_id,
        records=collars, epsg=SITKA_EPSG, georef_method="assumed",
    )
    stations = _trace_survey_stations(rows, shape)
    index = await _collar_index(fx.conn, fx.project_id)
    surveys = await _write_intervals(
        fx.conn, workspace_id=fx.workspace_id, sheet_type="survey",
        records=stations, index=index,
    )
    return written, surveys


async def test_collars_write_at_a_non_athabasca_srid(scoped_project) -> None:
    """The regression. Raised InvalidParameterValueError before the fix."""
    fx = scoped_project
    shape = _discover_trace_columns(TRACE_COLUMNS)
    written, _ = await _ingest(fx, SYNTHETIC_ROWS, shape)

    assert written["written"] == 2
    count = await fx.conn.fetchval(
        "SELECT count(*) FROM silver.collars WHERE project_id = $1::uuid",
        fx.project_id,
    )
    assert count == 2


async def test_the_map_position_is_alaska_not_saskatchewan(scoped_project) -> None:
    """geom_4326 is what the map draws, and it must survive the transform.

    Conforming `geom` to the column's declared SRID must not drag the
    geographic position with it — geom_4326 is transformed from the SOURCE
    srid, so it stays exact.
    """
    fx = scoped_project
    shape = _discover_trace_columns(TRACE_COLUMNS)
    await _ingest(fx, SYNTHETIC_ROWS, shape)

    row = await fx.conn.fetchrow(
        "SELECT ST_X(geom_4326) lon, ST_Y(geom_4326) lat, ST_SRID(geom) srid, "
        "       easting, northing "
        "FROM silver.collars WHERE project_id = $1::uuid AND hole_id = $2",
        fx.project_id, "TR002-Sitka",
    )
    # Unga Island, Alaska — where Apollo-Sitka is.
    assert -161.0 < row["lon"] < -160.0, f"lon {row['lon']} is not Alaska"
    assert 55.0 < row["lat"] < 55.5, f"lat {row['lat']} is not Alaska"
    # The source values are kept untouched; only `geom` is reprojected.
    assert row["easting"] == pytest.approx(400807.0)
    assert row["northing"] == pytest.approx(6117291.0)


async def test_stations_resolve_to_the_collars_written_moments_earlier(
    scoped_project,
) -> None:
    fx = scoped_project
    shape = _discover_trace_columns(TRACE_COLUMNS)
    _, surveys = await _ingest(fx, SYNTHETIC_ROWS, shape)

    assert surveys["written"] == 4
    assert surveys["orphaned"] == 0, (
        "a station that cannot resolve its collar is COUNTED, not raised — "
        "the index must be rebuilt after the collar write"
    )

    joined = await fx.conn.fetch(
        "SELECT c.hole_id, count(s.survey_id) n, min(s.depth) d0 "
        "FROM silver.collars c JOIN silver.surveys s ON s.collar_id = c.collar_id "
        "WHERE c.project_id = $1::uuid GROUP BY c.hole_id ORDER BY c.hole_id",
        fx.project_id,
    )
    assert [r["hole_id"] for r in joined] == ["TR002-Sitka", "TR003-Sitka"]
    for r in joined:
        assert r["n"] == 2
        assert float(r["d0"]) == 0.0, "every hole needs a station at its collar"


async def test_re_ingesting_replaces_stations_rather_than_doubling_them(
    scoped_project,
) -> None:
    fx = scoped_project
    shape = _discover_trace_columns(TRACE_COLUMNS)
    await _ingest(fx, SYNTHETIC_ROWS, shape)
    _, second = await _ingest(fx, SYNTHETIC_ROWS, shape)

    assert second["replaced"] == 4
    total = await fx.conn.fetchval(
        "SELECT count(*) FROM silver.surveys s JOIN silver.collars c "
        "ON c.collar_id = s.collar_id WHERE c.project_id = $1::uuid",
        fx.project_id,
    )
    assert total == 4, (
        "a re-ingest that appends doubles every station and bends the "
        "trajectory, with nothing visible to notice"
    )


@pytest.mark.skipif(not REAL_TRACE.exists(), reason="RedStar delivery not present")
async def test_the_real_sitka_trace_lands_whole(scoped_project) -> None:
    """Five trenches, ten stations, from the actual customer file."""
    fx = scoped_project
    rows = _read_mapinfo_dat_table(REAL_TRACE)
    shape = _discover_trace_columns(list(rows[0]))
    assert shape is not None

    collars, surveys = await _ingest(fx, rows, shape)
    assert collars["written"] == 5
    assert surveys["written"] == 10
    assert surveys["orphaned"] == 0

    depths = await fx.conn.fetch(
        "SELECT c.hole_id, max(s.depth) eoh "
        "FROM silver.collars c JOIN silver.surveys s ON s.collar_id = c.collar_id "
        "WHERE c.project_id = $1::uuid GROUP BY c.hole_id ORDER BY c.hole_id",
        fx.project_id,
    )
    assert {r["hole_id"]: float(r["eoh"]) for r in depths} == {
        "TR002-Sitka": 61.5, "TR003-Sitka": 32.0, "TR004-Sitka": 40.0,
        "TR005-Sitka": 19.0, "TR006-Sitka": 82.0,
    }
