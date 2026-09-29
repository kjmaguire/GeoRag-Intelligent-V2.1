"""GIS-12 (audit 2026-09-29): azimuth reference, applied only when declared.

Default unchanged (Kyle): with no recognised reference nothing is applied.
A declared true / magnetic / other-grid reference is converted to the grid
of the collar's own UTM zone, which is the frame the trace is built in.
"""
from __future__ import annotations

import pytest

from app.services.ingest.azimuth_reference import (
    apply,
    azimuth_correction,
    true_north_bearing_in_grid,
)


def _corr(ref: str | None, *, decl: float | None = None, project: int | None = None,
          lon: float = -102.1, lat: float = 58.0):
    return azimuth_correction(
        orientation_reference=ref, magnetic_declination=decl,
        project_epsg=project, local_epsg=32613, lon=lon, lat=lat,
    )


@pytest.mark.parametrize("ref", [None, "", "BOH", "TOH", "whatever"])
def test_nothing_declared_means_nothing_applied(ref: str | None) -> None:
    c = _corr(ref, decl=12.0)
    assert c.degrees == 0.0 and c.reference == "none"
    assert apply(45.0, c) == 45.0


def test_convergence_magnitude_matches_the_audit() -> None:
    # Audit: (-102.1, 58) in zone 13 has 2.46 degrees of convergence.
    beta = true_north_bearing_in_grid(32613, -102.1, 58.0)
    assert abs(beta) == pytest.approx(2.46, abs=0.02)
    # East of the central meridian (-105), true north is WEST of grid north.
    assert beta < 0


def test_true_north_reference_is_corrected() -> None:
    c = _corr("true_north")
    assert c.reference == "true"
    assert c.degrees == pytest.approx(true_north_bearing_in_grid(32613, -102.1, 58.0))
    # Due-north true azimuth is ~357.5 in the zone-13 grid there.
    assert apply(0.0, c) == pytest.approx(360.0 + c.degrees)


def test_magnetic_reference_uses_declination_then_convergence() -> None:
    c = _corr("magnetic", decl=8.0)
    beta = true_north_bearing_in_grid(32613, -102.1, 58.0)
    assert c.degrees == pytest.approx(8.0 + beta)


def test_magnetic_without_declination_is_not_applied() -> None:
    c = _corr("magnetic", decl=None)
    assert c.degrees == 0.0 and c.note


def test_grid_of_the_same_zone_is_no_change() -> None:
    assert _corr("grid_north", project=32613).degrees == 0.0
    assert _corr("grid", project=None).degrees == 0.0


def test_grid_of_another_zone_is_converted() -> None:
    # Project in zone 14, collar computed in its own zone 13 (near the
    # boundary): azimuths are zone-14 grid, the trace is zone-13 grid.
    c = _corr("grid", project=32614, lon=-102.1)
    expected = (
        true_north_bearing_in_grid(32613, -102.1, 58.0)
        - true_north_bearing_in_grid(32614, -102.1, 58.0)
    )
    assert c.degrees == pytest.approx(expected)
    assert abs(c.degrees) > 1.0


# ---------------------------------------------------------------------------
# Wired into promote_silver_to_gold._promote_traces (Hatchet import: CI env)
# ---------------------------------------------------------------------------


class _TraceConn:
    def __init__(self, reference: str | None) -> None:
        self.reference = reference
        self.wkts: list[str] = []

    async def fetchrow(self, sql: str, *args: object) -> dict | None:
        return {"orientation_reference": self.reference,
                "magnetic_declination": None, "crs_epsg": 32613}

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        if "FROM silver.collars" in sql:
            return [{"collar_id": "c1", "elevation": 500.0, "total_depth": 500.0,
                     "azimuth": 0.0, "dip": -45.0, "lon": -102.1, "lat": 58.0,
                     "existing_hash": None}]
        return []   # no surveys: straight-line fallback from the collar

    async def execute(self, sql: str, *args: object) -> str:
        self.wkts.append(str(args[3]))
        return "OK"


async def _toe_offset(reference: str | None) -> tuple[float, float]:
    from app.hatchet_workflows import promote_silver_to_gold as m

    conn = _TraceConn(reference)
    out = m.PromoteSilverToGoldOutput()
    await m._promote_traces(conn, workspace_id="w", project_id="p", out=out)  # type: ignore[arg-type]
    last = conn.wkts[0].split("(")[1].rstrip(")").split(",")[-1].split()
    return float(last[0]), float(last[1])


@pytest.mark.asyncio
async def test_undeclared_reference_builds_the_same_trace_as_before() -> None:
    east, north = await _toe_offset(None)
    assert east == pytest.approx(0.0, abs=1e-6)   # due north in the grid
    assert north > 0


@pytest.mark.asyncio
async def test_true_north_reference_rotates_the_trace_by_the_convergence() -> None:
    east, north = await _toe_offset("true")
    # ~2.46 degrees west of grid north: tan(2.46 deg) = 0.043.
    assert east == pytest.approx(-north * 0.043, rel=0.05)
