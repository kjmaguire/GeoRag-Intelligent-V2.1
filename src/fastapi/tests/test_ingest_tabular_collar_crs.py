"""GIS audit 2026-09-29 — the tabular writer places each table in its own CRS.

End-to-end through ``run_ingest_tabular`` with a recording connection (the
harness of test_ingest_tabular_typed_tables). Imports the Hatchet workflow
module, so it needs the same environment as that file (HATCHET_CLIENT_TOKEN
in CI).

GIS-1: a Longitude/Latitude collar table in a UTM project used to be written
with ``$15 = 32613`` (or the project EPSG) and land on the equator; it must
be written with 4326. GIS-2: an undeclared projected table is still written
with 32613 and reported ONCE as ``collar_crs_assumed``.
"""
from __future__ import annotations

from typing import Any

import pytest

from tests.test_ingest_tabular_typed_tables import (  # noqa: F401 — `env` is a fixture
    _Conn,
    _dbf,
    env,
)

LATLON_COLLARS = [
    {"HoleID": "KL-01", "Longitude": -105.6231, "Latitude": 57.2104,
     "Elevation": 480.0, "Azimuth": 90.0, "Dip": -60.0, "Depth": 300.0},
    {"HoleID": "KL-02", "Longitude": -105.6187, "Latitude": 57.2139,
     "Elevation": 482.0, "Azimuth": 90.0, "Dip": -60.0, "Depth": 250.0},
]

UTM_COLLARS = [
    {"HoleID": "TR-01", "Easting": 394240.0, "Northing": 6215000.0,
     "Elevation": 12.0, "Azimuth": 322.0, "Dip": -5.0, "Depth": 61.5},
]


def _source_epsgs(conn: Any) -> set[int]:
    # The source EPSG is $15 in _COLLAR_SQL (index 14); drill_type and
    # hole_status ($16/$17) follow it since the ING-1/PG-10 fix.
    return {row[14] for row in conn.rows_for("silver.collars")}


def _project_crs(conn: _Conn, epsg: int | None) -> None:
    original = conn.fetchval

    async def fetchval(sql: str, *a: Any) -> Any:
        if "crs_epsg" in sql:
            return epsg
        return await original(sql, *a)

    conn.fetchval = fetchval  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_lat_lon_collars_in_a_utm_project_are_written_as_4326(
    env, monkeypatch, promotion_dispatch_spy,  # noqa: F811
) -> None:
    _project_crs(env.conn, 32613)
    _dbf(monkeypatch, env, LATLON_COLLARS)

    out = await env.run("Collars.dbf")

    assert out.written["collar"]["written"] == 2
    assert _source_epsgs(env.conn) == {4326}
    # georef_method is 'detected' ($14), not 'declared' under the project CRS.
    assert {row[13] for row in env.conn.rows_for("silver.collars")} == {"detected"}
    assert "collar_crs_geographic_detected" in env.codes()
    assert "collar_crs_assumed" not in env.codes()


@pytest.mark.asyncio
async def test_lat_lon_collars_with_no_declaration_are_not_assumed(
    env, monkeypatch, promotion_dispatch_spy,  # noqa: F811
) -> None:
    _dbf(monkeypatch, env, LATLON_COLLARS)

    await env.run("Collars.dbf")

    assert _source_epsgs(env.conn) == {4326}
    assert "collar_crs_assumed" not in env.codes()


@pytest.mark.asyncio
async def test_undeclared_utm_collars_are_assumed_and_warned_once(
    env, monkeypatch, promotion_dispatch_spy,  # noqa: F811
) -> None:
    _dbf(monkeypatch, env, UTM_COLLARS)

    await env.run("Collars.dbf")

    assert _source_epsgs(env.conn) == {32613}
    assumed = [w for w in env.warnings if w.get("code") == "collar_crs_assumed"]
    assert len(assumed) == 1
    assert "Import wizard" in assumed[0]["detail"]
    # UpdateProjectRequest deliberately leaves crs_epsg out of its rules, so
    # the advice must not send the geologist to an edit screen that has no
    # CRS field (it did until 2026-09-30).
    assert "Edit project" not in assumed[0]["detail"]
    assert "when the project is created" in assumed[0]["detail"]


@pytest.mark.asyncio
async def test_project_crs_is_used_for_projected_collars(
    env, monkeypatch, promotion_dispatch_spy,  # noqa: F811
) -> None:
    _project_crs(env.conn, 26908)
    _dbf(monkeypatch, env, UTM_COLLARS)

    await env.run("Collars.dbf")

    assert _source_epsgs(env.conn) == {26908}
    assert "collar_crs_assumed" not in env.codes()
