"""CC-03 Item 5 — Coverage density endpoint.

Wraps the silver.coverage_density() PostGIS function into a FastAPI
GET that returns a GeoJSON FeatureCollection ready for MapLibre.

Anti-bias UX contract (Anna 2026-05-23):
  - Every cell carries a `bias_warning` boolean.
  - Sparse cells (count < 3) flip the warning; the frontend renders a
    dashed border + the standard warning copy.
  - Empty cells are filtered out by the SQL function — they aren't
    rendered. Coverage absence is communicated by the contrast between
    populated cells and the cell-free background, not by drawing zero
    cells everywhere.

The frontend renders this directly as a MapLibre `fill` layer; no
client-side reaggregation. The hexgrid is computed in EPSG:3857 (web
mercator, metres) and reprojected to 4326 (WGS84) for the response.
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.services.auth import UserContext, extract_user_context, verify_service_key

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/coverage",
    tags=["coverage"],
    dependencies=[Depends(verify_service_key)],
)


CoverageKind = Literal["collars", "reports", "spatial_features"]


class CoverageFeatureProperties(BaseModel):
    record_count: int = Field(..., ge=0)
    bias_warning: bool = Field(
        ...,
        description=(
            "True when the cell has fewer than 3 records — sparse coverage "
            "should be visually distinguished and labelled with the standard "
            "'results may reflect historical exploration bias' copy."
        ),
    )


class CoverageFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: dict
    properties: CoverageFeatureProperties


class CoverageDensityResponse(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    project_id: uuid.UUID
    kind: CoverageKind
    cell_size_m: int
    feature_count: int
    max_count: int
    features: list[CoverageFeature]


@router.get(
    "/density",
    response_model=CoverageDensityResponse,
)
async def coverage_density(
    request: Request,
    project_id: uuid.UUID = Query(..., description="silver.projects.project_id"),
    kind: CoverageKind = Query(
        "collars",
        description=(
            "What to count per cell: 'collars' (drill holes), 'reports' "
            "(silver.reports with geom set), or 'spatial_features' "
            "(generic GIS layer features)."
        ),
    ),
    cell_size_m: int = Query(
        1000,
        description="Hex cell long-axis in metres. Must be one of 500 / 1000 / 5000 / 10000.",
    ),
    _user: UserContext = Depends(extract_user_context),
) -> CoverageDensityResponse:
    """Coverage density GeoJSON for MapLibre fill rendering.

    Responses
    ---------
    200  application/json — FeatureCollection of hex cells with count > 0
    422  application/json — invalid kind / cell_size_m
    503  application/json — pg pool not initialised
    """
    if cell_size_m not in (500, 1000, 5000, 10000):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="cell_size_m_must_be_500_1000_5000_or_10000",
        )

    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pg_pool_not_ready",
        )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ST_AsGeoJSON(cell_polygon)::jsonb AS geom_json,"
            "       record_count, bias_warning"
            "  FROM silver.coverage_density($1, $2, $3)",
            project_id, kind, cell_size_m,
        )

    features: list[CoverageFeature] = []
    max_count = 0
    for row in rows:
        rc = int(row["record_count"])
        if rc > max_count:
            max_count = rc
        features.append(
            CoverageFeature(
                geometry=row["geom_json"],
                properties=CoverageFeatureProperties(
                    record_count=rc,
                    bias_warning=bool(row["bias_warning"]),
                ),
            )
        )

    logger.info(
        "coverage_density project=%s kind=%s cell=%dm features=%d max=%d",
        project_id, kind, cell_size_m, len(features), max_count,
    )

    return CoverageDensityResponse(
        project_id=project_id,
        kind=kind,
        cell_size_m=cell_size_m,
        feature_count=len(features),
        max_count=max_count,
        features=features,
    )
