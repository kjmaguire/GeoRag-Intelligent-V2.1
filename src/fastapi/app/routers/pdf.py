"""PDF Ingestion Subsystem — HTTP endpoints for Stage 2–6.

§04p Phase 1.A endpoints
------------------------
  POST /pdf/render_page   — render a full page to PNG + X-PDF-Provenance header
  POST /pdf/crop_region   — render a cropped region to PNG + X-PDF-Provenance header

§04p Phase 1.B endpoints
------------------------
  GET /pdf/extract_text   — text spans + bboxes + font metadata (pdfminer.six)
  GET /pdf/find_tables    — table matrices + cell bboxes + provenance (pdfplumber)

§04p Phase 1.C-i endpoints
---------------------------
  GET /pdf/find_legends   — typed layout regions with bbox + provenance (Docling)
                            Returns ALL layout regions (text/figure/table/header/
                            footer/formula/title/caption/footnote/list/page_number).
                            Optional ?region_type= filter for server-side filtering.
                            Endpoint name is §04p-specified; the agent or Phase 2
                            extractors filter for legend-shaped regions.

§04p Phase 1.C-ii endpoints
----------------------------
  POST /pdf/ocr_region    — PaddleOCR PP-OCRv5 on a specific page region.
                            Region-targeted only — bbox parameter is REQUIRED and
                            must describe a positive non-degenerate rectangle.
                            Zero-area and inverted bboxes are rejected with 422.
                            Full-page OCR is refused per §04p "never full-page by
                            default" — pass an explicit bbox from find_legends output.

§04p Phase 1.D endpoints
-------------------------
  GET /pdf/summarize_section — Qwen-VL vision-language section summary with
                            claim → provenance map.  Every claim in the response
                            carries a (pdf_id, page, bbox) grounding tuple per
                            the §04p provenance contract.  Non-resolvable claims
                            are rejected at the service layer (VlSummaryShape
                            Pydantic validation — §04i Citation completeness guard).
                            section_kind parameter selects the section_ref variant:
                              page         — single page (requires ?page=N)
                              page_range   — page range (requires ?page_start + ?page_end)
                              layout_region — Docling region (requires ?region_id=UUID)

§04p Phase 2.A endpoints
-------------------------
  GET /pdf/find_coordinates — deterministic regex extraction of geographic
                            coordinates (UTM, lat/lon decimal, lat/lon DMS)
                            from silver.pdf_text_blocks.  Validated via Pydantic
                            bounds-check before return.  The VL model never sees
                            raw coordinate strings — it receives the typed,
                            bounds-checked output of this endpoint instead.
                            Optional ?coord_kind= filter for server-side narrowing.
                            Returns 200 + empty list (NOT 404) when no text blocks
                            are available or no coordinate patterns were found.

Bronze store contract
---------------------
All endpoints accept a ``pdf_id`` query parameter (for GET) or in the request
body (for POST).  The PDF must have already been through Stage 1 preflight and
the normalised bytes stored in the Bronze store at key ``pdfs/{pdf_id}.pdf``.

If the key is absent the endpoint returns 404:
  {"detail": "pdf_not_found"}

Full ingestion-side wiring (the Dagster asset that runs preflight and writes to
Bronze before the agent calls these endpoints) is out of scope for Phase 1.A/B/C-i/C-ii.

Auth
----
Uses the existing verify_service_key + extract_user_context pattern from
other routers (X-Service-Key header + Bearer JWT).  The PDF endpoints are
internal — only callable from the FastAPI-agent process or from Laravel.

Provenance header (Stage 2)
-----------------
Every Stage 2 200 response carries::

    X-PDF-Provenance: <base64-encoded JSON>

The JSON matches the PdfProvenance model exactly:
  {"pdf_id": str, "page": int, "bbox": [x0,y0,x1,y1]|null,
   "source_method": "pypdfium2", "extraction_confidence": 1.0}

extraction_confidence is always 1.0 because pypdfium2 render is lossless.

Provenance in Stage 3 responses (extract_text, find_tables)
-----------------------------------------------------------
Each PdfTextBlock and PdfTable in the JSON response carries a ``provenance``
field (PdfProvenance model) with (pdf_id, page, bbox) per §04p contract.

Provenance in Stage 4 responses (find_legends)
----------------------------------------------
Each PdfLayoutRegion carries a ``provenance`` field with (pdf_id, page, bbox)
and source_method='docling'.  region_confidence may be None for region types
where Docling does not emit a confidence score.

Provenance in Stage 5 responses (ocr_region)
---------------------------------------------
OcrRegionResponse carries a ``provenance`` field with (pdf_id, page, bbox)
and source_method='paddle_ocr'.  extraction_confidence equals mean_confidence
(OCR is lossy — NOT hardcoded 1.0 like pdfminer/pdfplumber paths).
"""

from __future__ import annotations

import base64
import io
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from starlette.responses import StreamingResponse

from app.models.pdf import (
    CoordKind,
    CropRegionRequest,
    ExtractTextResponse,
    FindCoordinatesResponse,
    FindLegendsResponse,
    FindTablesResponse,
    LayoutRegionType,
    OcrLine,
    OcrRegionRequest,
    OcrRegionResponse,
    PdfCoordinate,
    PdfLayoutRegion,
    PdfProvenance,
    PdfTable,
    PdfTextBlock,
    RenderPageRequest,
    SummarizeSectionResponse,
    VlClaim,
)
from app.services.auth import UserContext, extract_user_context, verify_service_key
from app.services.pdf_vl import VlBackendError, VlOutputShapeError, VlSectionTooLargeError

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/pdf",
    tags=["pdf"],
    dependencies=[Depends(verify_service_key)],
)

_BRONZE_PDF_KEY_TEMPLATE = "pdfs/{pdf_id}.pdf"


def _encode_provenance(provenance: PdfProvenance) -> str:
    """Base64-encode a PdfProvenance model for the X-PDF-Provenance header."""
    json_bytes = provenance.model_dump_json().encode()
    return base64.b64encode(json_bytes).decode()


async def _fetch_pdf_bytes(request: Request, pdf_id: str) -> bytes:
    """Retrieve normalised PDF bytes from the Bronze store.

    Returns the bytes or raises HTTPException 404 if the key is absent.
    """
    bronze_store = getattr(request.app.state, "bronze_store", None)
    if bronze_store is None:
        logger.error("bronze_store not initialised on app.state")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="bronze_store_not_ready",
        )

    key = _BRONZE_PDF_KEY_TEMPLATE.format(pdf_id=pdf_id)
    pdf_bytes: bytes | None = await bronze_store.get(key)
    if pdf_bytes is None:
        logger.info("PDF not found in Bronze store: pdf_id=%s key=%s", pdf_id[:16], key)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="pdf_not_found",
        )
    return pdf_bytes


def _require_workspace_id(user: UserContext) -> uuid.UUID:
    """Resolve the request's workspace_id or fail with 401.

    §04p silver.pdf_* tables all require workspace_id. The JWT minted by
    Laravel's FastApiJwtMinter embeds workspace_id directly; absence means
    either an unscoped token or a stale minter — either way, the request
    cannot proceed and the user must re-authenticate against a workspace.
    """
    wid = user.workspace_id
    if not wid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="workspace_required",
        )
    try:
        return uuid.UUID(str(wid))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="workspace_required",
        ) from exc


def _get_render_service(request: Request):  # type: ignore[return]
    """Retrieve the PdfRenderService from app.state.

    Raises 503 if the service is not initialised (startup hook not called).
    """
    service = getattr(request.app.state, "pdf_render_service", None)
    if service is None:
        logger.error("pdf_render_service not initialised on app.state")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pdf_render_service_not_ready",
        )
    return service


# ---------------------------------------------------------------------------
# POST /pdf/render_page
# ---------------------------------------------------------------------------


@router.post("/render_page")
async def render_page(
    body: RenderPageRequest,
    request: Request,
    _user: UserContext = Depends(extract_user_context),
) -> StreamingResponse:
    """Render a full PDF page to PNG.

    Returns the PNG image bytes in the response body and a base64-encoded
    PdfProvenance JSON in the ``X-PDF-Provenance`` header.

    The caller must have already run preflight and stored the normalised PDF
    in the Bronze store at ``pdfs/{pdf_id}.pdf``.

    Responses
    ---------
    200  image/png  — PNG bytes + X-PDF-Provenance header
    404  application/json  — {"detail": "pdf_not_found"}
    401  application/json  — missing / invalid service key or JWT
    503  application/json  — Bronze store or render service not initialised
    """
    pdf_bytes = await _fetch_pdf_bytes(request, body.pdf_id)
    render_service = _get_render_service(request)

    try:
        png_bytes = await render_service.render_page(
            pdf_bytes=pdf_bytes,
            pdf_id=body.pdf_id,
            page=body.page,
            dpi=body.dpi,
        )
    except Exception as exc:
        logger.exception(
            "render_page failed: pdf_id=%s page=%d dpi=%d",
            body.pdf_id[:16], body.page, body.dpi,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="render_failed",
        ) from exc

    provenance = PdfProvenance(
        pdf_id=body.pdf_id,
        page=body.page,
        bbox=None,
        source_method="pypdfium2",
        extraction_confidence=1.0,
    )
    provenance_header = _encode_provenance(provenance)

    logger.debug(
        "render_page OK pdf_id=%s page=%d dpi=%d bytes=%d",
        body.pdf_id[:16], body.page, body.dpi, len(png_bytes),
    )

    return StreamingResponse(
        io.BytesIO(png_bytes),
        media_type="image/png",
        headers={"X-PDF-Provenance": provenance_header},
    )


# ---------------------------------------------------------------------------
# Helpers — Stage 3 (extract service)
# ---------------------------------------------------------------------------


def _get_extract_service(request: Request):  # type: ignore[return]
    """Retrieve the PdfExtractService from app.state.

    Raises 503 if the service is not initialised (pdfminer or pdfplumber
    import failed during startup).
    """
    service = getattr(request.app.state, "pdf_extract_service", None)
    if service is None:
        logger.error("pdf_extract_service not initialised on app.state")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pdf_extract_service_not_ready",
        )
    return service


def _build_text_block(block: dict, pdf_id: str) -> PdfTextBlock:
    """Convert a raw extract-worker dict to a PdfTextBlock Pydantic model."""
    bbox = (
        block["bbox_x0"],
        block["bbox_y0"],
        block["bbox_x1"],
        block["bbox_y1"],
    )
    return PdfTextBlock(
        block_id=block.get("block_id") or uuid.uuid4(),
        pdf_id=pdf_id,
        page=block["page"],
        bbox=bbox,
        text=block["text"],
        font_name=block.get("font_name"),
        font_size=block.get("font_size"),
        provenance=PdfProvenance(
            pdf_id=pdf_id,
            page=block["page"],
            bbox=list(bbox),
            source_method="pdfminer",
            extraction_confidence=1.0,
        ),
    )


def _build_table(table: dict, pdf_id: str) -> PdfTable:
    """Convert a raw extract-worker dict to a PdfTable Pydantic model."""
    rows = table["rows"]
    cell_bboxes_raw = table["cell_bboxes"]

    # Compute overall table bbox from all cell bboxes.
    all_x0 = [b[0] for row in cell_bboxes_raw for b in row if b[2] > b[0]]
    all_y0 = [b[1] for row in cell_bboxes_raw for b in row if b[3] > b[1]]
    all_x1 = [b[2] for row in cell_bboxes_raw for b in row if b[2] > b[0]]
    all_y1 = [b[3] for row in cell_bboxes_raw for b in row if b[3] > b[1]]
    if all_x0:
        table_bbox: list[float] = [min(all_x0), min(all_y0), max(all_x1), max(all_y1)]
    else:
        table_bbox = [0.0, 0.0, 0.0, 0.0]

    # Ensure cell_bboxes is typed correctly as list[list[tuple[float,...]]].
    typed_cell_bboxes: list[list[tuple[float, float, float, float]]] = [
        [tuple(b) for b in row]  # type: ignore[misc]
        for row in cell_bboxes_raw
    ]

    return PdfTable(
        table_index=table["table_index"],
        pdf_id=pdf_id,
        page=table["page"],
        rows=rows,
        cell_bboxes=typed_cell_bboxes,
        provenance=PdfProvenance(
            pdf_id=pdf_id,
            page=table["page"],
            bbox=table_bbox,
            source_method="pdfplumber",
            extraction_confidence=1.0,
        ),
    )


# ---------------------------------------------------------------------------
# GET /pdf/extract_text
# ---------------------------------------------------------------------------


@router.get("/extract_text")
async def extract_text(
    request: Request,
    pdf_id: str = Query(..., description="SHA-256 hex of the normalised PDF in the Bronze store"),
    page: int | None = Query(None, ge=1, description="1-indexed page to extract; omit for all pages"),
    user: UserContext = Depends(extract_user_context),
) -> ExtractTextResponse:
    """Extract text blocks with bboxes and font metadata from a PDF page.

    Uses pdfminer.six as the primary text source (lossless — OCR is fallback
    only per §04p Stage 3).  Results are cached in silver.pdf_text_blocks;
    subsequent calls for the same (pdf_id, page) are served from the cache
    without re-running the extraction worker.

    Every text block in the response carries a ``provenance`` field with
    (pdf_id, page, bbox) per the §04p provenance contract, feeding §04i
    Numeric grounding and Citation completeness guards.

    Responses
    ---------
    200  application/json  — ExtractTextResponse with blocks + cache_hit
    404  application/json  — {"detail": "pdf_not_found"}
    401  application/json  — missing / invalid service key or JWT
    503  application/json  — Bronze store or extract service not initialised
    """
    workspace_id = _require_workspace_id(user)
    pdf_bytes = await _fetch_pdf_bytes(request, pdf_id)
    extract_service = _get_extract_service(request)

    try:
        raw_blocks, cache_hit = await extract_service.extract_text(
            pdf_bytes=pdf_bytes,
            pdf_id=pdf_id,
            workspace_id=workspace_id,
            page=page,
        )
    except Exception as exc:
        logger.exception(
            "extract_text failed: pdf_id=%s page=%s",
            pdf_id[:16], page,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="extract_text_failed",
        ) from exc

    blocks = [_build_text_block(b, pdf_id) for b in raw_blocks]

    logger.debug(
        "extract_text OK pdf_id=%s page=%s blocks=%d cache_hit=%s",
        pdf_id[:16], page, len(blocks), cache_hit,
    )

    return ExtractTextResponse(
        blocks=blocks,
        cache_hit=cache_hit,
        pdf_id=pdf_id,
        page=page,
    )


# ---------------------------------------------------------------------------
# GET /pdf/find_tables
# ---------------------------------------------------------------------------


@router.get("/find_tables")
async def find_tables(
    request: Request,
    pdf_id: str = Query(..., description="SHA-256 hex of the normalised PDF in the Bronze store"),
    page: int | None = Query(None, ge=1, description="1-indexed page to scan; omit for all pages"),
    user: UserContext = Depends(extract_user_context),
) -> FindTablesResponse:
    """Extract table matrices with cell-level bboxes from a PDF page.

    Uses pdfplumber find_tables() (built on pdfminer.six).  Results are cached
    in silver.pdf_table_cells; subsequent calls for the same (pdf_id, page) are
    served from the cache without re-running the extraction worker.

    The response is raw table structure only — Docling structure-refinement
    (Phase 1.C) is not applied in Phase 1.B.

    Every table in the response carries a ``provenance`` field with
    (pdf_id, page, table_bbox) per the §04p provenance contract.

    Responses
    ---------
    200  application/json  — FindTablesResponse with tables + cache_hit
    404  application/json  — {"detail": "pdf_not_found"}
    401  application/json  — missing / invalid service key or JWT
    503  application/json  — Bronze store or extract service not initialised
    """
    workspace_id = _require_workspace_id(user)
    pdf_bytes = await _fetch_pdf_bytes(request, pdf_id)
    extract_service = _get_extract_service(request)

    try:
        raw_tables, cache_hit = await extract_service.extract_tables(
            pdf_bytes=pdf_bytes,
            pdf_id=pdf_id,
            workspace_id=workspace_id,
            page=page,
        )
    except Exception as exc:
        logger.exception(
            "find_tables failed: pdf_id=%s page=%s",
            pdf_id[:16], page,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="find_tables_failed",
        ) from exc

    tables = [_build_table(t, pdf_id) for t in raw_tables]

    logger.debug(
        "find_tables OK pdf_id=%s page=%s tables=%d cache_hit=%s",
        pdf_id[:16], page, len(tables), cache_hit,
    )

    return FindTablesResponse(
        tables=tables,
        cache_hit=cache_hit,
        pdf_id=pdf_id,
        page=page,
    )


# ---------------------------------------------------------------------------
# Helpers — Stage 4 (layout service)
# ---------------------------------------------------------------------------


def _get_layout_service(request: Request):  # type: ignore[return]
    """Retrieve the PdfLayoutService from app.state.

    Raises 503 if the service is not initialised (docling import failed during
    startup or the lifespan hook was not reached).
    """
    service = getattr(request.app.state, "pdf_layout_service", None)
    if service is None:
        logger.error("pdf_layout_service not initialised on app.state")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pdf_layout_service_not_ready",
        )
    return service


def _build_layout_region(region: dict, pdf_id: str) -> PdfLayoutRegion:
    """Convert a raw layout-worker dict to a PdfLayoutRegion Pydantic model.

    Mirrors _build_text_block — the region dict comes either from the
    _detect_layout_worker (fresh detection) or from a Silver cache read.
    Both paths use the same key names.
    """
    bbox = (
        region["bbox_x0"],
        region["bbox_y0"],
        region["bbox_x1"],
        region["bbox_y1"],
    )
    # region_id may be present from a cache read (UUID from DB) or absent on
    # a fresh worker result (generated here for the response model).
    region_id_raw = region.get("region_id")
    region_id = uuid.UUID(str(region_id_raw)) if region_id_raw else uuid.uuid4()

    return PdfLayoutRegion(
        region_id=region_id,
        pdf_id=pdf_id,
        page=region["page"],
        region_index=region["region_index"],
        region_type=region["region_type"],
        bbox=bbox,
        region_confidence=region.get("region_confidence"),
        provenance=PdfProvenance(
            pdf_id=pdf_id,
            page=region["page"],
            bbox=list(bbox),
            source_method="docling",
            extraction_confidence=region.get("region_confidence") or 1.0,
        ),
    )


# ---------------------------------------------------------------------------
# GET /pdf/find_legends
# ---------------------------------------------------------------------------


@router.get("/find_legends")
async def find_legends(
    request: Request,
    pdf_id: str = Query(..., description="SHA-256 hex of the normalised PDF in the Bronze store"),
    page: int | None = Query(None, ge=1, description="1-indexed page to scan; omit for all pages"),
    region_type: LayoutRegionType | None = Query(
        None,
        description=(
            "Optional region type filter.  When set, only regions of this type are returned. "
            "Applied server-side over cached Silver rows — no additional Docling inference."
        ),
    ),
    user: UserContext = Depends(extract_user_context),
) -> FindLegendsResponse:
    """Detect typed layout regions in a PDF page using Docling.

    Returns ALL layout region types (text/figure/table/header/footer/formula/
    title/caption/footnote/list/page_number/unknown) unless the optional
    ``region_type`` query parameter is supplied, in which case only regions
    of that type are returned.

    Despite the endpoint name, no mining-domain legend heuristics are applied
    in Phase 1.C-i.  The agent or Phase 2 deterministic extractors filter for
    legend-shaped regions from the typed output.

    Results are cached in silver.pdf_layout_regions; subsequent calls for the
    same (pdf_id, page) combination are served from the cache without
    re-running Docling inference.

    Every region in the response carries a ``provenance`` field with
    (pdf_id, page, bbox) per the §04p provenance contract, feeding §04i
    Citation completeness and Numeric grounding guards.

    Responses
    ---------
    200  application/json  — FindLegendsResponse with regions + cache_hit
    404  application/json  — {"detail": "pdf_not_found"}
    401  application/json  — missing / invalid service key or JWT
    503  application/json  — Bronze store or layout service not initialised
    """
    workspace_id = _require_workspace_id(user)
    pdf_bytes = await _fetch_pdf_bytes(request, pdf_id)
    layout_service = _get_layout_service(request)

    try:
        raw_regions, cache_hit = await layout_service.detect_layout(
            pdf_bytes=pdf_bytes,
            pdf_id=pdf_id,
            workspace_id=workspace_id,
            page=page,
        )
    except RuntimeError as exc:
        # RuntimeError is raised when docling is not installed — surface as 503.
        logger.error("detect_layout failed (docling unavailable?): %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pdf_layout_service_not_ready",
        ) from exc
    except Exception as exc:
        logger.exception(
            "find_legends failed: pdf_id=%s page=%s",
            pdf_id[:16], page,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="find_legends_failed",
        ) from exc

    # Build Pydantic models from raw dicts.
    regions = [_build_layout_region(r, pdf_id) for r in raw_regions]

    # Apply optional server-side type filter.
    if region_type is not None:
        regions = [r for r in regions if r.region_type == region_type]

    logger.debug(
        "find_legends OK pdf_id=%s page=%s regions=%d region_type=%s cache_hit=%s",
        pdf_id[:16], page, len(regions), region_type, cache_hit,
    )

    return FindLegendsResponse(
        regions=regions,
        cache_hit=cache_hit,
        pdf_id=pdf_id,
        page=page,
    )


# ---------------------------------------------------------------------------
# POST /pdf/crop_region
# ---------------------------------------------------------------------------


@router.post("/crop_region")
async def crop_region(
    body: CropRegionRequest,
    request: Request,
    _user: UserContext = Depends(extract_user_context),
) -> StreamingResponse:
    """Render and crop a PDF region to PNG.

    ``bbox`` must be given in PDF user-space coordinates
    (origin = bottom-left, y increases upward, units = points).

    Returns the cropped PNG image bytes in the response body and a
    base64-encoded PdfProvenance JSON in the ``X-PDF-Provenance`` header.

    Responses
    ---------
    200  image/png  — PNG bytes + X-PDF-Provenance header
    404  application/json  — {"detail": "pdf_not_found"}
    401  application/json  — missing / invalid service key or JWT
    422  application/json  — bbox must have exactly 4 elements
    503  application/json  — Bronze store or render service not initialised
    """
    pdf_bytes = await _fetch_pdf_bytes(request, body.pdf_id)
    render_service = _get_render_service(request)

    bbox_tuple = (body.bbox[0], body.bbox[1], body.bbox[2], body.bbox[3])

    try:
        png_bytes = await render_service.crop_region(
            pdf_bytes=pdf_bytes,
            pdf_id=body.pdf_id,
            page=body.page,
            bbox=bbox_tuple,
            dpi=body.dpi,
        )
    except Exception as exc:
        logger.exception(
            "crop_region failed: pdf_id=%s page=%d bbox=%s dpi=%d",
            body.pdf_id[:16], body.page, bbox_tuple, body.dpi,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="render_failed",
        ) from exc

    provenance = PdfProvenance(
        pdf_id=body.pdf_id,
        page=body.page,
        bbox=list(bbox_tuple),
        source_method="pypdfium2",
        extraction_confidence=1.0,
    )
    provenance_header = _encode_provenance(provenance)

    logger.debug(
        "crop_region OK pdf_id=%s page=%d bbox=%s dpi=%d bytes=%d",
        body.pdf_id[:16], body.page, bbox_tuple, body.dpi, len(png_bytes),
    )

    return StreamingResponse(
        io.BytesIO(png_bytes),
        media_type="image/png",
        headers={"X-PDF-Provenance": provenance_header},
    )


# ---------------------------------------------------------------------------
# Helpers — Stage 5 (OCR service)
# ---------------------------------------------------------------------------


def _get_ocr_service(request: Request):  # type: ignore[return]
    """Retrieve the PdfOcrService from app.state.

    Raises 503 if the service is not initialised (paddleocr import failed
    during startup or the lifespan hook was not reached).
    """
    service = getattr(request.app.state, "pdf_ocr_service", None)
    if service is None:
        logger.error("pdf_ocr_service not initialised on app.state")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pdf_ocr_service_not_ready",
        )
    return service


def _validate_ocr_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise 422 if the bbox is zero-area or inverted.

    §04p "region-targeted only, never full-page by default" — the endpoint
    refuses any bbox that does not describe a positive non-degenerate rectangle.

    Rejected cases:
      - x0 >= x1 (zero-width or inverted in x)
      - y0 >= y1 (zero-height or inverted in y)

    The caller is responsible for supplying a meaningful region bbox (e.g.,
    from find_legends output).  The service never falls back to full-page OCR.
    """
    x0, y0, x1, y1 = bbox
    if x0 >= x1 or y0 >= y1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="ocr_full_page_refused",
        )


def _build_ocr_response(result: dict, request_body: OcrRegionRequest) -> OcrRegionResponse:
    """Convert a raw OCR service result dict to an OcrRegionResponse Pydantic model.

    Parameters
    ----------
    result:
        Dict returned by PdfOcrService.ocr_region().
        Keys: ocr_id, text_content, lines, mean_confidence, source_method.
    request_body:
        The validated OcrRegionRequest so we can echo pdf_id, page, bbox, dpi.

    Note: cache_hit is NOT set here — the caller uses model_copy to inject it
    after receiving the (result, cache_hit) tuple from the service.
    """
    ocr_lines = [
        OcrLine(
            text=ln["text"],
            bbox=(ln["bbox"][0], ln["bbox"][1], ln["bbox"][2], ln["bbox"][3]),
            confidence=float(ln["confidence"]),
        )
        for ln in result.get("lines", [])
    ]

    mean_confidence = float(result.get("mean_confidence", 1.0))

    return OcrRegionResponse(
        ocr_id=uuid.UUID(result["ocr_id"]),
        pdf_id=request_body.pdf_id,
        page=request_body.page,
        region_bbox=request_body.bbox,
        dpi=request_body.dpi,
        text_content=result.get("text_content", ""),
        lines=ocr_lines,
        mean_confidence=mean_confidence,
        cache_hit=False,  # overwritten by model_copy in the endpoint handler
        provenance=PdfProvenance(
            pdf_id=request_body.pdf_id,
            page=request_body.page,
            bbox=list(request_body.bbox),
            source_method="paddle_ocr",
            # extraction_confidence reflects OCR quality — NOT hardcoded 1.0.
            # Per §04p and §04i Layer 3 (Numeric grounding): OCR is lossy;
            # numerical claims from OCR-derived text carry lower certainty.
            extraction_confidence=mean_confidence,
        ),
    )


# ---------------------------------------------------------------------------
# POST /pdf/ocr_region
# ---------------------------------------------------------------------------


@router.post("/ocr_region")
async def ocr_region(
    body: OcrRegionRequest,
    request: Request,
    user: UserContext = Depends(extract_user_context),
) -> OcrRegionResponse:
    """Run PaddleOCR PP-OCRv5 on a specific region of a PDF page.

    Region-targeted only (§04p Stage 5 — "never full-page by default").
    The agent passes a bbox derived from ``find_legends`` output (typically a
    figure or table region whose text was not captured by the pdfminer/pdfplumber
    Stage 3 path).

    The OCR pipeline:
      1. Renders the cropped region via PdfRenderService.crop_region() at the
         requested DPI (default 300).
      2. Passes the PNG bytes to the PaddleOCR PP-OCRv5 worker in a separate
         process (ProcessPoolExecutor — PaddleOCR is synchronous and CPU-bound).
      3. Caches the result in silver.pdf_ocr_results keyed on
         (pdf_id, page, bbox, dpi).  Subsequent calls for the same region+DPI
         are served from the cache without re-running OCR.

    ``bbox`` must be a positive non-degenerate rectangle in PDF user-space
    coordinates (bottom-left origin, y increases upward, units = points).
    Zero-area and inverted bboxes are rejected with 422 before any work begins.

    ``provenance.extraction_confidence`` reflects OCR quality (mean per-line
    confidence) — NOT a hardcoded 1.0.  OCR is lossy; use this field to gate
    downstream numerical claim verification (§04i Layer 3).

    Responses
    ---------
    200  application/json  — OcrRegionResponse with lines + provenance + cache_hit
    404  application/json  — {"detail": "pdf_not_found"}
    401  application/json  — missing / invalid service key or JWT
    422  application/json  — {"detail": "ocr_full_page_refused"} for zero-area or
                             inverted bbox (x0>=x1 or y0>=y1)
    503  application/json  — Bronze store, render service, or OCR service not initialised
    """
    # Reject zero-area / inverted bboxes before any I/O.
    _validate_ocr_bbox(body.bbox)

    workspace_id = _require_workspace_id(user)
    pdf_bytes = await _fetch_pdf_bytes(request, body.pdf_id)
    ocr_service = _get_ocr_service(request)

    try:
        result, cache_hit = await ocr_service.ocr_region(
            pdf_bytes=pdf_bytes,
            pdf_id=body.pdf_id,
            page=body.page,
            bbox=body.bbox,
            workspace_id=workspace_id,
            dpi=body.dpi,
        )
    except RuntimeError as exc:
        # RuntimeError is raised when paddleocr is not installed — surface as 503.
        logger.error("ocr_region failed (paddleocr unavailable?): %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pdf_ocr_service_not_ready",
        ) from exc
    except Exception as exc:
        logger.exception(
            "ocr_region failed: pdf_id=%s page=%d bbox=%s dpi=%d",
            body.pdf_id[:16], body.page, body.bbox, body.dpi,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ocr_region_failed",
        ) from exc

    response = _build_ocr_response(result, body)
    # Inject the actual cache_hit flag (model_copy avoids a second DB round-trip).
    response = response.model_copy(update={"cache_hit": cache_hit})

    logger.debug(
        "ocr_region OK pdf_id=%s page=%d dpi=%d lines=%d mean_conf=%.3f cache_hit=%s",
        body.pdf_id[:16], body.page, body.dpi,
        len(response.lines), response.mean_confidence, cache_hit,
    )

    return response


# ---------------------------------------------------------------------------
# Helpers — Stage 6 (VL service)
# ---------------------------------------------------------------------------


def _get_vl_service(request: Request):  # type: ignore[return]
    """Retrieve the PdfVlService from app.state.

    Raises 503 if the service is not initialised (VL config error at startup
    or the lifespan hook was not reached).
    """
    service = getattr(request.app.state, "pdf_vl_service", None)
    if service is None:
        logger.error("pdf_vl_service not initialised on app.state")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pdf_vl_service_not_ready",
        )
    return service


def _build_vl_provenance(claims: list[VlClaim], pdf_id: str) -> list[PdfProvenance]:
    """Build one PdfProvenance per VlClaim.

    source_method='qwen_vl' per §04p provenance contract.
    extraction_confidence reflects the LLM's per-claim confidence — NOT
    hardcoded 1.0 (VL is lossy; per §04i Layer 3 Numeric grounding, callers
    must weight numerical claims by this field).
    """
    return [
        PdfProvenance(
            pdf_id=pdf_id,
            page=claim.page,
            bbox=list(claim.bbox),
            source_method="qwen_vl",
            extraction_confidence=claim.confidence,
        )
        for claim in claims
    ]


# ---------------------------------------------------------------------------
# GET /pdf/summarize_section
# ---------------------------------------------------------------------------


@router.get("/summarize_section", response_model=SummarizeSectionResponse)
async def summarize_section(
    request: Request,
    pdf_id: str = Query(..., description="SHA-256 hex of the normalised PDF in the Bronze store"),
    section_kind: str = Query(
        ...,
        description=(
            "Section reference kind: 'page' | 'page_range' | 'layout_region'. "
            "Determines which additional parameters are required."
        ),
    ),
    page: int | None = Query(
        None,
        ge=1,
        description="Required when section_kind='page'. 1-indexed page number.",
    ),
    page_start: int | None = Query(
        None,
        ge=1,
        description="Required when section_kind='page_range'. First page (inclusive, 1-indexed).",
    ),
    page_end: int | None = Query(
        None,
        ge=1,
        description="Required when section_kind='page_range'. Last page (inclusive, 1-indexed).",
    ),
    region_id: uuid.UUID | None = Query(
        None,
        description="Required when section_kind='layout_region'. UUID from silver.pdf_layout_regions.",
    ),
    user: UserContext = Depends(extract_user_context),
) -> SummarizeSectionResponse:
    """Summarise a PDF section using Qwen-VL vision-language reasoning.

    Stage 6 of the §04p PDF Ingestion Subsystem.  Renders the requested
    section pages at 200 DPI, sends them to the configured VL backend
    (Ollama in dev, vLLM in prod), and returns a structured JSON summary
    with per-claim provenance.

    The §04i Citation completeness guard is enforced at the service layer:
    every claim in the response carries a (pdf_id, page, bbox) grounding
    tuple.  Non-resolvable claims are rejected by VlSummaryShape Pydantic
    validation before the response is returned.

    Cache semantics: results are cached in silver.pdf_vl_summaries keyed on
    (pdf_id, section_ref_hash, model_id).  Different model versions are
    cached independently.

    Responses
    ---------
    200  application/json  — SummarizeSectionResponse with summary + claims + provenance
    404  application/json  — {"detail": "pdf_not_found"}
    401  application/json  — missing / invalid service key or JWT
    422  application/json  — {"detail": "section_ref_invalid"} for parameter mismatch
    422  application/json  — {"detail": "section_too_large", "max_pages": N}
    502  application/json  — {"detail": "vl_backend_error"} — LLM unreachable / non-200
    502  application/json  — {"detail": "vl_output_shape_error"} — LLM output failed validation
    503  application/json  — VL service not initialised (model not pulled or config error)
    """
    # Validate section_kind and required companion parameters.
    _VALID_KINDS = {"page", "page_range", "layout_region"}
    if section_kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="section_ref_invalid",
        )

    if section_kind == "page":
        if page is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="section_ref_invalid",
            )
        section_ref = {"kind": "page", "page": page}

    elif section_kind == "page_range":
        if page_start is None or page_end is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="section_ref_invalid",
            )
        if page_end < page_start:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="section_ref_invalid",
            )
        section_ref = {"kind": "page_range", "page_start": page_start, "page_end": page_end}

    else:  # layout_region
        if region_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="section_ref_invalid",
            )
        section_ref = {"kind": "layout_region", "region_id": str(region_id)}

    workspace_id = _require_workspace_id(user)

    # Fetch the PDF from the Bronze store.
    pdf_bytes = await _fetch_pdf_bytes(request, pdf_id)

    # Retrieve the VL service.
    vl_service = _get_vl_service(request)

    try:
        result, cache_hit = await vl_service.summarize_section(
            pdf_bytes=pdf_bytes,
            pdf_id=pdf_id,
            section_ref=section_ref,
            workspace_id=workspace_id,
        )
    except VlSectionTooLargeError as exc:
        logger.info(
            "summarize_section section_too_large pdf_id=%s pages=%d max=%d",
            pdf_id[:16], exc.page_count, exc.max_pages,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"detail": "section_too_large", "max_pages": exc.max_pages},
        )
    except VlBackendError as exc:
        logger.warning(
            "summarize_section vl_backend_error pdf_id=%s status=%s detail=%r",
            pdf_id[:16], exc.status_code, exc.detail,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="vl_backend_error",
        )
    except VlOutputShapeError as exc:
        logger.warning(
            "summarize_section vl_output_shape_error pdf_id=%s reason=%r",
            pdf_id[:16], exc.reason,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="vl_output_shape_error",
        )
    except Exception as exc:
        logger.exception(
            "summarize_section unexpected error pdf_id=%s section_ref=%r",
            pdf_id[:16], section_ref,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="summarize_section_failed",
        ) from exc

    # Build VlClaim objects from the raw claims dicts.
    claims = [VlClaim.model_validate(c) for c in result["claims"]]

    # Build per-claim PdfProvenance list per §04p contract.
    provenance = _build_vl_provenance(claims, pdf_id)

    logger.debug(
        "summarize_section OK pdf_id=%s section_kind=%s claims=%d cache_hit=%s",
        pdf_id[:16], section_kind, len(claims), cache_hit,
    )

    return SummarizeSectionResponse(
        summary_id=result["summary_id"],
        pdf_id=pdf_id,
        section_ref=result["section_ref"],
        summary_text=result["summary_text"],
        claims=claims,
        model_id=result["model_id"],
        model_backend=result["model_backend"],
        mean_claim_confidence=result["mean_claim_confidence"],
        prompt_tokens=result["prompt_tokens"],
        completion_tokens=result["completion_tokens"],
        cache_hit=cache_hit,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# Helpers — Phase 2.A (coordinates service)
# ---------------------------------------------------------------------------


def _get_coordinates_service(request: Request):  # type: ignore[return]
    """Retrieve the PdfCoordinatesService from app.state.

    Raises 503 if the service is not initialised (startup hook not called or
    DB pool not ready at startup time).
    """
    service = getattr(request.app.state, "pdf_coordinates_service", None)
    if service is None:
        logger.error("pdf_coordinates_service not initialised on app.state")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pdf_coordinates_service_not_ready",
        )
    return service


def _build_coordinate(coord: dict, pdf_id: str) -> PdfCoordinate:
    """Convert a raw coord dict (from service or DB) to a PdfCoordinate Pydantic model.

    The coord dict may come from:
      - The service's in-memory extraction result (keys: coord_kind, raw_match, etc.)
      - A Silver cache read (same keys, but coord_id as UUID from DB)

    Both paths are normalised here.  match_bbox is reconstructed from the four
    separate column values (match_bbox_x0/y0/x1/y1).
    """
    # Build match_bbox tuple from the four separate bbox columns (or None when any is NULL).
    x0 = coord.get("match_bbox_x0")
    y0 = coord.get("match_bbox_y0")
    x1 = coord.get("match_bbox_x1")
    y1 = coord.get("match_bbox_y1")
    match_bbox: tuple[float, float, float, float] | None
    if x0 is not None and y0 is not None and x1 is not None and y1 is not None:
        match_bbox = (float(x0), float(y0), float(x1), float(y1))
    elif coord.get("match_bbox") is not None:
        # From in-memory extraction (tuple already assembled).
        match_bbox = coord["match_bbox"]
    else:
        match_bbox = None

    # coord_id: present in cache reads (UUID column), generated for fresh extractions.
    raw_coord_id = coord.get("coord_id")
    coord_id = uuid.UUID(str(raw_coord_id)) if raw_coord_id else uuid.uuid4()

    page = int(coord["page"])

    # Build PdfProvenance — source_method='regex' per the v1.46 §04p enum
    # extension, extraction_confidence=1.0 because regex matches that pass
    # bounds-check are lossless. The chain is text-via-pdfminer-then-regex;
    # 'regex' covers the deterministic-extraction step layered on top of
    # the upstream pdfminer text source recorded in silver.pdf_text_blocks.
    provenance = PdfProvenance(
        pdf_id=pdf_id,
        page=page,
        bbox=list(match_bbox) if match_bbox else None,
        source_method="regex",
        extraction_confidence=float(coord.get("extraction_confidence", 1.0)),
    )

    # source_block_id may be a UUID, str, or None.
    raw_block_id = coord.get("source_block_id")
    source_block_id: uuid.UUID | None = None
    if raw_block_id is not None:
        try:
            source_block_id = uuid.UUID(str(raw_block_id))
        except (ValueError, AttributeError):
            source_block_id = None

    return PdfCoordinate(
        coord_id=coord_id,
        pdf_id=pdf_id,
        page=page,
        source_block_id=source_block_id,
        coord_kind=coord["coord_kind"],
        raw_match=coord["raw_match"],
        match_bbox=match_bbox,
        latitude=coord.get("latitude"),
        longitude=coord.get("longitude"),
        utm_zone=coord.get("utm_zone"),
        utm_hemisphere=coord.get("utm_hemisphere"),
        utm_easting=coord.get("utm_easting"),
        utm_northing=coord.get("utm_northing"),
        datum=coord.get("datum"),
        extraction_confidence=float(coord.get("extraction_confidence", 1.0)),
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# GET /pdf/find_coordinates
# ---------------------------------------------------------------------------


@router.get("/find_coordinates")
async def find_coordinates(
    request: Request,
    pdf_id: str = Query(
        ...,
        description="SHA-256 hex of the normalised PDF in the Bronze store",
    ),
    page: int | None = Query(
        None,
        ge=1,
        description="1-indexed page to scan; omit for all pages",
    ),
    coord_kind: CoordKind | None = Query(
        None,
        description=(
            "Optional coordinate type filter: 'utm' | 'latlon_decimal' | 'latlon_dms'.  "
            "When set, only coordinates of this type are returned (server-side filter over "
            "cached Silver rows — no additional regex pass)."
        ),
    ),
    user: UserContext = Depends(extract_user_context),
) -> FindCoordinatesResponse:
    """Extract geographic coordinates from a PDF using deterministic regex.

    §04p Phase 2.A — determinism-over-LLM enforcement for coordinates.

    Reads text from silver.pdf_text_blocks (populated by GET /pdf/extract_text).
    Runs four regex patterns (UTM full, UTM terse + proximity guard, lat/lon
    decimal, lat/lon DMS) over each block.  Every match is validated via Pydantic
    bounds-check before being returned or cached.

    Datum hints (NAD27/NAD83/WGS84) found within 200 characters of a coordinate
    match in the same text block are attached as the ``datum`` field.

    Results are cached in silver.pdf_coordinates.  Subsequent calls for the same
    (pdf_id, page) are served from the cache without re-running the regex.

    The endpoint returns 200 + empty ``coordinates`` list when:
      - No text blocks have been extracted yet for this pdf_id/page.
        (Call GET /pdf/extract_text first to populate the Phase 1.B cache.)
      - No coordinate patterns were found in the extracted text.

    This is NOT a 404 condition — 404 is reserved for "pdf_id not in Bronze
    store".  An empty coordinates list is the legitimate outcome for a PDF page
    that contains no coordinate strings.

    Responses
    ---------
    200  application/json  — FindCoordinatesResponse (may have empty coordinates list)
    404  application/json  — {"detail": "pdf_not_found"}
    401  application/json  — missing / invalid service key or JWT
    503  application/json  — Bronze store or coordinates service not initialised
    """
    workspace_id = _require_workspace_id(user)

    # Bronze store lookup confirms the pdf_id is valid.
    # We do NOT pass pdf_bytes to the coordinates service — it reads text from
    # silver.pdf_text_blocks directly (not from the raw PDF).
    await _fetch_pdf_bytes(request, pdf_id)

    coords_service = _get_coordinates_service(request)

    try:
        raw_coords, cache_hit = await coords_service.find_coordinates(
            pdf_id=pdf_id,
            workspace_id=workspace_id,
            page=page,
        )
    except Exception as exc:
        logger.exception(
            "find_coordinates failed: pdf_id=%s page=%s",
            pdf_id[:16], page,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="find_coordinates_failed",
        ) from exc

    # Build Pydantic models from raw dicts.
    coordinates = [_build_coordinate(c, pdf_id) for c in raw_coords]

    # Apply optional server-side type filter.
    if coord_kind is not None:
        coordinates = [c for c in coordinates if c.coord_kind == coord_kind]

    logger.debug(
        "find_coordinates OK pdf_id=%s page=%s coords=%d coord_kind=%s cache_hit=%s",
        pdf_id[:16], page, len(coordinates), coord_kind, cache_hit,
    )

    return FindCoordinatesResponse(
        coordinates=coordinates,
        cache_hit=cache_hit,
        pdf_id=pdf_id,
        page=page,
    )
