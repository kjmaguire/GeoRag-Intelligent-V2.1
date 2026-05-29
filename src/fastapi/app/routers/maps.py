"""CC-01 Item 3 (stub) — Map ingest scaffold.

This module exposes the future map-digitisation surface as a versioned
HTTP contract that returns ``501 Not Implemented`` today. Landing the
contract early means:

  * downstream Laravel + UI work can stub against a real route signature,
  * the gate (Milestone 2 VL-model-size decision) is documented in code
    and not just in a kickoff doc,
  * the response shape callers will eventually receive is captured here
    so anyone wiring the full implementation later doesn't have to
    invent it.

When the gate flips (post-Milestone 2 benchmark):

  1. Replace the 501 raise with a call to the real VL pipeline
     (Qwen2.5-VL section summarisation, scale/grid/legend detection,
     control-point extraction → ``silver.control_points``).
  2. The response shape (``MapIngestResponse`` below) is the contract
     callers depend on — extend it, don't redefine it.
  3. Remove the ``X-Map-Ingest-Status: stub`` and ``Retry-After`` header
     from the response.

See:
  * docs/cc01_partial_items_kickoff.md — CC-01 Item 3 plan
  * database/migrations/2026_05_23_020000_create_silver_control_points.php
    — schema scaffold for the eventual output
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.services.auth import UserContext, extract_user_context, verify_service_key

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/maps",
    tags=["maps"],
    dependencies=[Depends(verify_service_key)],
)


# ---------------------------------------------------------------------------
# Request / response contracts (locked early so downstream code can stub)
# ---------------------------------------------------------------------------


class MapIngestRequest(BaseModel):
    """Caller specifies where the source map is — exactly one of source_pdf_id
    or source_image_key must be set."""

    workspace_id: uuid.UUID = Field(..., description="Tenancy scope (RLS).")
    source_pdf_id: str | None = Field(
        None,
        pattern=r"^[0-9a-f]{64}$",
        description="§04p Bronze SHA-256 when the source is a PDF page.",
    )
    source_page: int | None = Field(
        None, ge=1, description="1-indexed page when source_pdf_id is set."
    )
    source_image_key: str | None = Field(
        None,
        description="Bronze object key when the source is a standalone image (TIFF/PNG/JPG).",
    )
    expected_crs_epsg: int | None = Field(
        None,
        ge=1024,
        le=32767,
        description=(
            "Optional hint — when the caller already knows the CRS code on the source. "
            "VL pipeline will still verify against legend / scale markers."
        ),
    )


class MapIngestControlPoint(BaseModel):
    """One ground-control point — mirrors silver.control_points row shape."""

    point_id: uuid.UUID
    pixel_x: float
    pixel_y: float
    longitude: float
    latitude: float
    georef_confidence: float = Field(..., ge=0.0, le=1.0)
    method: Literal[
        "qwen_vl_grid",
        "qwen_vl_legend",
        "qwen_vl_manual",
        "human_pick",
        "survey_marker",
    ]
    notes: str | None = None


class MapIngestResponse(BaseModel):
    """Locked contract — extend, don't redefine."""

    workspace_id: uuid.UUID
    source_pdf_id: str | None
    source_page: int | None
    source_image_key: str | None
    detected_crs_epsg: int | None
    scale_denominator: float | None = Field(
        None,
        description="The map's declared scale denominator (e.g. 50000 for 1:50,000). NULL when not detected.",
    )
    north_arrow_detected: bool
    legend_bbox: tuple[float, float, float, float] | None = Field(
        None,
        description="(x0, y0, x1, y1) in PDF user-space of the detected legend block. NULL when not found.",
    )
    control_points: list[MapIngestControlPoint] = Field(
        default_factory=list,
        description="Ordered list of control points written to silver.control_points.",
    )
    affine_residual_rms_m: float | None = Field(
        None,
        ge=0.0,
        description="Root-mean-square residual (metres) of the affine fit through the control points. NULL until ≥3 points.",
    )
    mean_control_confidence: float | None = Field(None, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# POST /maps/ingest — stub
# ---------------------------------------------------------------------------


@router.post(
    "/ingest",
    response_model=MapIngestResponse,
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    responses={
        501: {
            "description": (
                "Gate: full VL map digitisation is blocked on the Milestone 2 "
                "Qwen-VL benchmark. Schema (silver.control_points) + contract "
                "are locked; implementation lands once the model-size decision "
                "is signed off."
            )
        }
    },
)
async def ingest_map(
    body: MapIngestRequest,
    user: UserContext = Depends(extract_user_context),
) -> Response:
    """Stub — returns 501 with a documented gate header."""
    # Validate the source-XOR upfront so callers building toward the real
    # contract get a 422 today rather than a 501-swallowed bad request.
    if (body.source_pdf_id is None) == (body.source_image_key is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="exactly_one_of_source_pdf_id_or_source_image_key_required",
        )
    if body.source_pdf_id is not None and body.source_page is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="source_page_required_when_source_pdf_id_is_set",
        )

    logger.info(
        "maps.ingest 501 stub — workspace=%s source=%s gate=milestone-2-vl",
        body.workspace_id,
        body.source_pdf_id or body.source_image_key,
    )
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "code": "not_implemented",
            "gate": "milestone-2-vl-decision",
            "doc": "docs/cc01_partial_items_kickoff.md#item-3-stub--map-ingest-scaffold",
        },
        headers={"Retry-After": "milestone-2-vl-decision"},
    )
