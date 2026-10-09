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


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # What GDAL actually raises (checked against a local HTTP server).
        ("HTTP response code: 404", True),
        ("/vsicurl/https://x/y.tif: No such file or directory", True),
        ("'/vsicurl/https://x/y.tif' does not exist in the file system, and is not recognized", True),
        ("HTTP response code: 403", False),
        ("HTTP response code: 500", False),
        ("HTTP response code: 4040", False),
        # The digits 404 inside something that is not a status.
        ("CURL error: Failed to connect to 127.0.0.1 port 4040 after 0 ms: Could not connect to server", False),
        ("CURL error: Operation timed out after 20404 milliseconds", False),
        ("CURL error: Empty reply from server", False),
    ],
)
def test_not_found_is_an_http_status_not_a_substring(message: str, expected: bool) -> None:
    from rasterio.errors import RasterioIOError

    assert dem._is_not_found(RasterioIOError(message)) is expected


def test_a_non_finite_position_is_recorded_not_fatal(tmp_path: Path) -> None:
    # One collar whose reprojection produced NaN must not abort the batch (it
    # used to raise out of the tile grouping) and must not re-select itself
    # forever: it maps to None, "no ground", like any position with no tile.
    _write_tile(tmp_path)
    points = [(float("nan"), 55.5), (-160.5, 55.5), (float("inf"), 55.5), (-160.5, 95.0)]
    got = dem.sample_elevations_sync(points, _cfg(tmp_path))
    assert got[0] is None and got[2] is None and got[3] is None
    assert got[1] == pytest.approx(_plane(-160.5, 55.5))


async def test_lookup_survives_a_non_finite_position(tmp_path: Path) -> None:
    _write_tile(tmp_path)
    got = await dem.lookup_elevations([(float("nan"), 1.0), (-160.5, 55.5)], _cfg(tmp_path))
    assert got[0] is None
    assert got[1] == pytest.approx(_plane(-160.5, 55.5))


def test_reads_go_north_to_south_then_west_to_east(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_tile(tmp_path)
    seen: list[tuple[float, float]] = []
    real = dem._bilinear

    def spy(src, lon, lat):  # noqa: ANN001, ANN202
        seen.append((lon, lat))
        return real(src, lon, lat)

    monkeypatch.setattr(dem, "_bilinear", spy)
    pts = [(-160.1, 55.2), (-160.9, 55.8), (-160.2, 55.8), (-160.5, 55.5)]
    dem.sample_elevations_sync(pts, _cfg(tmp_path))
    assert seen == [(-160.9, 55.8), (-160.2, 55.8), (-160.5, 55.5), (-160.1, 55.2)]


def test_an_expired_deadline_reads_nothing_and_leaves_points_for_the_next_run(tmp_path: Path) -> None:
    import time

    _write_tile(tmp_path)
    got = dem.sample_elevations_sync([(-160.5, 55.5)], _cfg(tmp_path), deadline=time.monotonic() - 1)
    assert got == {}


def test_running_out_of_time_mid_tile_keeps_what_was_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    _write_tile(tmp_path)
    real = dem._bilinear
    calls = {"n": 0}

    def slow_first(src, lon, lat):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(0.08)  # past the deadline after the first point
        return real(src, lon, lat)

    monkeypatch.setattr(dem, "_bilinear", slow_first)
    pts = [(-160.9, 55.8), (-160.2, 55.8), (-160.5, 55.5)]
    got = dem.sample_elevations_sync(pts, _cfg(tmp_path), deadline=time.monotonic() + 0.04)
    # Partial progress is returned, not discarded with the thread.
    assert list(got) == [0]
    assert got[0] == pytest.approx(_plane(-160.9, 55.8))


async def test_lookup_never_raises_and_passes_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def boom(points, config, deadline=None):  # noqa: ANN001, ANN202
        seen["deadline"] = deadline
        raise RuntimeError("gdal exploded")

    monkeypatch.setattr(dem, "sample_elevations_sync", boom)
    cfg = dem.DemConfig(url_template="x/{tile}.tif", source="s", timeout_s=2)
    assert await dem.lookup_elevations([(-160.5, 55.5)], cfg, deadline=123.0) == {}
    assert seen["deadline"] == 123.0


def test_a_source_label_longer_than_the_column_is_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dem.SOURCE_ENV, "copernicus_glo30_eu_central_mirror_v2")
    cfg = dem.config_from_env()
    assert len(cfg.source) == dem.SOURCE_MAX_LEN == 32
    assert cfg.source == "copernicus_glo30_eu_central_mirr"


def test_the_source_limit_matches_the_column_width() -> None:
    migration = (
        Path(__file__).resolve().parents[3]
        / "database/migrations/2026_10_06_110000_add_terrain_elevation_to_collars.php"
    ).read_text()
    assert f"elevation_dem_source varchar({dem.SOURCE_MAX_LEN})" in migration


def test_the_run_budget_is_shared_and_has_a_breaker() -> None:
    b = dem.TerrainBudget(run_s=100, project_s=10)
    assert b.usable
    import time

    assert b.project_deadline() <= time.monotonic() + 10 + 0.5
    b.trip()
    assert not b.usable
    spent = dem.TerrainBudget(run_s=0, project_s=10)
    assert not spent.usable
    # A project never gets more than what is left of the run.
    short = dem.TerrainBudget(run_s=1, project_s=100)
    assert short.project_deadline() <= time.monotonic() + 1.0


def test_effective_elevation_only_trusts_a_lookup_at_the_present_position() -> None:
    sql = dem.EFFECTIVE_ELEVATION_SQL
    assert sql.startswith("COALESCE(c.elevation,")
    assert "ST_Equals(c.elevation_dem_geom, c.geom_4326)" in sql


def test_datum_offset_is_the_median() -> None:
    assert dem.datum_offset_m([]) is None
    assert dem.datum_offset_m([1000.0, 1004.0, 990.0, float("nan")]) == 1000.0


# ---------------------------------------------------------------------------
# The promotion step
# ---------------------------------------------------------------------------


class _Conn:
    """Scripted stand-in for the asyncpg connection ``_fill_terrain_elevations`` uses."""

    def __init__(
        self,
        targets: list[dict],
        references: list[dict] | None = None,
        *,
        candidates: int | None = None,
        pending: int | None = None,
        held: int = 0,
    ) -> None:
        self.targets = targets
        self.references = references or []
        self.candidates = len(targets) if candidates is None else candidates
        self.pending = len(targets) if pending is None else pending
        self.held = held
        self.fetches: list[str] = []
        self.executed: list[str] = []
        self.writes: list[tuple] = []

    async def fetchrow(self, sql: str, *args: object) -> dict:
        assert sql == m._TERRAIN_SCOPE
        return {"candidates": self.candidates, "pending": self.pending, "held": self.held}

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        self.fetches.append(sql)
        if sql == m._TERRAIN_TARGETS:
            assert len(args) == 1  # project only; staleness is cleared first
            return self.targets
        if sql == m._TERRAIN_REFERENCES:
            return self.references
        raise AssertionError(f"unexpected query {sql}")

    async def execute(self, sql: str, *args: object) -> str:
        self.executed.append(sql)
        if sql == m._TERRAIN_CLEAR_STALE:
            assert args[1] == "copernicus_glo30"
        elif sql == m._TERRAIN_WRITE:
            self.writes.append(args)
        else:
            assert sql == m._TERRAIN_CLEAR_ALL
        return "UPDATE 0"


def _targets(n: int) -> list[dict]:
    return [{"collar_id": f"c{i}", "lon": -160.56 + i * 0.001, "lat": 55.19} for i in range(n)]


def _budget() -> dem.TerrainBudget:
    return dem.TerrainBudget(run_s=1000, project_s=100)


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (dem.URL_TEMPLATE_ENV, dem.SOURCE_ENV, dem.TIMEOUT_ENV):
        monkeypatch.delenv(name, raising=False)


def _fake_lookup(monkeypatch: pytest.MonkeyPatch, *results: dict[int, float | None]) -> list:
    """Each call to lookup_elevations returns the next dict, indexed within THAT call."""
    calls: list = []
    queue = list(results)

    async def fake(points, config=None, deadline=None):  # noqa: ANN001, ANN202
        calls.append((list(points), deadline))
        return dict(queue.pop(0)) if queue else {}

    monkeypatch.setattr(dem, "lookup_elevations", fake)
    return calls


async def _fill(conn: _Conn, budget: dem.TerrainBudget | None = None) -> m.PromoteSilverToGoldOutput:
    out = m.PromoteSilverToGoldOutput()
    await m._fill_terrain_elevations(conn, project_id="p", out=out, budget=budget or _budget())  # type: ignore[arg-type]
    return out


async def test_fills_collars_with_no_elevation(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _Conn(_targets(3))
    _fake_lookup(monkeypatch, {0: 12.9, 1: None, 2: 53.5})
    out = await _fill(conn)

    assert len(conn.writes) == 1
    ids, lons, lats, elevs, source = conn.writes[0]
    assert ids == ["c0", "c1", "c2"]
    assert elevs == [12.9, None, 53.5]
    assert lons[0] == pytest.approx(-160.56) and lats[0] == pytest.approx(55.19)
    assert source == "copernicus_glo30"
    assert out.collars_terrain_elevation_filled == 2
    assert out.collars_terrain_no_ground == 1


async def test_stale_lookups_are_cleared_before_anything_is_selected(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _Conn(_targets(1))
    _fake_lookup(monkeypatch, {0: 5.0})
    await _fill(conn)
    assert conn.executed[0] == m._TERRAIN_CLEAR_STALE


async def test_heights_are_written_to_the_centimetre(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _Conn(_targets(1))
    _fake_lookup(monkeypatch, {0: 12.899999618530273})
    await _fill(conn)
    assert conn.writes[0][3] == [12.9]


async def test_a_transient_failure_writes_nothing_for_that_collar(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _Conn(_targets(2))
    _fake_lookup(monkeypatch, {1: 40.0})  # index 0 absent = not read
    await _fill(conn)
    ids, *_ = conn.writes[0]
    assert ids == ["c1"]


async def test_a_host_that_returns_nothing_trips_the_breaker_for_the_rest_of_the_run(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = _budget()
    first = _Conn(_targets(2))
    calls = _fake_lookup(monkeypatch, {})  # nothing read at all
    await _fill(first, budget)
    assert first.writes == []
    assert budget.tripped

    # The next project makes no request at all, instead of waiting out its own timeouts.
    second = _Conn(_targets(2))
    await _fill(second, budget)
    assert len(calls) == 1
    assert second.writes == []
    assert second.executed == [m._TERRAIN_CLEAR_STALE]  # SQL-only housekeeping still runs


async def test_a_spent_run_budget_skips_the_lookup(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_lookup(monkeypatch, {0: 1.0})
    await _fill(_Conn(_targets(1)), dem.TerrainBudget(run_s=0, project_s=10))
    assert calls == []


async def test_every_lookup_carries_the_project_deadline(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    refs = [{"lon": -160.55, "lat": 55.19, "elevation": 62.0}]
    calls = _fake_lookup(monkeypatch, {0: 45.0}, {0: 1.0})
    await _fill(_Conn(_targets(1), refs))
    assert len(calls) == 2
    assert all(deadline is not None for _, deadline in calls)
    assert calls[0][1] == calls[1][1]


async def test_a_local_grid_project_is_not_filled_and_does_no_bulk_lookup(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Surveyed collars carry RL = height + 1000 (a mine grid). Filling the
    # gaps with sea-level heights would put those holes 1 km below their
    # neighbours.
    refs = [{"lon": -160.55, "lat": 55.19, "elevation": 1050.0}, {"lon": -160.54, "lat": 55.19, "elevation": 1030.0}]
    conn = _Conn(_targets(20000), refs)
    calls = _fake_lookup(monkeypatch, {0: 45.0, 1: 46.0})
    out = await _fill(conn)
    assert conn.writes == []
    assert out.collars_terrain_datum_mismatch == 20000
    assert out.collars_terrain_elevation_filled == 0
    # Only the two reference collars were sampled; the 20,000 targets never were.
    assert [len(points) for points, _ in calls] == [2]
    assert m._TERRAIN_TARGETS not in conn.fetches


async def test_a_mismatch_takes_back_heights_the_project_already_holds(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Collars were filled while the project had no surveyed collars; a later
    # upload brought local-grid RLs. The earlier heights are withdrawn.
    refs = [{"lon": -160.55, "lat": 55.19, "elevation": 1050.0}]
    conn = _Conn([], refs, candidates=3, pending=0, held=3)
    _fake_lookup(monkeypatch, {0: 45.0})
    out = await _fill(conn)
    assert m._TERRAIN_CLEAR_ALL in conn.executed
    assert out.collars_terrain_datum_mismatch == 3


async def test_surveyed_collars_that_could_not_be_read_fill_nothing(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Failing open here stamps sea-level heights on a local-grid project
    # permanently (the lookup is recorded as current), so "could not measure"
    # means "do not fill this run".
    refs = [{"lon": -160.55, "lat": 55.19, "elevation": 1050.0}]
    budget = _budget()
    conn = _Conn(_targets(2), refs)
    calls = _fake_lookup(monkeypatch, {})
    out = await _fill(conn, budget)
    assert conn.writes == [] and out.collars_terrain_elevation_filled == 0
    assert len(calls) == 1  # the targets were never looked up
    assert budget.tripped


async def test_surveyed_collars_with_no_ground_in_the_model_fill_nothing(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = [{"lon": -160.55, "lat": 55.19, "elevation": 12.0}]
    conn = _Conn(_targets(2), refs)
    calls = _fake_lookup(monkeypatch, {0: None})
    await _fill(conn)
    assert conn.writes == [] and len(calls) == 1


async def test_a_project_whose_surveys_agree_with_the_model_is_filled(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = [{"lon": -160.55, "lat": 55.19, "elevation": 62.0}]
    conn = _Conn(_targets(1), refs)
    _fake_lookup(monkeypatch, {0: 50.0}, {0: 45.0})  # 12 m apart: canopy, not a datum
    out = await _fill(conn)
    assert conn.writes[0][3] == [45.0]
    assert out.collars_terrain_elevation_filled == 1


async def test_an_out_of_range_height_is_recorded_as_no_ground(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _Conn(_targets(1))
    _fake_lookup(monkeypatch, {0: -32767.0})
    out = await _fill(conn)
    assert conn.writes[0][3] == [None]
    assert out.collars_terrain_no_ground == 1


async def test_disabled_reads_nothing_and_withdraws_stored_heights(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dem.URL_TEMPLATE_ENV, "")
    calls = _fake_lookup(monkeypatch, {})
    conn = _Conn(_targets(1), held=2)
    await _fill(conn)
    assert calls == [] and conn.fetches == []
    # Heights from an earlier, enabled run must not outlive the switch-off.
    assert conn.executed == [m._TERRAIN_CLEAR_ALL]


async def test_an_idle_project_with_no_terrain_heights_makes_no_reference_lookup(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = [{"lon": 10.0, "lat": 50.0, "elevation": 1000.0}]
    calls = _fake_lookup(monkeypatch, {})
    conn = _Conn([], refs, candidates=4, pending=0, held=0)
    await _fill(conn)
    assert calls == [] and conn.fetches == []


async def test_held_heights_are_still_checked_against_the_datum_when_nothing_is_pending(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A local-grid project already holding terrain heights must lose them
    # even with nothing new to fill.
    refs = [{"lon": 10.0, "lat": 50.0, "elevation": 1000.0}]
    calls = _fake_lookup(monkeypatch, {0: 0.0})
    conn = _Conn([], refs, candidates=4, pending=0, held=2)
    await _fill(conn)
    assert len(calls) == 1
    assert m._TERRAIN_CLEAR_ALL in conn.executed


async def test_nothing_to_look_up_makes_no_lookup(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_lookup(monkeypatch, {})
    conn = _Conn([], candidates=0, pending=0)
    await _fill(conn)
    assert calls == []
    assert conn.fetches == []


async def test_nothing_pending_and_no_surveyed_collars_makes_no_lookup(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Everything already has a current lookup: a nightly sweep costs SQL only.
    calls = _fake_lookup(monkeypatch, {})
    conn = _Conn([], candidates=4, pending=0)
    await _fill(conn)
    assert calls == []


async def test_a_database_error_never_escapes(enabled: None) -> None:
    class Broken:
        async def execute(self, *a: object) -> str:
            raise RuntimeError("connection reset")

    # Must not raise: the trace/interval promotions after it still run.
    await m._fill_terrain_elevations(
        Broken(),  # type: ignore[arg-type]
        project_id="p",
        out=m.PromoteSilverToGoldOutput(),
        budget=_budget(),
    )


def test_stale_clearing_covers_moved_and_re_sourced_collars() -> None:
    sql = m._TERRAIN_CLEAR_STALE
    assert "elevation_dem_source IS DISTINCT FROM" in sql
    assert "NOT ST_Equals(c.elevation_dem_geom, c.geom_4326)" in sql
    assert "elevation_dem_m      = NULL" in sql


def test_targets_are_collars_with_no_current_lookup() -> None:
    sql = m._TERRAIN_TARGETS
    assert "c.elevation IS NULL" in sql
    assert "c.elevation_dem_geom IS NULL" in sql


def test_write_never_touches_the_file_elevation() -> None:
    sql = m._TERRAIN_WRITE
    assert "SET elevation_dem_m" in sql
    assert "elevation =" not in sql.replace("elevation_dem", "")
    # A file elevation that landed between the read and the write wins.
    assert "c.elevation IS NULL" in sql


def test_every_reader_uses_the_shared_effective_elevation() -> None:
    import inspect

    # Read as text: importing app.agent.tools needs the full service settings.
    app = Path(__file__).resolve().parents[1] / "app"
    tools = (app / "agent/tools.py").read_text()
    viz = (app / "routers/visualizations.py").read_text()

    assert "EFFECTIVE_ELEVATION_SQL" in inspect.getsource(m._promote_traces)
    assert "COALESCE({EFFECTIVE_ELEVATION_SQL}, 0.0)::float" in tools
    assert "{EFFECTIVE_ELEVATION_SQL} AS elevation" in viz
    # None of them is left on the file's elevation alone.
    assert "COALESCE(c.elevation, 0.0)" not in tools
    assert "COALESCE(c.elevation, c.elevation_dem_m)" not in inspect.getsource(m._promote_traces)


def test_the_range_guard_matches_the_check_constraint() -> None:
    migration = (
        Path(__file__).resolve().parents[3]
        / "database/migrations/2026_10_06_110000_add_terrain_elevation_to_collars.php"
    ).read_text()
    assert f"BETWEEN {int(m._TERRAIN_MIN_M)} AND {int(m._TERRAIN_MAX_M)}" in migration
    assert math.isclose(m._TERRAIN_MIN_M, -500.0)


# ---------------------------------------------------------------------------
# Sampling: one bad tile or point, and longitudes outside -180..180
# ---------------------------------------------------------------------------


class _Src:
    def __enter__(self) -> _Src:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _sampler_config() -> dem.DemConfig:
    return dem.DemConfig(url_template="https://example.test/{tile}.tif", source="copernicus_glo30", timeout_s=5.0)


def test_a_non_io_error_on_one_tile_keeps_the_other_tiles(monkeypatch: pytest.MonkeyPatch) -> None:
    # A CRS error on one point must not discard what another tile already read.
    monkeypatch.setattr("rasterio.open", lambda location: _Src())

    def bilinear(src: object, lon: float, lat: float) -> float:
        if lon > 15.0:
            raise ValueError("bad transform")
        return 5.0

    monkeypatch.setattr(dem, "_bilinear", bilinear)
    out = dem.sample_elevations_sync([(10.0, 50.0), (20.0, 50.0)], _sampler_config())
    assert out == {0: 5.0}  # point 1 is absent: retried next run, not recorded as no ground


def test_longitudes_are_wrapped_into_the_raster_range(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    monkeypatch.setattr("rasterio.open", lambda location: _Src())
    monkeypatch.setattr(dem, "_bilinear", lambda src, lon, lat: seen.append(lon) or 1.0)
    out = dem.sample_elevations_sync([(200.5, -30.0)], _sampler_config())
    assert out == {0: 1.0}
    assert seen == [pytest.approx(-159.5)]
    assert dem._wrap_lon(-180.0) == -180.0
    assert dem._wrap_lon(180.0) == -180.0
    assert math.isnan(dem._wrap_lon(float("nan")))

