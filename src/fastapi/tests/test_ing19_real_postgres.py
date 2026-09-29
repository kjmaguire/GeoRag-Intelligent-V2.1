"""ING-19 writers against a real PostGIS database.

Needs a database where ``php artisan migrate`` (or at least the migrations
that create silver.workspaces / projects / geophysics_surveys /
geochronology_samples and 2026_09_29_231500_wire_geophysics_and_geochronology
_ingest_tables) has run, and a SUPERUSER DSN in ``ING19_PG_DSN``. Skipped
otherwise — it never falls back to a shared default database, because it
creates and deletes a workspace of its own.

The writes run as ``georag_app`` (``SET ROLE``) with ``app.workspace_id``
bound, i.e. through the fail-closed ``tenant_isolation`` policies the
migration installs, which is the path the Hatchet worker takes.

What a fake connection cannot prove and this does:

* the SQL is valid PostGIS: an XYZ line becomes a LINESTRING in 4326 at the
  right place, the survey AOI is the hull of its lines;
* ``ON CONFLICT (workspace_id, project_id, survey_name)`` and the geochron
  ``NULLS NOT DISTINCT`` key are real arbiters — a second upload REPLACES;
* another workspace sees none of it.

Run locally (private cluster, non-default port):
    ING19_PG_DSN=postgresql://georag@127.0.0.1:55439/wire \\
        PYTHONPATH=.:../georag_geoparsers pytest tests/test_ing19_real_postgres.py
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

asyncpg = pytest.importorskip("asyncpg")

from app.services.ingest.geochronology_writer import write_geochronology  # noqa: E402
from app.services.ingest.geophysics_writer import (  # noqa: E402
    write_dcip_survey,
    write_xyz_survey,
)

pytestmark = pytest.mark.integration

DSN = os.environ.get("ING19_PG_DSN")
FIXTURES = Path(__file__).parent / "fixtures" / "geophysics"


@pytest.fixture
async def scoped():
    """``(conn as georag_app bound to a fresh workspace, workspace, project)``."""
    if not DSN:
        pytest.skip("ING19_PG_DSN not set")
    try:
        admin = await asyncpg.connect(DSN)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"database unreachable: {exc}")
    exists = await admin.fetchval("SELECT to_regclass('silver.geophysics_lines') IS NOT NULL")
    if not exists:
        await admin.close()
        pytest.skip("ING-19 migration not applied to this database")

    ws, pj = str(uuid.uuid4()), str(uuid.uuid4())
    await admin.execute(
        "INSERT INTO silver.workspaces (workspace_id, name, slug) VALUES ($1, $2, $2)",
        ws, f"ing19-{ws[:8]}",
    )
    await admin.execute(
        "INSERT INTO silver.projects (project_id, workspace_id, project_name,"
        " orientation_reference, crs_epsg) VALUES ($1, $2, 'ING-19', 'BOH', 26909)",
        pj, ws,
    )
    conn = await asyncpg.connect(DSN)
    await conn.execute("SET ROLE georag_app")
    await conn.execute("SELECT set_config('app.workspace_id', $1, false)", ws)
    try:
        yield conn, ws, pj
    finally:
        await conn.close()
        await admin.execute("DELETE FROM silver.projects WHERE project_id = $1", pj)
        await admin.execute("DELETE FROM silver.workspaces WHERE workspace_id = $1", ws)
        await admin.close()


async def test_xyz_lands_as_4326_lines_and_a_reupload_replaces(scoped) -> None:
    conn, ws, pj = scoped
    for attempt in (1, 2):
        result = await write_xyz_survey(
            conn, path=str(FIXTURES / "sitka_mag.xyz"), workspace_id=ws, project_id=pj,
            survey_name="sitka_mag.xyz", source_file=f"2026092{attempt}_120000_sitka_mag.xyz",
            source_file_sha256="a" * 64, source_object_key="xyz/k", declared_epsg=None,
            project_epsg=26909, default_epsg=32613,
        )
        assert result.replaced is (attempt == 2)

    assert await conn.fetchval("SELECT count(*) FROM silver.geophysics_surveys") == 1
    rows = await conn.fetch(
        "SELECT line_id, line_type, point_count, GeometryType(geom) AS kind,"
        " ST_SRID(geom) AS srid, ST_X(ST_StartPoint(geom)) AS lon,"
        " ST_Y(ST_StartPoint(geom)) AS lat"
        " FROM silver.geophysics_lines ORDER BY line_id",
    )
    assert [(r["line_id"], r["line_type"], r["point_count"]) for r in rows] == [
        ("1010", "line", 4), ("1020", "line", 3), ("9010", "tie", 2),
    ]
    assert {r["kind"] for r in rows} == {"LINESTRING"} and {r["srid"] for r in rows} == {4326}
    # NAD83 / UTM 9N (495000, 6220000) is on the Alaska panhandle / BC coast.
    assert -130 < rows[0]["lon"] < -128 and 56 < rows[0]["lat"] < 56.3
    assert await conn.fetchval(
        "SELECT count(*) FROM silver.geophysics_line_channels",
    ) == 12
    survey = await conn.fetchrow(
        "SELECT survey_type, georef_method, crs_epsg, GeometryType(aoi_geom) AS aoi"
        " FROM silver.geophysics_surveys",
    )
    assert dict(survey) == {
        "survey_type": "magnetic", "georef_method": "declared", "crs_epsg": 26909,
        "aoi": "POLYGON",
    }


async def test_dcip_readings_and_models_replace_in_place(scoped, tmp_path: Path) -> None:
    from georag_geoparsers.dcip2d_survey import read_dcip2d_survey

    conn, ws, pj = scoped
    export = tmp_path / "L1200N" / "export"
    export.mkdir(parents=True)
    (export / "SYN_Vp_XYZ.rdtmd").write_text(
        "Vp - Line 1200 N\nPole-Dipole\n  100.0  100.0  150.0  200.0  0.5\n"
        "  150.0  150.0  200.0  250.0  0.25\n", encoding="ascii",
    )
    (export / "ipinv2d.chg").write_text("  2   2\n-1e30 5.0 6.0 7.0\n", encoding="ascii")
    survey = read_dcip2d_survey(export, skip_bad_rows=True)
    for _ in range(2):
        await write_dcip_survey(
            conn, survey=survey, workspace_id=ws, project_id=pj,
            survey_name="L1200N DCIP2D — ip.zip/L1200N/export", source_file="ip.zip",
            source_file_sha256=None, source_object_key="geophysics/k",
        )
    assert await conn.fetchval("SELECT count(*) FROM silver.geophysics_dcip_observations") == 2
    model = await conn.fetchrow(
        "SELECT nx, nz, air_mask, earth_min, unit FROM silver.geophysics_dcip_models",
    )
    assert (model["nx"], model["nz"], model["earth_min"], model["unit"]) == (2, 2, 5.0, "mV/V")
    assert model["air_mask"] == [True, False, False, False]
    assert await conn.fetchval(
        "SELECT aoi_geom IS NULL AND crs_epsg IS NULL FROM silver.geophysics_surveys",
    )


async def test_geochron_replaces_per_file_and_isolates_tenants(scoped) -> None:
    from georag_geoparsers.csv_geochronology import parse_csv_geochronology

    conn, ws, pj = scoped
    parsed = parse_csv_geochronology(FIXTURES / "ages_utm.csv", source_label="ages_utm.csv")
    first = await write_geochronology(
        conn, workspace_id=ws, project_id=pj, result=parsed, label="ages_utm.csv",
        source_file="20260929_120000_ages_utm.csv", source_file_sha256=None,
        source_object_key=None, declared_epsg=None, project_epsg=26909, default_epsg=32613,
    )
    again = await write_geochronology(
        conn, workspace_id=ws, project_id=pj, result=parsed, label="ages_utm.csv",
        source_file="20260930_090000_ages_utm.csv", source_file_sha256=None,
        source_object_key=None, declared_epsg=None, project_epsg=26909, default_epsg=32613,
    )
    assert (first.written, again.written, again.replaced) == (3, 3, 3)
    rows = await conn.fetch(
        "SELECT sample_id, age_ma::text AS age, source_row, georef_method,"
        " ST_SRID(geom) AS srid FROM silver.geochronology_samples ORDER BY sample_id",
    )
    assert [(r["sample_id"], r["age"], r["source_row"]) for r in rows] == [
        ("SK-01", "1845.2", 2), ("SK-02", "1810", 3), ("SK-03", "1750.5", 4),
    ]
    assert rows[0]["georef_method"] == "declared" and rows[0]["srid"] == 4326
    assert rows[2]["srid"] is None         # no coordinates: age kept, unlocated

    # The same sample/system/mineral from ANOTHER file is refused, not moved.
    other = await write_geochronology(
        conn, workspace_id=ws, project_id=pj, result=parsed, label="copy.csv",
        source_file="copy.csv", source_file_sha256=None, source_object_key=None,
        declared_epsg=None, project_epsg=26909, default_epsg=32613,
    )
    assert (other.written, other.skipped) == (0, 3)

    await conn.execute(
        "SELECT set_config('app.workspace_id', $1, false)", str(uuid.uuid4()),
    )
    assert await conn.fetchval("SELECT count(*) FROM silver.geochronology_samples") == 0
    await conn.execute("SELECT set_config('app.workspace_id', $1, false)", ws)
