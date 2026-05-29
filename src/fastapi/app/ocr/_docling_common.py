"""Shared Docling helpers for parse_mixed + parse_table_heavy.

Both modules call into Docling with the same pipeline configuration
(do_ocr=False — OCR is dispatched externally to parse_scanned per
ADR-0002's separation-of-concerns), and both need to normalize
Docling's document model into the canonical bbox / label shape used
by the silver schema.

The label mapping converts Docling's region labels into the
silver.ingest_layouts.layout_label CHECK-constrained enum values
(from the doc-phase 50 migration).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


# Docling label → silver.ingest_layouts.layout_label CHECK enum
# Anything not in this map → "other"
_DOCLING_LABEL_MAP = {
    "text": "text",
    "title": "title",
    "section_header": "section_header",
    "list_item": "list_item",
    "table": "table",
    "picture": "figure",
    "figure": "figure",
    "caption": "caption",
    "footnote": "footnote",
    "page_header": "page_header",
    "page_footer": "page_footer",
    "formula": "formula",
    "code": "code",
}


def normalize_label(docling_label: str | None) -> str:
    """Map a Docling layout label to the silver schema enum."""
    if docling_label is None:
        return "other"
    return _DOCLING_LABEL_MAP.get(docling_label.lower(), "other")


def _bbox_from_prov(prov: Any) -> list[float] | None:
    """Extract a [x0, y0, x1, y1] bbox from a Docling prov entry.

    Docling uses ``coord_origin`` (BOTTOMLEFT for PDF, TOPLEFT for
    image). We preserve Docling's native coord_origin in the bbox
    (BOTTOMLEFT for PDFs going through this stack) — same convention
    as pdfminer.six's output in parse_native. Callers writing to
    silver.ingest_layouts.bbox should not need to translate.
    """
    if prov is None or len(prov) == 0:
        return None
    bbox = getattr(prov[0], "bbox", None)
    if bbox is None:
        return None
    try:
        return [
            round(float(bbox.l), 3),
            round(float(bbox.b), 3),
            round(float(bbox.r), 3),
            round(float(bbox.t), 3),
        ]
    except (AttributeError, TypeError):
        return None


def _page_from_prov(prov: Any) -> int | None:
    """Extract the 0-indexed page number from a Docling prov entry.

    Docling uses 1-indexed page_no.
    """
    if prov is None or len(prov) == 0:
        return None
    page_no = getattr(prov[0], "page_no", None)
    if page_no is None:
        return None
    return int(page_no) - 1


def run_docling_no_ocr(pdf_path: Path) -> Any:
    """Run Docling with OCR disabled. Returns the converted document.

    OCR work is dispatched to parse_scanned at the orchestration layer
    (Step 7). This keeps Docling fast on native pages and avoids the
    RapidOCR-cache-write permission issue in the FastAPI container.
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.do_ocr = False
    # Table-structure detection stays on; it's the value-add of using
    # Docling here. The TableFormer model is bundled in the docling
    # package (not separately downloaded) so no permission concern.

    conv = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=opts),
        }
    )
    result = conv.convert(str(pdf_path))
    return result.document


def extract_table_cells(table_obj: Any) -> list[list[str | None]]:
    """Extract a 2D cell grid from a Docling table item.

    Docling's table data shape: ``table.data.table_cells`` is a list
    of cell objects each with ``start_row_offset_idx``, ``end_row_offset_idx``,
    ``start_col_offset_idx``, ``end_col_offset_idx``, ``text``.
    Spans are flattened (the cell text appears once at its top-left
    position; other cells in the span are empty).
    """
    if not hasattr(table_obj, "data"):
        return []
    data = table_obj.data
    cells = getattr(data, "table_cells", None) or getattr(data, "cells", None)
    if not cells:
        return []

    # Determine grid size
    max_row = 0
    max_col = 0
    for c in cells:
        max_row = max(max_row, int(getattr(c, "end_row_offset_idx", 0)))
        max_col = max(max_col, int(getattr(c, "end_col_offset_idx", 0)))

    if max_row == 0 or max_col == 0:
        return []

    grid: list[list[str | None]] = [
        [None] * max_col for _ in range(max_row)
    ]
    for c in cells:
        r = int(getattr(c, "start_row_offset_idx", 0))
        col = int(getattr(c, "start_col_offset_idx", 0))
        text = getattr(c, "text", None)
        if 0 <= r < max_row and 0 <= col < max_col:
            grid[r][col] = text
    return grid
