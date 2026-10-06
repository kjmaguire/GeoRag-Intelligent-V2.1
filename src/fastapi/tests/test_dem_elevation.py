"""Terrain-model elevation for collars whose file had none (2026-10-06).

Two halves:

* ``app/services/dem_elevation.py`` — tile naming, configuration, and the
  sampler, run against a synthetic GeoTIFF laid out exactly like a Copernicus
  GLO-30 tile (one file per 1°x1° cell, named by its south-west corner) so the
  real rasterio/GDAL path is exercised without the network.
* ``promote_silver_to_gold._fill_terrain_elevations`` — what gets written,
  and above all what does NOT: a project whose surveyed collars sit on a local
  grid must not have its gaps filled with sea-level heights.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from app.hatchet_workflows import promote_silver_to_gold as m
from app.services import dem_elevation as dem

# ---------------------------------------------------------------------------
# Tile naming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lon", "lat", "expected"),
    [
        # RedStar, Unga Island, Alaska — the tile verified live on 2026-10-06.
        (-160.559, 55.192, "Copernicus_DSM_COG_10_N55_00_W161_00_DEM"),
        # The Saskatchewan rehearsal projects.
        (-104.995, 53.249, "Copernicus_DSM_COG_10_N53_00_W105_00_DEM"),
        # Southern + eastern hemispheres.
        (151.2, -33.9, "Copernicus_DSM_COG_10_S34_00_E151_00_DEM"),
        # On a cell boundary the point belongs to the cell to its north-east.
        (0.0, 0.0, "Copernicus_DSM_COG_10_N00_00_E000_00_DEM"),
        (-1.0, 45.0, "Copernicus_DSM_COG_10_N45_00_W001_00_DEM"),
        # 180° is -180°.
        (180.0, 10.5, "Copernicus_DSM_COG_10_N10_00_W180_00_DEM"),
    ],
)
def test_copernicus_tile_names_the_south_west_corner(lon: float, lat: float, expected: str) -> None:
    assert dem.copernicus_tile(lon, lat) == expected


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_defaults_point_at_the_public_copernicus_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (dem.URL_TEMPLATE_ENV, dem.SOURCE_ENV, dem.TIMEOUT_ENV):
        monkeypatch.delenv(name, raising=False)
    cfg = dem.config_from_env()
    assert cfg.enabled
    assert cfg.url_template == dem.DEFAULT_URL_TEMPLATE
    assert cfg.source == "copernicus_glo30"
    assert cfg.timeout_s == dem.DEFAULT_TIMEOUT_S
    assert cfg.url_template.format(tile="T") == "https://copernicus-dem-30m.s3.amazonaws.com/T/T.tif"


def test_an_empty_template_turns_the_lookup_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dem.URL_TEMPLATE_ENV, "")
    assert not dem.config_from_env().enabled


def test_a_bad_timeout_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dem.TIMEOUT_ENV, "soon")
    assert dem.config_from_env().timeout_s == dem.DEFAULT_TIMEOUT_S


async def test_lookup_is_a_no_op_when_disabled() -> None:
    cfg = dem.DemConfig(url_template="", source="x", timeout_s=5)
    assert await dem.lookup_elevations([(-160.5, 55.2)], cfg) == {}


# ---------------------------------------------------------------------------
# The sampler, against a synthetic Copernicus-shaped tile
# ---------------------------------------------------------------------------

_WEST, _SOUTH = -161.0, 55.0
_SIZE = 120  # pixels per side; the real tiles are 3600, the maths is the same


def _plane(lon: float, lat: float) -> float:
    """A tilted plane: bilinear interpolation reproduces it exactly."""
    return 100.0 + 50.0 * (lon - _WEST) + 20.0 * (_SOUTH + 1.0 - lat)


def _write_tile(directory: Path, *, nodata_block: bool = False) -> Path:
    import rasterio
    from rasterio.transform import from_bounds

    transform = from_bounds(_WEST, _SOUTH, _WEST + 1.0, _SOUTH + 1.0, _SIZE, _SIZE)
    step = 1.0 / _SIZE
    data = np.empty((_SIZE, _SIZE), dtype="float64")
    for r in range(_SIZE):
        for c in range(_SIZE):
            data[r, c] = _plane(_WEST + (c + 0.5) * step, _SOUTH + 1.0 - (r + 0.5) * step)
    if nodata_block:
        data[:, : _SIZE // 2] = -32767.0  # the western half is sea
    path = directory / f"{dem.copernicus_tile(_WEST + 0.5, _SOUTH + 0.5)}.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=_SIZE,
        height=_SIZE,
        count=1,
        dtype="float64",
        crs="EPSG:4326",
        transform=transform,
        nodata=-32767.0,
    ) as dst:
        dst.write(data, 1)
    return path


def _cfg(directory: Path) -> dem.DemConfig:
    return dem.DemConfig(url_template=str(directory / "{tile}.tif"), source="test_dem", timeout_s=5)


def test_bilinear_sample_reproduces_a_plane(tmp_path: Path) -> None:
    _write_tile(tmp_path)
    points = [(-160.559, 55.192), (-160.1234, 55.8765), (-160.5, 55.5)]
    got = dem.sample_elevations_sync(points, _cfg(tmp_path))
    for i, (lon, lat) in enumerate(points):
        assert got[i] == pytest.approx(_plane(lon, lat), abs=1e-6)


def test_a_missing_tile_is_no_ground_not_a_failure(tmp_path: Path) -> None:
    _write_tile(tmp_path)
    # (-150.5, 54.5) is in a tile that was never written — like a
    # Copernicus all-ocean cell, which the bucket answers with 404.
    got = dem.sample_elevations_sync([(-160.5, 55.5), (-150.5, 54.5)], _cfg(tmp_path))
    assert got[0] == pytest.approx(_plane(-160.5, 55.5))
    assert got[1] is None


def test_an_unreadable_tile_is_left_out_so_it_is_retried(tmp_path: Path) -> None:
    # A file that exists but is not a raster: a transient-class failure
    # (the real-world analogue is a timeout or a 403). It must be ABSENT
    # from the result, not None, or the caller would record "no ground".
    bad = tmp_path / f"{dem.copernicus_tile(-160.5, 55.5)}.tif"
    bad.write_bytes(b"this is not a GeoTIFF")
    got = dem.sample_elevations_sync([(-160.5, 55.5)], _cfg(tmp_path))
    assert got == {}


def test_nodata_neighbours_are_dropped_not_averaged_in(tmp_path: Path) -> None:
    _write_tile(tmp_path, nodata_block=True)
    step = 1.0 / _SIZE
    # Exactly between the last sea column and the first land column: half
    # the weight is on nodata. The answer is the land value, not a mean
    # dragged towards -32767.
    lon = _WEST + (_SIZE // 2) * step
    lat = 55.5
    got = dem.sample_elevations_sync([(lon, lat), (_WEST + 0.1, lat)], _cfg(tmp_path))
    land = _plane(_WEST + (_SIZE // 2 + 0.5) * step, lat)
    assert got[0] == pytest.approx(land, abs=1e-6)
    assert got[1] is None  # entirely in the sea


def test_datum_offset_is_the_median() -> None:
    assert dem.datum_offset_m([]) is None
    assert dem.datum_offset_m([1000.0, 1004.0, 990.0, float("nan")]) == 1000.0


# ---------------------------------------------------------------------------
# The promotion step
# ---------------------------------------------------------------------------


class _Conn:
    def __init__(self, targets: list[dict], references: list[dict]) -> None:
        self.targets = targets
        self.references = references
        self.fetches: list[str] = []
        self.writes: list[tuple] = []

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        self.fetches.append(sql)
        if sql == m._TERRAIN_TARGETS:
            assert args[1] == "copernicus_glo30"
            return self.targets
        if sql == m._TERRAIN_REFERENCES:
            return self.references
        raise AssertionError(f"unexpected query {sql}")

    async def execute(self, sql: str, *args: object) -> str:
        assert sql == m._TERRAIN_WRITE
        self.writes.append(args)
        return f"UPDATE {len(args[0])}"  # type: ignore[arg-type]


def _targets(n: int) -> list[dict]:
    return [{"collar_id": f"c{i}", "lon": -160.56 + i * 0.001, "lat": 55.19} for i in range(n)]


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (dem.URL_TEMPLATE_ENV, dem.SOURCE_ENV, dem.TIMEOUT_ENV):
        monkeypatch.delenv(name, raising=False)


def _fake_lookup(monkeypatch: pytest.MonkeyPatch, heights: dict[int, float | None]) -> list:
    calls: list = []

    async def fake(points, config=None):  # noqa: ANN001, ANN202
        calls.append(list(points))
        return dict(heights)

    monkeypatch.setattr(dem, "lookup_elevations", fake)
    return calls


async def test_fills_collars_with_no_elevation(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _Conn(_targets(3), references=[])
    _fake_lookup(monkeypatch, {0: 12.9, 1: None, 2: 53.5})
    out = m.PromoteSilverToGoldOutput()
    await m._fill_terrain_elevations(conn, project_id="p", out=out)  # type: ignore[arg-type]

    assert len(conn.writes) == 1
    ids, lons, lats, elevs, source = conn.writes[0]
    assert ids == ["c0", "c1", "c2"]
    assert elevs == [12.9, None, 53.5]
    assert lons[0] == pytest.approx(-160.56) and lats[0] == pytest.approx(55.19)
    assert source == "copernicus_glo30"
    assert out.collars_terrain_elevation_filled == 2
    assert out.collars_terrain_no_ground == 1


async def test_a_transient_failure_writes_nothing_for_that_collar(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _Conn(_targets(2), references=[])
    _fake_lookup(monkeypatch, {1: 40.0})  # index 0 absent = not read
    out = m.PromoteSilverToGoldOutput()
    await m._fill_terrain_elevations(conn, project_id="p", out=out)  # type: ignore[arg-type]
    ids, *_ = conn.writes[0]
    assert ids == ["c1"]


async def test_a_local_grid_project_is_not_filled(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # Surveyed collars carry RL = height + 1000 (a mine grid). Filling the
    # gaps with sea-level heights would put those holes 1 km below their
    # neighbours.
    refs = [{"lon": -160.55, "lat": 55.19, "elevation": 1050.0}, {"lon": -160.54, "lat": 55.19, "elevation": 1030.0}]
    conn = _Conn(_targets(2), references=refs)
    _fake_lookup(monkeypatch, {0: 45.0, 1: 46.0, 2: 50.0, 3: 30.0})
    out = m.PromoteSilverToGoldOutput()
    await m._fill_terrain_elevations(conn, project_id="p", out=out)  # type: ignore[arg-type]
    assert conn.writes == []
    assert out.collars_terrain_datum_mismatch == 2
    assert out.collars_terrain_elevation_filled == 0


async def test_a_project_whose_surveys_agree_with_the_model_is_filled(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = [{"lon": -160.55, "lat": 55.19, "elevation": 62.0}]
    conn = _Conn(_targets(1), references=refs)
    _fake_lookup(monkeypatch, {0: 45.0, 1: 50.0})  # 12 m apart: canopy, not a datum
    out = m.PromoteSilverToGoldOutput()
    await m._fill_terrain_elevations(conn, project_id="p", out=out)  # type: ignore[arg-type]
    assert conn.writes[0][3] == [45.0]
    assert out.collars_terrain_elevation_filled == 1


async def test_an_out_of_range_height_is_recorded_as_no_ground(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _Conn(_targets(1), references=[])
    _fake_lookup(monkeypatch, {0: -32767.0})
    out = m.PromoteSilverToGoldOutput()
    await m._fill_terrain_elevations(conn, project_id="p", out=out)  # type: ignore[arg-type]
    assert conn.writes[0][3] == [None]
    assert out.collars_terrain_no_ground == 1


async def test_disabled_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dem.URL_TEMPLATE_ENV, "")
    conn = _Conn(_targets(1), references=[])
    await m._fill_terrain_elevations(conn, project_id="p", out=m.PromoteSilverToGoldOutput())  # type: ignore[arg-type]
    assert conn.fetches == []


async def test_nothing_to_look_up_makes_no_lookup(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_lookup(monkeypatch, {})
    conn = _Conn([], references=[])
    await m._fill_terrain_elevations(conn, project_id="p", out=m.PromoteSilverToGoldOutput())  # type: ignore[arg-type]
    assert calls == []
    assert conn.fetches == [m._TERRAIN_TARGETS]


async def test_a_database_error_never_escapes(enabled: None) -> None:
    class Broken:
        async def fetch(self, *a: object) -> list:
            raise RuntimeError("connection reset")

    # Must not raise: the trace/interval promotions after it still run.
    await m._fill_terrain_elevations(Broken(), project_id="p", out=m.PromoteSilverToGoldOutput())  # type: ignore[arg-type]


def test_targets_query_re_looks_up_moved_or_re_sourced_collars() -> None:
    sql = m._TERRAIN_TARGETS
    assert "c.elevation IS NULL" in sql
    assert "elevation_dem_source IS DISTINCT FROM" in sql
    assert "ST_Equals(c.elevation_dem_geom, c.geom_4326)" in sql


def test_write_never_touches_the_file_elevation() -> None:
    sql = m._TERRAIN_WRITE
    assert "SET elevation_dem_m" in sql
    assert "elevation =" not in sql.replace("elevation_dem", "")
    # A file elevation that landed between the read and the write wins.
    assert "c.elevation IS NULL" in sql


def test_traces_use_the_terrain_height_when_the_file_had_none() -> None:
    import inspect

    src = inspect.getsource(m._promote_traces)
    assert "COALESCE(c.elevation, c.elevation_dem_m) AS elevation" in src


def test_the_range_guard_matches_the_check_constraint() -> None:
    migration = (
        Path(__file__).resolve().parents[3]
        / "database/migrations/2026_10_06_110000_add_terrain_elevation_to_collars.php"
    ).read_text()
    assert f"BETWEEN {int(m._TERRAIN_MIN_M)} AND {int(m._TERRAIN_MAX_M)}" in migration
    assert math.isclose(m._TERRAIN_MIN_M, -500.0)
