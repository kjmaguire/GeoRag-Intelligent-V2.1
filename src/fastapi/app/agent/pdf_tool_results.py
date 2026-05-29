"""Pydantic result models for the 8 PDF subsystem agent tools.

§04p Phase 2.B-i — agent-binding pass.

All 8 tool wrappers in agentic_escalation.py return one of these models.
The ``success: bool`` + ``error: str | None`` envelope is mandatory so the
Pydantic AI agent receives a structured failure payload (useful for
self-correction) instead of an unhandled exception (which creates silent
quality regressions).

Summary helpers (PdfTextBlockSummary etc.) intentionally omit low-value
fields (font metadata, extraction_confidence, raw block IDs) to keep the
token footprint manageable.  The agent rarely needs sub-character font data;
it needs coordinates, text, and provenance.

The ``source_chunk_id`` field requirement from §04i Citation completeness is
enforced at synthesis time (the agent's GeoRAGResponse output model), NOT
inside these tool results.  These models carry (pdf_id, page, bbox) tuples
that the synthesiser must reference when assembling citations.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Summary helper models (shared across multiple tool result types)
# ---------------------------------------------------------------------------


class PdfTextBlockSummary(BaseModel):
    """Condensed representation of a single PDF text block.

    Font metadata (font_name, font_size) is omitted here to keep agent-context
    tokens manageable.  The full schema is in silver.pdf_text_blocks.
    """

    page: int = Field(..., description="1-indexed page number where this text block appears")
    text: str = Field(..., description="Extracted text content of the block")
    bbox: tuple[float, float, float, float] = Field(
        ...,
        description="(x0, y0, x1, y1) in PDF user-space points (bottom-left origin, y-up)",
    )


class PdfTableSummary(BaseModel):
    """Condensed representation of a single PDF table."""

    page: int = Field(..., description="1-indexed page number containing this table")
    table_index: int = Field(..., description="0-indexed table ordinal within the page")
    rows: list[list[str | None]] = Field(
        ...,
        description="Cell text matrix — outer list = rows, inner list = columns. None = empty cell",
    )
    bbox_first_cell: tuple[float, float, float, float] | None = Field(
        default=None,
        description=(
            "(x0, y0, x1, y1) of the first cell (row 0, col 0). "
            "Use as a location anchor when citing the table in a GeoRAGResponse."
        ),
    )
    total_cells: int = Field(..., description="Total number of cells (rows × cols)")


class PdfLayoutRegionSummary(BaseModel):
    """Condensed representation of a Docling-detected layout region."""

    page: int = Field(..., description="1-indexed page number where this region appears")
    region_index: int = Field(..., description="0-indexed region ordinal within the page")
    region_type: str = Field(
        ...,
        description=(
            "Layout class: text | figure | table | header | footer | "
            "formula | title | caption | footnote | list | page_number | unknown"
        ),
    )
    bbox: tuple[float, float, float, float] = Field(
        ...,
        description="(x0, y0, x1, y1) in PDF user-space points (bottom-left origin, y-up)",
    )
    region_confidence: float | None = Field(
        default=None,
        description="Docling detection confidence [0.0, 1.0], or None when not available",
    )


class VlClaimSummary(BaseModel):
    """A single VL-model claim with its (page, bbox) provenance anchor.

    This is the §04p (pdf_id, page, bbox) provenance contract carrier.
    The pdf_id is implicit from the parent PdfSummarizeSectionToolResult.
    Every claim here MUST be cited (by page + bbox) in the agent's final
    GeoRAGResponse — enforced by §04i Citation completeness (Layer 2).
    """

    claim_text: str = Field(..., description="Verbatim factual claim from the VL summary")
    page: int = Field(..., description="1-indexed page where this claim is visually grounded")
    bbox: tuple[float, float, float, float] = Field(
        ...,
        description="(x0, y0, x1, y1) in PDF user-space points grounding this claim",
    )
    confidence: float = Field(
        ...,
        description="VL model self-reported confidence that the claim is accurately grounded [0.0, 1.0]",
    )


class PdfCoordinateSummary(BaseModel):
    """A single geographic coordinate extracted by deterministic regex.

    Coordinates come from §04p Stage 3.5 (regex over silver.pdf_text_blocks).
    Never invented by the VL model — the §04p key-note enforces this.
    """

    coord_kind: str = Field(
        ...,
        description="Type of coordinate: 'utm' | 'latlon_decimal' | 'latlon_dms'",
    )
    raw_match: str = Field(
        ...,
        description="Raw text match from the PDF (what the regex captured verbatim)",
    )
    latitude: float | None = Field(
        default=None,
        description="WGS-84 latitude (decimal degrees, south negative). None for UTM-only matches.",
    )
    longitude: float | None = Field(
        default=None,
        description="WGS-84 longitude (decimal degrees, west negative). None for UTM-only matches.",
    )
    utm_zone: int | None = Field(
        default=None,
        description="UTM zone number [1, 60]. None for lat/lon matches.",
    )
    utm_easting: float | None = Field(
        default=None,
        description="UTM easting (metres). None for lat/lon matches.",
    )
    utm_northing: float | None = Field(
        default=None,
        description="UTM northing (metres). None for lat/lon matches.",
    )
    datum: str | None = Field(
        default=None,
        description=(
            "Datum hint found within 200 chars of the match (e.g. 'NAD83', 'WGS 84'). "
            "None when no datum text was nearby."
        ),
    )
    page: int | None = Field(
        default=None,
        description="1-indexed page number where this coordinate was found",
    )


# ---------------------------------------------------------------------------
# Per-tool result models (one per @agent.tool wrapper)
# ---------------------------------------------------------------------------


class PdfRenderPageToolResult(BaseModel):
    """Result of the pdf_render_page tool.

    On success, png_base64 contains the full-page PNG encoded as base64.
    Token-cost note: a 200-DPI A4 page PNG is typically 300–800 KB of base64.
    The agent should call this tool only when it genuinely needs to SEE the
    page (e.g., before deciding which find_legends regions to request next).
    For coordinate or table queries, call the structured tools directly.
    """

    success: bool = Field(..., description="True when render succeeded")
    error: str | None = Field(
        default=None,
        description="Human-readable error reason when success=False",
    )
    pdf_id: str | None = Field(default=None, description="pdf_id that was rendered")
    page: int | None = Field(default=None, description="1-indexed page that was rendered")
    dpi: int | None = Field(default=None, description="Render resolution used")
    png_base64: str | None = Field(
        default=None,
        description=(
            "Base64-encoded PNG bytes of the rendered page. "
            "Present only when success=True."
        ),
    )


class PdfCropRegionToolResult(BaseModel):
    """Result of the pdf_crop_region tool.

    Returns a base64 PNG of the cropped region.  Smaller than a full-page
    render so token cost is lower, but still substantial for large regions.
    """

    success: bool = Field(..., description="True when crop succeeded")
    error: str | None = Field(default=None)
    pdf_id: str | None = Field(default=None)
    page: int | None = Field(default=None)
    bbox: tuple[float, float, float, float] | None = Field(
        default=None,
        description="The bbox that was cropped: (x0, y0, x1, y1) in PDF user-space points",
    )
    dpi: int | None = Field(default=None)
    png_base64: str | None = Field(
        default=None,
        description="Base64-encoded PNG bytes of the cropped region. Present only when success=True.",
    )


class PdfExtractTextToolResult(BaseModel):
    """Result of the pdf_extract_text tool.

    Font metadata is omitted from each block to save tokens.  The agent can
    use page + bbox from each block to cite the source in its GeoRAGResponse.
    """

    success: bool = Field(..., description="True when extraction succeeded (even if blocks is empty)")
    error: str | None = Field(default=None)
    blocks: list[PdfTextBlockSummary] = Field(
        default_factory=list,
        description=(
            "Extracted text blocks, ordered top-to-bottom, left-to-right. "
            "Empty list when the page has no extractable text (image-only page)."
        ),
    )
    cache_hit: bool = Field(
        default=False,
        description="True when results came from the Silver cache (no extraction was re-run)",
    )


class PdfFindTablesToolResult(BaseModel):
    """Result of the pdf_find_tables tool.

    Tables are returned with their full cell-text matrix so the agent can
    read assay values, grades, resource estimates etc. directly.  The first-
    cell bbox is enough to cite the table location in a GeoRAGResponse.
    """

    success: bool = Field(..., description="True when table extraction succeeded")
    error: str | None = Field(default=None)
    tables: list[PdfTableSummary] = Field(
        default_factory=list,
        description="Detected tables. Empty list when no tables were found.",
    )
    cache_hit: bool = Field(default=False)


class PdfFindLegendsToolResult(BaseModel):
    """Result of the pdf_find_legends tool (layout region detection).

    'Legends' here is shorthand for Docling layout regions — the tool
    surfaces figure regions, table outlines, headers, titles, etc.  Use
    the bbox from a 'figure' or 'table' region to drive a subsequent
    pdf_crop_region → pdf_ocr_region chain.
    """

    success: bool = Field(..., description="True when layout detection succeeded")
    error: str | None = Field(default=None)
    regions: list[PdfLayoutRegionSummary] = Field(
        default_factory=list,
        description=(
            "Detected layout regions.  Filter by region_type "
            "('figure', 'table', 'header', 'title' etc.) to locate target content."
        ),
    )
    cache_hit: bool = Field(default=False)


class PdfOcrRegionToolResult(BaseModel):
    """Result of the pdf_ocr_region tool.

    Lines are collapsed to a single text_content string + mean_confidence
    to save tokens.  If the agent needs individual line bboxes it should
    use pdf_crop_region + inspect the image instead.

    mean_confidence follows the PaddleOCR convention: 1.0 when no lines
    were detected (the model could not find any text, but did not error).
    """

    success: bool = Field(..., description="True when OCR completed (even if no text was found)")
    error: str | None = Field(default=None)
    text_content: str | None = Field(
        default=None,
        description=(
            "All recognised text concatenated with newlines. "
            "None when the region contained no detectable text or OCR errored."
        ),
    )
    mean_confidence: float | None = Field(
        default=None,
        description="Mean per-line OCR confidence [0.0, 1.0]. 1.0 when no lines detected.",
    )
    cache_hit: bool = Field(default=False)


class PdfSummarizeSectionToolResult(BaseModel):
    """Result of the pdf_summarize_section tool (Qwen-VL inference).

    CRITICAL — §04i Citation completeness (Layer 2):
    Every claim in the ``claims`` list carries (page, bbox) provenance.
    When the agent quotes a claim in its final GeoRAGResponse it MUST
    reference the claim's (page, bbox) as the source_chunk_id or the
    Pydantic AI typed-output validator will reject the response.

    §04p Determinism rule:
    Numeric values (assays, depths, UTM coordinates) that appear in
    summary_text MUST be cross-checked with verify_numerical_claim (for
    structured data) or pdf_find_coordinates (for UTM/lat-lon) before being
    quoted to the user.  VL model output is NOT a primary numeric source.
    """

    success: bool = Field(...)
    error: str | None = Field(default=None)
    summary_text: str | None = Field(
        default=None,
        description="Natural-language section summary produced by Qwen-VL",
    )
    claims: list[VlClaimSummary] = Field(
        default_factory=list,
        description=(
            "Structured claims with (page, bbox) provenance. "
            "Each claim MUST be cited in the final GeoRAGResponse."
        ),
    )
    cache_hit: bool = Field(default=False)


class PdfFindCoordinatesToolResult(BaseModel):
    """Result of the pdf_find_coordinates tool.

    Coordinates are extracted deterministically by regex (§04p Stage 3.5).
    The VL model NEVER invents coordinate values — this tool is the
    authoritative source for all UTM and lat/lon values from a PDF.

    Usage note: extract_text MUST be called first for the target (pdf_id,
    page) combination so that silver.pdf_text_blocks is populated.  If the
    text blocks are absent, the tool returns success=True with an empty
    coordinates list and a message in error explaining what to do next.
    """

    success: bool = Field(...)
    error: str | None = Field(default=None)
    coordinates: list[PdfCoordinateSummary] = Field(
        default_factory=list,
        description=(
            "Regex-extracted coordinates.  Empty list means either no "
            "coordinates were found OR extract_text has not been run yet "
            "(check the error field for guidance)."
        ),
    )
    cache_hit: bool = Field(default=False)
