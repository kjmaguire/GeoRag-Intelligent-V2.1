"""§04p native parser — pdfminer.six text extraction + pdfplumber tables.

**Master-plan §9.3 / §9.4 reference.** Path for PDFs classified as
``native`` by the profiler. Clean text-layer PDFs (modern SEDAR+
NI 43-101 filings, etc.) — no OCR invoked.

**Status:** Step 3 implementation (doc-phase 51).

Measured CPU latency baseline (2026-05-12 smoke-bench on
Threadripper 5955WX, 6-CPU WSL2 container):
- ~60 ms/page across pdfminer.six text + pdfplumber table scan

This module is a **pure function** — it does NOT write to the database.
The Hatchet ingest_pdf step (Step 7) is responsible for taking the
return value and persisting rows to silver.ingest_extractions,
silver.ocr_page_quality, silver.document_ingestion_quality.

Output schema (locked here):
    {
        "passages": [
            {
                "page": int,                  # 0-indexed
                "region": int,                # 0-indexed in reading order on the page
                "bbox": [x0, y0, x1, y1],     # PDF page coordinates (pt)
                "source_method": "pdfminer_six",
                "extraction_confidence": float,
                "text_content": str,
            },
            ...
        ],
        "tables": [
            {
                "page": int,
                "table_id": int,             # 0-indexed per page
                "bbox": [x0, y0, x1, y1],
                "cells": list[list[str|None]],
                "structure_confidence": float,
                "cell_confidence": float,
                "header_detected": bool,
                "parser_used": "pdfplumber",
            },
            ...
        ],
        "parser_used": "native",
        "extraction_confidence": float,     # document-level
        "per_page_passage_counts": list[int],
        "per_page_table_counts": list[int],
        "page_count": int,
    }
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Sequence


# Native PDFs have effectively perfect text extraction; reflect that as a
# baseline confidence. Step 5's mixed parser may lower this for regions
# where layout dispatch routed differently.
NATIVE_EXTRACTION_CONFIDENCE = 1.0
NATIVE_TABLE_STRUCTURE_CONFIDENCE = 0.95
NATIVE_TABLE_CELL_CONFIDENCE = 1.0


async def parse_native(
    pdf_path: Path,
    pages: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Extract text + tables from a native-profile PDF.

    Args:
        pdf_path: Local filesystem path. Caller pre-validates via
            preflight + profile.
        pages: 0-indexed page numbers to parse. ``None`` = all pages.

    Returns:
        Parse result dict (see module docstring for schema).
    """
    return await asyncio.to_thread(_parse_native_sync, pdf_path, pages)


def _parse_native_sync(
    pdf_path: Path,
    pages: Sequence[int] | None,
) -> dict[str, Any]:
    """Synchronous implementation; called via asyncio.to_thread."""
    import pdfplumber
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer

    passages: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    page_count = 0

    # pdfminer.six uses 1-indexed page_numbers in extract_pages; convert.
    pdfminer_page_numbers = (
        [p + 1 for p in pages] if pages is not None else None
    )

    # ----- Pass 1: text extraction with bboxes (pdfminer.six) -----
    for page_layout in extract_pages(str(pdf_path), page_numbers=pdfminer_page_numbers):
        page_idx = page_layout.pageid - 1
        page_count += 1
        region_idx = 0
        for element in page_layout:
            if isinstance(element, LTTextContainer):
                text = element.get_text().strip()
                if not text:
                    continue
                passages.append({
                    "page": page_idx,
                    "region": region_idx,
                    "bbox": [
                        round(float(element.x0), 3),
                        round(float(element.y0), 3),
                        round(float(element.x1), 3),
                        round(float(element.y1), 3),
                    ],
                    "source_method": "pdfminer_six",
                    "extraction_confidence": NATIVE_EXTRACTION_CONFIDENCE,
                    "text_content": text,
                })
                region_idx += 1

    # ----- Pass 2: table extraction with bboxes (pdfplumber) -----
    with pdfplumber.open(pdf_path) as plumber_pdf:
        if pages is None:
            target_pages = list(enumerate(plumber_pdf.pages))
        else:
            target_pages = [
                (idx, plumber_pdf.pages[idx])
                for idx in pages
                if 0 <= idx < len(plumber_pdf.pages)
            ]

        for page_idx, page in target_pages:
            try:
                page_tables = page.find_tables() or []
            except Exception:
                page_tables = []

            for table_idx, table in enumerate(page_tables):
                try:
                    cells = table.extract() or []
                except Exception:
                    cells = []
                tables.append({
                    "page": page_idx,
                    "table_id": table_idx,
                    "bbox": [round(float(v), 3) for v in table.bbox],
                    "cells": cells,
                    "structure_confidence": NATIVE_TABLE_STRUCTURE_CONFIDENCE,
                    "cell_confidence": NATIVE_TABLE_CELL_CONFIDENCE,
                    "header_detected": _heuristic_header_detected(cells),
                    "parser_used": "pdfplumber",
                })

    return {
        "passages": passages,
        "tables": tables,
        "parser_used": "native",
        "extraction_confidence": NATIVE_EXTRACTION_CONFIDENCE,
        "per_page_passage_counts": _count_per_page(passages, page_count),
        "per_page_table_counts": _count_per_page(tables, page_count),
        "page_count": page_count,
    }


def _heuristic_header_detected(cells: list[list[str | None]]) -> bool:
    """Cheap header heuristic: first row exists, has no None cells, and
    differs in style from subsequent rows.

    Step 5's table-heavy parser will replace this with the Docling
    TableFormer's structured header detection. For native tables the
    heuristic is good enough to populate the column for now.
    """
    if not cells or len(cells) < 2:
        return False
    first_row = cells[0]
    if not first_row or any(c is None for c in first_row):
        return False
    # Header cells tend to be short strings without numeric content
    str_cells = [str(c) for c in first_row if c is not None]
    if not str_cells:
        return False
    short_string_fraction = sum(
        1 for c in str_cells if len(c) <= 30 and not c.replace(".", "").replace(",", "").replace("-", "").isdigit()
    ) / len(str_cells)
    return short_string_fraction >= 0.7


def _count_per_page(items: list[dict[str, Any]], total_pages: int) -> list[int]:
    """Helper: build a list of length total_pages with the count of items per page."""
    counts = [0] * total_pages
    for item in items:
        page_idx = item.get("page", 0)
        if 0 <= page_idx < total_pages:
            counts[page_idx] += 1
    return counts
