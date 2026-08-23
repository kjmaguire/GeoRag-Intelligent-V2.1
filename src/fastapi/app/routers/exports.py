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

from fastapi import APIRouter, Depends, Request
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


class ExportRequest(BaseModel):
    project_id: str
    format: str = "shapefile"  # "shapefile" | "geopackage"


async def _fetch_collars(project_id: str, pg_pool):
    """Fetch collar records and return as a GeoDataFrame in WGS84."""
    sql = (
        "SELECT collar_id::text, hole_id, total_depth, hole_type, status, "
        "drill_date::text, "
        "ST_X(ST_Transform(geom, 4326)) AS longitude, "
        "ST_Y(ST_Transform(geom, 4326)) AS latitude "
        "FROM silver.collars WHERE project_id = $1 ORDER BY hole_id"
    )
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
        return {"error": "No collar data found for this project"}

    tmpdir = tempfile.mkdtemp(prefix="georag_shp_")
    shp_path = os.path.join(tmpdir, "georag_collars.shp")

    # Hard rule 2 — GeoPandas writes through GDAL/OGR, which is sync and
    # CPU-bound. A 40,000-collar project is seconds of blocking on the
    # event loop that serves every other request in this worker.
    def _write_bundle() -> str:
        gdf.to_file(shp_path, driver="ESRI Shapefile")
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
        return {"error": "No collar data found for this project"}

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
