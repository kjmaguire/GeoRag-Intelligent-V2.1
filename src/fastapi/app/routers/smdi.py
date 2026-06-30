"""SMDI features endpoint — plan v1.1 Phase 6.

Returns the full SMDI FeatureCollection (~6,012 points, ~300 KB gzipped)
served from the local `public.smdi_deposits` table populated by the
`smdi_deposits_refresh` Dagster asset.

The plan's original intent was to proxy + cache the paginated upstream
ArcGIS REST response. Since Dagster already pulls upstream daily into
PostGIS, this endpoint reads from the local copy:
  - faster (single SQL read vs. 4 paginated HTTP requests)
  - upstream-friendly (zero load on Saskatchewan's FeatureServer)
  - already 24h-fresh by virtue of the daily Dagster schedule

If a stricter freshness budget surfaces, swap the SQL read for a live
upstream paginated fetch — the response contract stays the same.

See:
  - docs/handoffs/smdi_ingestion_2026_05_25.md — full plan reconciliation
  - src/dagster/georag_dagster/assets/smdi_deposits.py — daily refresh
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.services.auth import UserContext, extract_user_context, verify_service_key

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/public-geo/smdi",
    tags=["public-geo", "smdi"],
    dependencies=[Depends(verify_service_key)],
)


class SmdiProperties(BaseModel):
    """Tile-payload-friendly subset of the public.smdi_deposits columns.

    Mirrors the Martin tile source property list (see docker/martin/martin.yaml)
    plus the canonical SMDI identifier so callers can deep-link to the
    upstream record viewer at mineraldeposits.saskatchewan.ca.
    """

    smdi: str | None = None
    name: str | None = None
    primary_commodities: str | None = None
    associated_commodities: str | None = None
    symbology_grouping: str | None = None
    status: str | None = None
    production: bool | None = None
    reserves_resources: bool | None = None
    discovery_type: str | None = None
    weblink: str | None = None


class SmdiFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: dict[str, Any]
    properties: SmdiProperties


class SmdiFeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    feature_count: int = Field(..., ge=0)
    features: list[SmdiFeature]


# Schema is documented via the SmdiFeatureCollection / SmdiFeature /
# SmdiProperties classes above (OpenAPI). The handler skips Pydantic
# response validation for the unfiltered path so 6,012-feature assembly
# stays inside the middleware request-timeout budget — the heavy lifting
# (FeatureCollection envelope, ST_AsGeoJSON) happens entirely in PostGIS.

# Single SQL statement builds the whole FeatureCollection as a JSON text
# blob server-side. Two interpolations only — the WHERE predicate (built
# from constant fragments) and the parameterised commodity_group value
# (passed via asyncpg). No user-controlled string ever enters the SQL.
_FEATURE_COLLECTION_SQL_TEMPLATE = """
SELECT json_build_object(
    'type', 'FeatureCollection',
    'feature_count', COALESCE(SUM(1), 0),
    'features', COALESCE(json_agg(
        json_build_object(
            'type', 'Feature',
            'geometry', ST_AsGeoJSON(geom)::json,
            'properties', json_build_object(
                'smdi', smdi,
                'name', name,
                'primary_commodities', primary_commodities,
                'associated_commodities', associated_commodities,
                'symbology_grouping', symbology_grouping,
                'status', status,
                'production', production,
                'reserves_resources', reserves_resources,
                'discovery_type', discovery_type,
                'weblink', weblink
            )
        )
        ORDER BY objectid
    ), '[]'::json)
)::text AS body
FROM public.smdi_deposits
{where_sql}
"""


@router.get(
    "/features",
    response_class=Response,
    responses={
        200: {
            "content": {"application/json": {"schema": SmdiFeatureCollection.model_json_schema()}},
            "description": "SMDI FeatureCollection.",
        },
        503: {"description": "pg pool not ready."},
    },
)
async def smdi_features(
    request: Request,
    producers_only: bool = Query(
        False,
        description=(
            "When true, returns only deposits with production=true (≈142 "
            "rows). Cheap server-side filter that saves ~280 KB on the "
            "wire vs. filtering client-side."
        ),
    ),
    commodity_group: str | None = Query(
        None,
        description=(
            "Optional symbology_grouping exact-match filter. Valid values: "
            "'Base Metals', 'Uranium', 'Precious Metals', 'Coal', "
            "'Industrial Materials', 'Other', 'Rare Earth Elements', "
            "'Gemstones', 'Helium', 'Potash / Salt', 'Lithium'."
        ),
    ),
    _user: UserContext = Depends(extract_user_context),
) -> Response:
    """Full SMDI FeatureCollection from public.smdi_deposits.

    Performance budget
    ------------------
    Unfiltered: ≈6,012 features, ≈1.5 MB JSON / ≈300 KB gzipped.
    With producers_only: ≈142 features.

    The full FeatureCollection is assembled by PostGIS in a single SQL
    statement (json_build_object + json_agg) so the Python side never
    iterates 6 K rows — keeps the unfiltered path well under the request-
    timeout middleware budget.
    """
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pg_pool_not_ready",
        )

    where_clauses: list[str] = []
    params: list[Any] = []
    if producers_only:
        where_clauses.append("production = TRUE")
    if commodity_group is not None:
        params.append(commodity_group)
        where_clauses.append(f"symbology_grouping = ${len(params)}")

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    sql = _FEATURE_COLLECTION_SQL_TEMPLATE.format(where_sql=where_sql)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)

    body = row["body"] if row else '{"type":"FeatureCollection","feature_count":0,"features":[]}'

    logger.info(
        "smdi_features producers_only=%s commodity_group=%s bytes=%d",
        producers_only, commodity_group, len(body),
    )

    return Response(content=body, media_type="application/json")
