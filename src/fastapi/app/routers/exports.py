"""GDAL-based export endpoints for formats requiring Python geospatial libs.

Laravel's GenerateExportJob proxies to these endpoints for Shapefile and
GeoPackage exports, since GDAL/OGR and geopandas are only available in
the FastAPI container.

Endpoints:
    POST /internal/exports/shapefile   — returns a ZIP of .shp/.shx/.dbf/.prj
    POST /internal/exports/geopackage  — returns a .gpkg file
"""

import logging
import os
import tempfile
import zipfile

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/exports", tags=["exports"])


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


@router.post("/shapefile")
async def export_shapefile(body: ExportRequest, request: Request):
    """Generate an ESRI Shapefile ZIP from collar data."""
    gdf = await _fetch_collars(body.project_id, request.app.state.pg_pool)

    if gdf.empty:
        return {"error": "No collar data found for this project"}

    tmpdir = tempfile.mkdtemp(prefix="georag_shp_")
    shp_path = os.path.join(tmpdir, "georag_collars.shp")
    gdf.to_file(shp_path, driver="ESRI Shapefile")

    # Bundle all shapefile components into a ZIP
    zip_path = os.path.join(tmpdir, "georag_collars_shapefile.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            fpath = shp_path.replace(".shp", ext)
            if os.path.exists(fpath):
                zf.write(fpath, os.path.basename(fpath))

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
    )


@router.post("/geopackage")
async def export_geopackage(body: ExportRequest, request: Request):
    """Generate a GeoPackage (.gpkg) from collar data."""
    gdf = await _fetch_collars(body.project_id, request.app.state.pg_pool)

    if gdf.empty:
        return {"error": "No collar data found for this project"}

    tmpdir = tempfile.mkdtemp(prefix="georag_gpkg_")
    gpkg_path = os.path.join(tmpdir, "georag_collars.gpkg")
    gdf.to_file(gpkg_path, driver="GPKG", layer="collars")

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
    )
