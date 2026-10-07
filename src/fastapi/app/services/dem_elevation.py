"""Ground elevation under a collar, from a terrain model, when the file had none.

WHY THIS EXISTS
===============

A collar file without an elevation column lands its collars with
``silver.collars.elevation`` NULL. Every 3D reader then draws the hole at
z = 0: the trace builder (``promote_silver_to_gold._promote_traces``) uses
``0.0`` for a NULL elevation and the Workspace 3D views do the same. RedStar's
five trenches on Unga Island, Alaska sat at sea level for exactly this reason
(project diagnostics, 2026-10-06).

Kyle asked on 2026-10-06 for the gap to be filled from a terrain model. This
module looks the ground surface up; ``promote_silver_to_gold`` writes the
result to ``silver.collars.elevation_dem_m`` and readers that need a z use
``COALESCE(elevation, elevation_dem_m)``.

THE FILE'S ELEVATION ALWAYS WINS
================================

The terrain value goes in its OWN column and is never written to
``elevation``. So a surveyed elevation that arrives later (a re-upload with
an RL column) takes over without anything having to notice, and every reader
that does not opt into the fallback — exports, the agent's tools,
``silver.mv_collar_summary`` — still sees NULL, which is the truth about what
the file said.

THE TERRAIN MODEL
=================

Copernicus DEM GLO-30: global, 1 arc-second (~30 m), heights in metres above
the EGM2008 geoid (orthometric, i.e. "metres above sea level" in the sense a
collar RL usually means), published as one Cloud-Optimised GeoTIFF per 1°x1°
cell in the public AWS Open Data bucket ``copernicus-dem-30m``. No key, no
account, and rasterio reads only the blocks it needs with HTTP range requests.
Cells that are all ocean are simply not published (HTTP 404), which is a
definitive "no ground here", not an error.

It is a SURFACE model (DSM), not bare earth: in forest it reads the canopy,
typically 5-30 m high. For a trench or a hole on open ground it is the ground.

Licence: free for any use, including commercial, with attribution — "produced
using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and
Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA;
all rights reserved".

Configuration
-------------

``COLLAR_DEM_URL_TEMPLATE``
    Where the tiles are. ``{tile}`` is replaced with the Copernicus tile name
    (``Copernicus_DSM_COG_10_N55_00_W161_00_DEM``). Defaults to the public
    bucket. A local mirror works (an ``https://`` URL or a filesystem path).
    **Set it to the empty string to turn the lookup off** — an air-gapped
    install with no mirror should, so the promotion does not spend its
    timeout on a host it cannot reach.
``COLLAR_DEM_SOURCE``
    The label written to ``silver.collars.elevation_dem_source`` (at most 32
    characters, the column's width; a longer label is cut with a warning).
    Defaults to ``copernicus_glo30``; change it when the template points at a
    different model, and every collar is looked up again (a lookup made with
    another source is treated as stale).
``COLLAR_DEM_TIMEOUT_S``
    Per-request HTTP timeout, seconds (default 20).

What the host can see
---------------------

Reading a tile with HTTP range requests tells the bucket operator (and its
access logs) WHICH 1° tile was opened and which blocks of it were read.
Copernicus COG blocks are 1024 px, about 28 km, so a collar's position is
disclosed to roughly that granularity — never a coordinate, a hole id, a
project or workspace name. For a client whose drill-target locations are
confidential even at that resolution, turn the lookup off
(``COLLAR_DEM_URL_TEMPLATE=""``) or point it at a mirror inside the
deployment.

Time bounds
-----------

``promote_silver_to_gold`` calls this once per project, inside a 20-minute
task. Three limits keep a slow or unreachable host from eating that:
a per-call ``deadline`` (a monotonic timestamp checked between points, so a
batch returns what it read instead of being abandoned), ``TerrainBudget``
(one wall-clock allowance for the whole run, shared by every project), and a
circuit breaker on that budget that trips when a call reads NOTHING, so later
projects skip the host instead of each waiting out its own timeouts.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import statistics
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

log = logging.getLogger("georag.dem_elevation")

URL_TEMPLATE_ENV = "COLLAR_DEM_URL_TEMPLATE"
SOURCE_ENV = "COLLAR_DEM_SOURCE"
TIMEOUT_ENV = "COLLAR_DEM_TIMEOUT_S"

DEFAULT_URL_TEMPLATE = "https://copernicus-dem-30m.s3.amazonaws.com/{tile}/{tile}.tif"
DEFAULT_SOURCE = "copernicus_glo30"
DEFAULT_TIMEOUT_S = 20.0

#: Width of silver.collars.elevation_dem_source.
SOURCE_MAX_LEN = 32

#: Wall-clock allowance for ALL terrain lookups in one promote run, and the
#: most a single project may take of it. The task has 20 minutes for
#: everything; terrain is an enrichment and gets a quarter of that at most.
RUN_BUDGET_S = 300.0
PROJECT_BUDGET_S = 120.0

#: A project that mixes collars WITH a file elevation and collars without one
#: gets the terrain fallback only when the two agree. Many mines carry collar
#: RLs on a local grid (elevation + 1000 m, + 5000 m, ...): filling the gaps
#: with sea-level heights there would draw those holes a kilometre away from
#: their neighbours, which is worse than the z = 0 it replaces because it
#: looks plausible. The median (file - terrain) over the project's surveyed
#: collars measures the offset. 50 m tolerates the DSM's canopy bias and an
#: older survey's datum, and is far below any local-grid offset seen in
#: practice.
MAX_DATUM_OFFSET_M = 50.0

#: Surveyed collars sampled to measure that offset. The median of 50 is as
#: stable as the median of 5,000 for this purpose and costs 1% of the reads.
DATUM_CHECK_SAMPLE = 50


#: The z every 3D reader draws a collar at, for the collar aliased ``c``: the
#: file's elevation, else the terrain height — but only a terrain height that
#: was looked up AT the collar's present position. A collar that has moved
#: (re-uploaded under its correct CRS) keeps its old ``elevation_dem_m`` until
#: the next promotion looks it up again; this keeps readers from drawing it at
#: the old location's ground in the meantime. NULL when neither applies; the
#: caller decides what NULL means (the trace builder and the long section use
#: 0, the agent tool reports 0 as before).
EFFECTIVE_ELEVATION_SQL = (
    "COALESCE(c.elevation, CASE WHEN c.elevation_dem_geom IS NOT NULL "
    "AND ST_Equals(c.elevation_dem_geom, c.geom_4326) THEN c.elevation_dem_m END)"
)


@dataclass(frozen=True)
class DemConfig:
    url_template: str
    source: str
    timeout_s: float

    @property
    def enabled(self) -> bool:
        return bool(self.url_template)


def config_from_env() -> DemConfig:
    """Read the lookup's configuration. Read per call, not at import."""
    raw_timeout = os.environ.get(TIMEOUT_ENV, "")
    try:
        timeout_s = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_S
    except ValueError:
        log.warning("dem_elevation: %s=%r is not a number; using %s", TIMEOUT_ENV, raw_timeout, DEFAULT_TIMEOUT_S)
        timeout_s = DEFAULT_TIMEOUT_S
    source = os.environ.get(SOURCE_ENV, "").strip() or DEFAULT_SOURCE
    if len(source) > SOURCE_MAX_LEN:
        # The label goes into a varchar(32): a longer one would fail every
        # write AFTER the whole remote lookup had been paid for.
        log.warning(
            "dem_elevation: %s=%r is longer than %d characters; using %r",
            SOURCE_ENV,
            source,
            SOURCE_MAX_LEN,
            source[:SOURCE_MAX_LEN],
        )
        source = source[:SOURCE_MAX_LEN]
    return DemConfig(
        url_template=os.environ.get(URL_TEMPLATE_ENV, DEFAULT_URL_TEMPLATE).strip(),
        source=source,
        timeout_s=max(1.0, timeout_s),
    )


class TerrainBudget:
    """One run's wall-clock allowance for terrain lookups, plus a breaker.

    Created once per ``promote`` run and passed to every project's fill.
    ``project_deadline()`` is the monotonic time a single project's lookup
    must stop by; ``trip()`` is called when a lookup read nothing at all (the
    host is down or blocked), after which ``usable`` is False for the rest of
    the run.
    """

    def __init__(self, run_s: float = RUN_BUDGET_S, project_s: float = PROJECT_BUDGET_S) -> None:
        self._end = time.monotonic() + run_s
        self._project_s = project_s
        self.tripped = False

    @property
    def usable(self) -> bool:
        return not self.tripped and time.monotonic() < self._end

    def project_deadline(self) -> float:
        return min(time.monotonic() + self._project_s, self._end)

    def trip(self) -> None:
        self.tripped = True


def copernicus_tile(lon: float, lat: float) -> str:
    """Copernicus GLO-30 tile name for the 1°x1° cell holding (lon, lat).

    Cells are named by their south-west corner: (-160.56, 55.19) is in
    ``N55_00_W161_00``.
    """
    if lon >= 180.0:
        lon -= 360.0
    lat_floor = math.floor(lat)
    lon_floor = math.floor(lon)
    ns = "N" if lat_floor >= 0 else "S"
    ew = "E" if lon_floor >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat_floor):02d}_00_{ew}{abs(lon_floor):03d}_00_DEM"


_HTTP_404 = re.compile(r"http response code:\s*404\b")


def _is_not_found(exc: Exception) -> bool:
    """True when the tile does not exist, as opposed to could not be fetched.

    GDAL reports a missing remote object as ``HTTP response code: 404``, an
    unpublished ``/vsicurl/`` path as "does not exist in the file system", and
    a missing local file as "No such file or directory". The status is matched
    as a status, not as the digits: a connection error naming port 4040 or a
    timeout of 20404 ms is not a 404. Anything else (a timeout, a 403, a 5xx,
    a DNS failure) is transient and must NOT be recorded as "no ground here".
    """
    text = str(exc).lower()
    return (
        bool(_HTTP_404.search(text))
        or "does not exist in the file system" in text
        or "no such file or directory" in text
    )


def _bilinear(src, lon: float, lat: float) -> float | None:  # noqa: ANN001 — rasterio dataset
    """Bilinear sample of band 1 at (lon, lat), or None off the raster/nodata.

    Pixel centres sit half a pixel in from the corners the affine transform
    maps, hence the 0.5 shift before interpolating. Masked (nodata) cells are
    dropped and the remaining weights renormalised, so a collar on a coast
    reads the land cells around it rather than nothing.
    """
    from rasterio.windows import Window  # noqa: PLC0415 — optional heavy import

    x, y = lon, lat
    if src.crs is not None and not src.crs.is_geographic:
        from rasterio.warp import transform as warp_transform  # noqa: PLC0415

        xs, ys = warp_transform("EPSG:4326", src.crs, [lon], [lat])
        x, y = xs[0], ys[0]

    col_f, row_f = ~src.transform * (x, y)
    if not (0.0 <= col_f <= src.width and 0.0 <= row_f <= src.height):
        return None

    c = col_f - 0.5
    r = row_f - 0.5
    c0 = min(max(math.floor(c), 0), max(src.width - 2, 0))
    r0 = min(max(math.floor(r), 0), max(src.height - 2, 0))
    dc = min(max(c - c0, 0.0), 1.0)
    dr = min(max(r - r0, 0.0), 1.0)

    import numpy as np  # noqa: PLC0415

    block = src.read(
        1,
        window=Window(c0, r0, min(2, src.width - c0), min(2, src.height - r0)),
        masked=True,
    )
    mask = np.ma.getmaskarray(block)
    total = 0.0
    weight_sum = 0.0
    for i in range(block.shape[0]):
        for j in range(block.shape[1]):
            if mask[i, j]:
                continue
            w = (dr if i else 1.0 - dr) * (dc if j else 1.0 - dc)
            if w <= 0.0:
                continue
            total += w * float(block[i, j])
            weight_sum += w
    if weight_sum <= 0.0:
        # Every neighbour with weight is nodata; take any valid one before
        # giving up (a coastal collar beside a nodata cell).
        valid = block.compressed()
        return float(valid[0]) if valid.size else None
    value = total / weight_sum
    return value if math.isfinite(value) else None


def _usable_position(lon: float, lat: float) -> bool:
    return math.isfinite(lon) and math.isfinite(lat) and -90.0 <= lat <= 90.0 and -180.0 <= lon < 360.0


def sample_elevations_sync(
    points: Sequence[tuple[float, float]],
    config: DemConfig,
    deadline: float | None = None,
) -> dict[int, float | None]:
    """Terrain height at each (lon, lat), keyed by the point's index.

    A point is ABSENT from the result when it could not be read for a
    transient reason (a tile that would not open, a read that failed, the
    ``deadline`` passing); it maps to None when the model definitively has no
    ground there (unpublished ocean tile, nodata) or the position is not a
    position at all (NaN, out of range). Callers record the second and retry
    the first.

    ``deadline`` is a ``time.monotonic()`` timestamp. It is checked before
    every tile and every point, so a batch that runs out of time returns what
    it read — the caller writes that and the rest is retried — rather than
    being abandoned with nothing. One GDAL request already in flight finishes
    first (at most about three request timeouts).

    Synchronous (GDAL does blocking I/O): call it through
    ``lookup_elevations``, never directly from async code.
    """
    import rasterio  # noqa: PLC0415 — keeps app import time free of GDAL
    from rasterio.errors import RasterioIOError  # noqa: PLC0415

    out: dict[int, float | None] = {}
    by_tile: dict[str, list[int]] = defaultdict(list)
    for index, (lon, lat) in enumerate(points):
        if not _usable_position(lon, lat):
            # Not retryable: recording it keeps one bad position from
            # re-selecting itself (and poisoning its batch) on every run.
            out[index] = None
            continue
        by_tile[copernicus_tile(lon, lat)].append(index)

    def expired() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    timeout = str(int(math.ceil(config.timeout_s)))
    with rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff",
        GDAL_HTTP_TIMEOUT=timeout,
        GDAL_HTTP_CONNECTTIMEOUT=timeout,
        GDAL_HTTP_MAX_RETRY="2",
        GDAL_HTTP_RETRY_DELAY="1",
    ):
        for tile, indexes in by_tile.items():
            if expired():
                log.warning("dem_elevation: out of time; tile %s and later are left for the next promotion", tile)
                break
            location = config.url_template.format(tile=tile)
            try:
                src = rasterio.open(location)
            except RasterioIOError as exc:
                if _is_not_found(exc):
                    for index in indexes:
                        out[index] = None
                else:
                    log.warning(
                        "dem_elevation: tile %s unreadable (%s); %d collar(s) left for the next promotion",
                        tile,
                        exc,
                        len(indexes),
                    )
                continue
            # North to south, then west to east: the order the blocks are
            # stored in, so neighbouring collars share a fetched block
            # instead of re-requesting it.
            indexes.sort(key=lambda i: (-points[i][1], points[i][0]))
            try:
                with src:
                    for index in indexes:
                        if expired():
                            log.warning(
                                "dem_elevation: out of time inside tile %s; the rest are left for the next promotion",
                                tile,
                            )
                            break
                        lon, lat = points[index]
                        out[index] = _bilinear(src, lon, lat)
            except RasterioIOError as exc:
                # What was read before the failure stands; the failing point
                # and the ones after it were never added.
                log.warning(
                    "dem_elevation: reading tile %s failed (%s); its remaining collars are left for the next promotion",
                    tile,
                    exc,
                )
    return out


async def lookup_elevations(
    points: Sequence[tuple[float, float]],
    config: DemConfig | None = None,
    deadline: float | None = None,
) -> dict[int, float | None]:
    """Async wrapper over ``sample_elevations_sync``; {} when disabled.

    Runs in a worker thread (GDAL blocks). The sampler stops itself at
    ``deadline`` (a ``time.monotonic()`` timestamp) and returns what it read;
    the ``wait_for`` here is only a backstop for a call GDAL itself never
    returns from, set a few request timeouts past the deadline. Never raises:
    a terrain lookup is an enrichment, and the trace, interval and structure
    promotions after it must still run.
    """
    cfg = config or config_from_env()
    if not cfg.enabled or not points:
        return {}
    backstop = None
    if deadline is not None:
        backstop = max(0.0, deadline - time.monotonic()) + cfg.timeout_s * 3 + 5.0
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(sample_elevations_sync, points, cfg, deadline),
            timeout=backstop,
        )
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning("dem_elevation: lookup of %d point(s) failed (%s)", len(points), exc)
        return {}


def datum_offset_m(file_minus_dem: Sequence[float]) -> float | None:
    """Median (file elevation - terrain) over surveyed collars, or None."""
    values = [v for v in file_minus_dem if math.isfinite(v)]
    if not values:
        return None
    return float(statistics.median(values))


__all__ = [
    "DATUM_CHECK_SAMPLE",
    "DEFAULT_SOURCE",
    "EFFECTIVE_ELEVATION_SQL",
    "DEFAULT_URL_TEMPLATE",
    "MAX_DATUM_OFFSET_M",
    "PROJECT_BUDGET_S",
    "RUN_BUDGET_S",
    "SOURCE_MAX_LEN",
    "DemConfig",
    "TerrainBudget",
    "config_from_env",
    "copernicus_tile",
    "datum_offset_m",
    "lookup_elevations",
    "sample_elevations_sync",
]
