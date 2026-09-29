"""GDAL-based export endpoints for formats requiring Python geospatial libs.

Laravel's GenerateExportJob proxies to these endpoints for Shapefile and
GeoPackage exports, since GDAL/OGR and geopandas are only available in
the FastAPI container.

Endpoints:
    POST /internal/exports/shapefile   — returns a ZIP of .shp/.shx/.dbf/.prj
    POST /internal/exports/geopackage  — returns a .gpkg file
"""

import asyncio
import logging
import os
import shutil
import tempfile
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.db.scoped_pool import bind_workspace_scope
from app.services.auth import verify_service_key

logger = logging.getLogger(__name__)

# main.py states that every /internal route requires X-Service-Key. This
# router was one of two that did not, so any workload inside the Container
# Apps environment could POST a project_id here with no header at all and
# get back a ZIP of that project's entire collar table. _fetch_collars
# filters on project_id alone and binds no workspace RLS context, so the
# project_id was the only thing between a caller and the data.
router = APIRouter(
    prefix="/internal/exports",
    tags=["exports"],
    dependencies=[Depends(verify_service_key)],
)


#: The projected CRS each exported collar's easting/northing/epsg are given
#: in: the PROJECT's declared ``crs_epsg`` when it is a projected system
#: (its spatial_ref_sys WKT is a PROJCS — so its epsg and unit agree with the
#: numbers, feet for a US state plane), else the UTM zone the collar
#: itself sits in — 326xx north / 327xx south, longitude clamped so +180 is
#: zone 60 (same rule as promote_silver_to_gold._collar_local_utm). Never a
#: hard-coded zone: a fixed 32613 is what put Alaskan holes in "UTM 13N"
#: metres a modelling tool would then misplace if it trusted the epsg.
_COLLAR_EXPORT_SQL = """
SELECT c.collar_id::text, c.hole_id, c.total_depth, c.elevation, c.azimuth,
       c.dip, c.hole_type, c.status, c.drill_date::text,
       ST_X(ST_Transform(c.geom_4326, m.epsg)) AS easting,
       ST_Y(ST_Transform(c.geom_4326, m.epsg)) AS northing,
       m.epsg                                  AS epsg,
       ST_X(c.geom_4326)                       AS longitude,
       ST_Y(c.geom_4326)                       AS latitude
  FROM silver.collars c
  LEFT JOIN LATERAL (
      SELECT COALESCE(
          (SELECT s.srid
             FROM silver.projects p
             JOIN public.spatial_ref_sys s ON s.srid = p.crs_epsg
            WHERE p.project_id = c.project_id
              AND s.srtext LIKE 'PROJCS%'),
          CASE WHEN ST_Y(c.geom_4326) >= 0 THEN 32600 ELSE 32700 END
            + LEAST(60, GREATEST(1, floor((ST_X(c.geom_4326) + 180.0) / 6.0)::int + 1))
      ) AS epsg
  ) m ON c.geom_4326 IS NOT NULL
 WHERE c.project_id = $1
 ORDER BY c.hole_id
"""


class ExportRequest(BaseModel):
    project_id: str
    format: str = "shapefile"  # "shapefile" | "geopackage"


async def _fetch_collars(project_id: str, pg_pool):
    """Fetch collar records and return as a GeoDataFrame in WGS84."""
    # elevation / azimuth / dip / total_depth plus the native-CRS easting,
    # northing and EPSG are what a modelling tool (Leapfrog, QGIS, Micromine)
    # needs to place and desurvey a hole in 3D. The export used to carry only
    # the WGS84 point and total_depth, so a hole could be drawn on a map but
    # not reconstructed. Native coordinates come from the geometry itself
    # (not the float easting/northing columns, which hold whatever each
    # source file used) so they always agree with the ``epsg`` reported
    # next to them.
    #
    # CRS at every hop: source -> geom_4326 (ingest) -> _COLLAR_EXPORT_SQL's
    # metric CRS here. That used to be ST_X/ST_Y of silver.collars.geom,
    # which was pinned to EPSG:32613 for every collar on earth; the column
    # was retired 2026-09-29, so the export now projects geom_4326 itself.
    sql = _COLLAR_EXPORT_SQL
    async with pg_pool.acquire() as conn:
        # Bind the tenant before reading. This used to be a bare acquire on
        # a query filtered by project_id alone, so the caller's project_id
        # was the only thing between them and the data — and RLS was not
        # armed to catch a wrong one, because every canonical policy treats
        # an unset app.workspace_id as permissive.
        #
        # SET LOCAL inside a transaction: the value is discarded at COMMIT,
        # so it cannot ride a pooled connection into the next request.
        async with conn.transaction():
            workspace_id = await conn.fetchval(
                "SELECT workspace_id::text FROM silver.projects "
                "WHERE project_id = $1::uuid",
                project_id,
            )
            if workspace_id:
                await bind_workspace_scope(
                    conn,
                    workspace_id=workspace_id,
                    site="routers.exports",
                )
            rows = await conn.fetch(sql, project_id)

    import geopandas as gpd

    if not rows:
        return gpd.GeoDataFrame()

    import pandas as pd
    df = pd.DataFrame([dict(r) for r in rows])
    gdf = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df["longitude"], df["latitude"]),
        crs="EPSG:4326",
    )
    return gdf


# DBF (the shapefile attribute table) caps field names at 10 characters and
# GDAL silently truncates longer ones — ``total_depth`` used to arrive in the
# modelling tool as ``total_dept``. Rename explicitly for the shapefile path
# only; the GeoPackage keeps the full column names.
_SHAPEFILE_FIELD_NAMES: dict[str, str] = {
    "total_depth": "tot_depth",
}


def _shapefile_columns(gdf):  # type: ignore[no-untyped-def]
    """Return ``gdf`` with every attribute name fitting the DBF 10-char cap."""
    renamed = gdf.rename(columns=_SHAPEFILE_FIELD_NAMES)
    too_long = [
        c for c in renamed.columns if c != renamed.geometry.name and len(c) > 10
    ]
    if too_long:  # pragma: no cover — guard against a future column addition
        raise ValueError(f"shapefile field names exceed 10 chars: {too_long}")
    return renamed


def _no_collars() -> HTTPException:
    """404 for a project with no collars.

    Used to be ``{"error": ...}`` with HTTP 200, which Laravel's exporters
    saved verbatim as a ``.zip`` / ``.gpkg`` the user then downloaded as a
    corrupt file. A non-2xx makes GenerateExportJob mark the export failed
    with this message instead.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="No collar data found for this project",
    )


def _cleanup(tmpdir: str) -> BackgroundTask:
    """Delete the working directory once the response has been sent.

    Both handlers used `tempfile.mkdtemp` and never removed the directory.
    FileResponse streams from it, so it cannot be deleted before the
    response is written — which is what a `with TemporaryDirectory()` block
    would do. Nothing deleted it after, either: every export left a
    shapefile bundle or a GeoPackage behind in the container's /tmp for the
    life of the revision.

    Starlette runs a BackgroundTask after the body is flushed, which is
    exactly the hook this needed.
    """
    return BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True)


@router.post("/shapefile")
async def export_shapefile(body: ExportRequest, request: Request):
    """Generate an ESRI Shapefile ZIP from collar data."""
    gdf = await _fetch_collars(body.project_id, request.app.state.pg_pool)

    if gdf.empty:
        raise _no_collars()

    tmpdir = tempfile.mkdtemp(prefix="georag_shp_")
    shp_path = os.path.join(tmpdir, "georag_collars.shp")

    # Hard rule 2 — GeoPandas writes through GDAL/OGR, which is sync and
    # CPU-bound. A 40,000-collar project is seconds of blocking on the
    # event loop that serves every other request in this worker.
    shp_gdf = _shapefile_columns(gdf)

    def _write_bundle() -> str:
        shp_gdf.to_file(shp_path, driver="ESRI Shapefile")
        zip_path = os.path.join(tmpdir, "georag_collars_shapefile.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                fpath = shp_path.replace(".shp", ext)
                if os.path.exists(fpath):
                    zf.write(fpath, os.path.basename(fpath))
        return zip_path

    try:
        zip_path = await asyncio.to_thread(_write_bundle)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    logger.info(
        "export_shapefile: project=%s records=%d zip_size=%d",
        body.project_id,
        len(gdf),
        os.path.getsize(zip_path),
    )

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename="georag_collars_shapefile.zip",
        background=_cleanup(tmpdir),
    )


@router.post("/geopackage")
async def export_geopackage(body: ExportRequest, request: Request):
    """Generate a GeoPackage (.gpkg) from collar data."""
    gdf = await _fetch_collars(body.project_id, request.app.state.pg_pool)

    if gdf.empty:
        raise _no_collars()

    tmpdir = tempfile.mkdtemp(prefix="georag_gpkg_")
    gpkg_path = os.path.join(tmpdir, "georag_collars.gpkg")

    try:
        await asyncio.to_thread(
            gdf.to_file, gpkg_path, driver="GPKG", layer="collars",
        )
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    logger.info(
        "export_geopackage: project=%s records=%d gpkg_size=%d",
        body.project_id,
        len(gdf),
        os.path.getsize(gpkg_path),
    )

    return FileResponse(
        gpkg_path,
        media_type="application/geopackage+sqlite3",
        filename="georag_collars.gpkg",
        background=_cleanup(tmpdir),
    )
