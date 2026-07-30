"""Pydantic models for the §04p PDF Ingestion Subsystem — Phase 1.A + 1.B + 1.D + 2.A.

This module owns the data contracts shared across:
  - app/services/pdf_preflight.py    (Stage 1 — qpdf via pikepdf)
  - app/services/pdf_render.py       (Stage 2 — pypdfium2)
  - app/services/pdf_extract.py      (Stage 3 — pdfminer.six + pdfplumber)
  - app/services/pdf_vl.py           (Stage 6 — Qwen-VL vision-language reasoning)
  - app/services/pdf_coordinates.py  (Phase 2.A — deterministic coordinate extraction)
  - app/routers/pdf.py               (HTTP endpoints)

Phase 1.B additions (text+layout):
  - PdfTextBlock         — one text block (LTTextBox) with bbox + font metadata
  - PdfTable             — one table with rows × cols matrix + cell bboxes
  - ExtractTextResponse  — list[PdfTextBlock] + cache_hit flag
  - FindTablesResponse   — list[PdfTable] + cache_hit flag

NOTE: Stage 4 (Docling layout detection — LayoutRegionType, PdfLayoutRegion,
FindLegendsResponse, GET /pdf/find_legends) was removed 2026-07-29. Docling
never ran in production (PDF_PARSER_DOCLING_ENABLED was false in every live
deployment); see app/services/ingest/pdf_report.py's module docstring for
the full removal rationale. Stage 5 (PaddleOCR — pdf_ocr.py, OcrLine,
OcrRegionRequest/Response, POST /pdf/ocr_region) was removed the same day —
same orphan pattern, zero callers anywhere in Laravel/frontend.

Phase 1.D additions (Qwen-VL vision-language reasoning):
  - VlBackend            — Literal type alias for allowed VL backend names
  - VlClaim              — one VL claim with page + bbox + confidence (re-exported from pdf_vl)
  - VlSummaryShape       — LLM output validation model (re-exported from pdf_vl)
  - SummarizeSectionResponse — GET /pdf/summarize_section response envelope

Phase 2.A additions (deterministic coordinate extraction):
  - CoordKind            — Literal type alias matching silver.pdf_coordinates CHECK enum
  - PdfCoordinate        — one coordinate match with parsed fields + provenance
  - FindCoordinatesResponse — GET /pdf/find_coordinates response envelope
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Provenance contract (§04p — every Silver-layer artifact carries this tuple)
# ---------------------------------------------------------------------------


class PdfProvenance(BaseModel):
    """Provenance tuple attached to every PDF-derived artifact.

    Directly supports §04i Citation completeness and Numeric grounding guards:
    any LLM claim must resolve back to one or more (pdf_id, page, bbox) tuples.

    source_method is limited to the methods implemented in Phase 1.A.
    Phase 1.B (text+layout) will extend the enum with pdfminer / pdfplumber /
    paddle_ocr / paddle_structure values.
    """

    pdf_id: str = Field(..., description="SHA-256 hex of the original PDF bytes (Bronze archive key)")
    page: int = Field(..., ge=1, description="1-indexed page number in the normalised PDF")
    bbox: list[float] | None = Field(
        None,
        description="[x0, y0, x1, y1] in PDF user-space coordinates; None for full-page renders",
    )
    source_method: Literal[
        "pypdfium2",
        "pdfminer",
        "pdfplumber",
        "paddle_ocr",
        "paddle_structure",
        "qwen_vl",
        "regex",
    ] = Field(..., description="Extraction method — determines confidence semantics. 'regex' covers deterministic pattern extraction (Phase 2.A find_coordinates) layered on top of an upstream text source — extraction_confidence reflects the regex+bounds-check certainty (1.0 when bounds pass).")
    extraction_confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Model confidence [0.0, 1.0]; 1.0 for lossless (pypdfium2) renders",
    )


# ---------------------------------------------------------------------------
# Stage 1 — Preflight report
# ---------------------------------------------------------------------------


class PreflightReport(BaseModel):
    """Output of Stage 1 (qpdf via pikepdf) preflight.

    Stored alongside the normalised PDF in the Bronze tier (SeaweedFS).
    The split_into_chunks field lists page-range tuples when the source PDF
    exceeded 500 pages.  Phase 1.A only reports these ranges; chunked storage
    into separate Bronze objects is a follow-up task (Phase 1.B or later).
    """

    pdf_id: str = Field(..., description="SHA-256 hex of the ORIGINAL (pre-normalisation) bytes")
    original_bytes_hash: str = Field(..., description="Alias for pdf_id; separate field for clarity in reports")
    page_count: int = Field(..., ge=1)
    was_repaired: bool = Field(..., description="True if pikepdf detected and repaired structural damage")
    was_linearized: bool = Field(..., description="True — linearization is always applied by preflight")
    was_encrypted: bool = Field(
        ...,
        description="Always False on success (encrypted PDFs raise PdfEncryptedError before repair)",
    )
    split_into_chunks: list[tuple[int, int]] = Field(
        default_factory=list,
        description=(
            "Non-empty when page_count > 500.  Each tuple is (first_page, last_page), 1-indexed. "
            "Example: [(1, 500), (501, 600)] for a 600-page PDF."
        ),
    )
    qpdf_version: str = Field(..., description="pikepdf version string (wraps libqpdf internally)")
    preflight_timestamp: datetime = Field(..., description="UTC timestamp of the preflight run")


# ---------------------------------------------------------------------------
# Stage 2 — Render request / response models
# ---------------------------------------------------------------------------


class RenderPageRequest(BaseModel):
    """Body for POST /pdf/render_page.

    The caller must have already run preflight so the Bronze store contains
    the normalised PDF at pdfs/{pdf_id}.pdf.  The endpoint returns 404 if
    the key is absent.
    """

    pdf_id: str = Field(..., description="SHA-256 hex identifying the normalised PDF in the Bronze store")
    page: int = Field(..., ge=1, description="1-indexed page number to render")
    dpi: int = Field(
        200,
        ge=72,
        le=300,
        description="Render resolution.  200–300 for VL input; 72–150 for thumbnails (§04p Stage 2).",
    )


class RenderPageResponse(BaseModel):
    """Metadata returned alongside the PNG bytes in the X-PDF-Provenance header.

    The actual image bytes are in the response body (StreamingResponse).
    This model is serialised to JSON, base64-encoded, and placed in the
    X-PDF-Provenance header per the §04p provenance contract.
    """

    provenance: PdfProvenance


class CropRegionRequest(BaseModel):
    """Body for POST /pdf/crop_region.

    bbox is in PDF user-space coordinates (origin = bottom-left, y increases
    upward).  The render service converts to pixel coordinates using the
    DPI scaling factor (pixels_per_point = dpi / 72.0).
    """

    pdf_id: str = Field(..., description="SHA-256 hex identifying the normalised PDF in the Bronze store")
    page: int = Field(..., ge=1, description="1-indexed page number")
    bbox: list[float] = Field(
        ...,
        min_length=4,
        max_length=4,
        description="[x0, y0, x1, y1] in PDF user-space points",
    )
    dpi: int = Field(
        200,
        ge=72,
        le=300,
        description="Render resolution (same as render_page — consistent scale matters for VL input)",
    )


# ---------------------------------------------------------------------------
# Stage 3 — Text + layout extraction models (Phase 1.B)
# ---------------------------------------------------------------------------


class PdfTextBlock(BaseModel):
    """A single text block extracted from a PDF page via pdfminer.six.

    Corresponds to one LTTextBox in the pdfminer layout tree.  Every block
    carries the full §04p provenance tuple (pdf_id, page, bbox) so the
    FastAPI citation pipeline can satisfy §04i Citation completeness and
    Numeric grounding guards.

    bbox is [x0, y0, x1, y1] in PDF user-space coordinates
    (origin = bottom-left, y increases upward, units = points).
    """

    block_id: uuid.UUID = Field(..., description="Stable identifier for this text block in silver.pdf_text_blocks")
    pdf_id: str = Field(..., description="SHA-256 hex of the Bronze-stored normalised PDF")
    page: int = Field(..., ge=1, description="1-indexed page number")
    bbox: tuple[float, float, float, float] = Field(
        ...,
        description="[x0, y0, x1, y1] in PDF user-space points (bottom-left origin, y-up)",
    )
    text: str = Field(..., description="Full text content of the block (stripped)")
    font_name: str | None = Field(
        None,
        description="Font name from the first character in the block; None if unavailable",
    )
    font_size: float | None = Field(
        None,
        description="Font size in points from the first character; None if unavailable",
    )
    provenance: PdfProvenance = Field(
        ...,
        description="Full provenance tuple per §04p contract",
    )


class PdfTable(BaseModel):
    """A single table extracted from a PDF page via pdfplumber.

    The rows matrix mirrors what pdfplumber.table.extract() returns:
    a list of lists, where each inner list is a row of cell text strings.
    None means an empty cell (pdfplumber returns None for empty cells).

    cell_bboxes is parallel to rows — cell_bboxes[row][col] is the
    (x0, y0, x1, y1) bounding box of the corresponding cell in PDF
    user-space coordinates (converted from pdfplumber's y-down origin).

    provenance uses the table's overall bounding box (min x0, min y0,
    max x1, max y1 across all cells).
    """

    table_index: int = Field(
        ...,
        ge=0,
        description="0-indexed position of this table on the page (pdfplumber find_tables() order)",
    )
    pdf_id: str = Field(..., description="SHA-256 hex of the Bronze-stored normalised PDF")
    page: int = Field(..., ge=1, description="1-indexed page number")
    rows: list[list[str | None]] = Field(
        ...,
        description="Cell text matrix. rows[row_index][col_index] = cell text or None",
    )
    cell_bboxes: list[list[tuple[float, float, float, float]]] = Field(
        ...,
        description="Parallel cell bboxes. cell_bboxes[row][col] = (x0, y0, x1, y1) in PDF user-space",
    )
    provenance: PdfProvenance = Field(
        ...,
        description="Full provenance tuple per §04p contract (bbox = overall table bbox)",
    )


# ---------------------------------------------------------------------------
# Stage 3 — Response envelopes
# ---------------------------------------------------------------------------


class ExtractTextResponse(BaseModel):
    """Response envelope for GET /pdf/extract_text.

    The cache_hit flag is exposed for observability — operators can verify the
    Silver cache is being populated correctly by watching for cache_hit=false on
    the first extraction and cache_hit=true on subsequent calls for the same
    (pdf_id, page).
    """

    blocks: list[PdfTextBlock] = Field(
        default_factory=list,
        description="Text blocks extracted from the requested page(s)",
    )
    cache_hit: bool = Field(
        ...,
        description="True if results were served from the Silver cache; False on fresh extraction",
    )
    pdf_id: str = Field(..., description="pdf_id echoed from the request")
    page: int | None = Field(
        None,
        description="1-indexed page filter from the request; None = all pages",
    )


class FindTablesResponse(BaseModel):
    """Response envelope for GET /pdf/find_tables.

    cache_hit semantics match ExtractTextResponse.
    """

    tables: list[PdfTable] = Field(
        default_factory=list,
        description="Tables extracted from the requested page(s)",
    )
    cache_hit: bool = Field(
        ...,
        description="True if results were served from the Silver cache; False on fresh extraction",
    )
    pdf_id: str = Field(..., description="pdf_id echoed from the request")
    page: int | None = Field(
        None,
        description="1-indexed page filter from the request; None = all pages",
    )


# ---------------------------------------------------------------------------
# Stage 6 — Qwen-VL vision-language models (Phase 1.D)
# ---------------------------------------------------------------------------

# Literal type alias matching the model_backend CHECK constraint in
# silver.pdf_vl_summaries.  Must stay in sync with the migration.
VlBackend = Literal["ollama", "vllm", "anthropic"]

# Re-export VlClaim and VlSummaryShape from pdf_vl so the router and tests
# can import them from models.pdf without creating a circular dependency.
# The service module (pdf_vl.py) defines the authoritative models; this
# re-export keeps the import surface consistent with Phase 1.B/C patterns.
from app.services.pdf_vl import VlClaim  # noqa: E402 — placed after class defs


class SummarizeSectionResponse(BaseModel):
    """Response envelope for GET /pdf/summarize_section.

    Every response carries a per-claim ``provenance`` list so the FastAPI
    citation pipeline can satisfy §04i Citation completeness and Numeric
    grounding guards: one PdfProvenance per claim, each with (pdf_id, page,
    bbox) grounding the claim to its source location in the PDF.

    ``mean_claim_confidence`` is None when the LLM found zero grounded claims
    (empty-content sections); callers should treat this as low-confidence and
    surface it in the §04i refusal-path evaluation.

    ``cache_hit`` is exposed for observability — operators can verify the
    Silver cache is being populated correctly.
    """

    summary_id: uuid.UUID = Field(
        ...,
        description="Stable identifier for this summary in silver.pdf_vl_summaries",
    )
    pdf_id: str = Field(..., description="SHA-256 hex of the Bronze-stored normalised PDF")
    section_ref: dict[str, Any] = Field(
        ...,
        description=(
            "Opaque section reference echoed from the request.  "
            "One of: {kind:'page', page:int} | {kind:'page_range', page_start:int, page_end:int} | "
            "{kind:'layout_region', region_id:UUID}"
        ),
    )
    summary_text: str = Field(
        ...,
        description="Natural-language summary of the section produced by Qwen-VL",
    )
    claims: list[VlClaim] = Field(
        default_factory=list,
        description=(
            "Per-claim provenance list.  Every factual statement in summary_text "
            "must appear here with a (page, bbox) grounding tuple per §04p contract."
        ),
    )
    model_id: str = Field(
        ...,
        description="Model identifier used for inference, e.g. 'qwen2.5-vl:7b'",
    )
    model_backend: VlBackend = Field(
        ...,
        description="LLM backend that produced the summary",
    )
    mean_claim_confidence: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description=(
            "Mean of per-claim LLM confidence scores [0.0, 1.0].  "
            "None when the LLM produced zero grounded claims."
        ),
    )
    prompt_tokens: int | None = Field(
        None,
        description="Prompt token count from the LLM usage field (None when not reported)",
    )
    completion_tokens: int | None = Field(
        None,
        description="Completion token count from the LLM usage field (None when not reported)",
    )
    cache_hit: bool = Field(
        ...,
        description="True if results were served from the Silver cache; False on fresh VL inference",
    )
    provenance: list[PdfProvenance] = Field(
        default_factory=list,
        description=(
            "One PdfProvenance per claim, ordered to match the claims list.  "
            "source_method='qwen_vl', extraction_confidence=claim.confidence per §04p contract."
        ),
    )


# ---------------------------------------------------------------------------
# Phase 2.A — Deterministic coordinate extraction models
# ---------------------------------------------------------------------------

# Literal type alias matching the CHECK constraint vocabulary in
# silver.pdf_coordinates.  Must stay in sync with the migration.
CoordKind = Literal["utm", "latlon_decimal", "latlon_dms"]


class PdfCoordinate(BaseModel):
    """A single geographic coordinate extracted from a PDF text block.

    Deterministic regex + Pydantic bounds-check extraction per §04p Phase 2.A.
    The VL model never sees raw PDF text containing potential coordinate strings —
    it receives the typed, bounds-checked output of this service instead.

    source_method is always 'regex' for Phase 2.A.  Phase 2.B may add
    'llm_validated' for cases where the regex match is ambiguous and the agent
    requests VL confirmation.

    match_bbox is (x0, y0, x1, y1) in PDF user-space coordinates (bottom-left
    origin, y increases upward, units = points), or None when the char-offset
    → bbox derivation cannot produce a valid rectangle (degenerate block bbox).

    extraction_confidence is 1.0 for all regex+bounds-check passing matches
    (lossless — the raw_match is verbatim so a downstream auditor can confirm
    the regex didn't lie).
    """

    coord_id: uuid.UUID = Field(
        ...,
        description="Stable identifier for this coordinate in silver.pdf_coordinates",
    )
    pdf_id: str = Field(..., description="SHA-256 hex of the Bronze-stored normalised PDF")
    page: int = Field(..., ge=1, description="1-indexed page number")
    source_block_id: uuid.UUID | None = Field(
        None,
        description=(
            "Back-pointer to the source silver.pdf_text_blocks row.  "
            "Nullable: SET NULL if the text block is re-extracted and the block_id changes."
        ),
    )
    coord_kind: CoordKind = Field(
        ...,
        description="Coordinate type — matches the silver.pdf_coordinates CHECK enum",
    )
    raw_match: str = Field(
        ...,
        description=(
            "Verbatim substring that matched the regex.  "
            "Preserved exactly so a downstream auditor can verify the match."
        ),
    )
    match_bbox: tuple[float, float, float, float] | None = Field(
        None,
        description=(
            "(x0, y0, x1, y1) of the matched substring within the source block, "
            "in PDF user-space points (bottom-left origin, y-up).  "
            "None when char-offset → bbox derivation is not possible."
        ),
    )
    latitude: float | None = Field(
        None,
        ge=-90.0,
        le=90.0,
        description="Decimal latitude in [-90, 90].  NULL for UTM coordinates.",
    )
    longitude: float | None = Field(
        None,
        ge=-180.0,
        le=180.0,
        description="Decimal longitude in [-180, 180].  NULL for UTM coordinates.",
    )
    utm_zone: int | None = Field(
        None,
        ge=1,
        le=60,
        description="UTM zone number [1, 60].  NULL for lat/lon coordinates.",
    )
    utm_hemisphere: Literal["N", "S"] | None = Field(
        None,
        description="UTM hemisphere 'N' or 'S'.  NULL for lat/lon coordinates.",
    )
    utm_easting: float | None = Field(
        None,
        ge=100_000.0,
        le=900_000.0,
        description="UTM easting in metres [100000, 900000].  NULL for lat/lon coordinates.",
    )
    utm_northing: float | None = Field(
        None,
        ge=0.0,
        le=10_000_000.0,
        description="UTM northing in metres [0, 10000000].  NULL for lat/lon coordinates.",
    )
    datum: str | None = Field(
        None,
        description=(
            "Datum hint detected from nearby text (e.g. 'NAD83', 'NAD27', 'WGS84').  "
            "Free-text; not enum because there are many valid datums.  "
            "None when no datum was detected within 200 chars."
        ),
    )
    extraction_confidence: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence of the extraction [0.0, 1.0].  "
            "1.0 for regex+bounds-check passing matches (lossless).  "
            "Phase 2.B may lower this for LLM-validated ambiguous matches."
        ),
    )
    provenance: PdfProvenance = Field(
        ...,
        description=(
            "Full provenance tuple per §04p contract.  "
            "source_method='regex', extraction_confidence=1.0."
        ),
    )


class FindCoordinatesResponse(BaseModel):
    """Response envelope for GET /pdf/find_coordinates.

    Returns 200 + empty coordinates list when:
      (a) No text blocks have been extracted yet for this pdf_id/page
          (caller should pre-call extract_text to populate silver.pdf_text_blocks).
      (b) No coordinate patterns were found in the extracted text.

    In both cases cache_hit=False.  The distinction between (a) and (b) is
    observable from the extract_text endpoint (which returns blocks=[]).

    Returns 200 + non-empty coordinates list when matches were found (cache
    miss) or were previously cached (cache_hit=True).

    NOTE: 404 is reserved for "pdf_id not in Bronze store" — not for "no
    coordinates found".  Empty-result is the legitimate §04p outcome for a
    PDF page containing no coordinate strings.
    """

    coordinates: list[PdfCoordinate] = Field(
        default_factory=list,
        description="Coordinate matches extracted from the requested page(s), optionally filtered by coord_kind",
    )
    cache_hit: bool = Field(
        ...,
        description="True if results were served from the Silver cache; False on fresh extraction",
    )
    pdf_id: str = Field(..., description="pdf_id echoed from the request")
    page: int | None = Field(
        None,
        description="1-indexed page filter from the request; None = all pages",
    )
