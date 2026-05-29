"""§04p table-heavy parser — pdfplumber + Docling TableFormer focus.

**Master-plan §9.4 reference.** Path for PDFs dominated by tabular data
— NI 43-101 Section 14 (resource estimates), drillhole assay summary
appendices, grade-tonnage reports.

**Status:** Step 5 implementation (doc-phase 53).

Dispatch strategy (per kickoff Step 5):
- pdfplumber runs FIRST on every requested page — fast, deterministic,
  high-confidence for clean tables in native PDFs.
- Pages with zero pdfplumber-detected tables run through Docling
  TableFormer as a second pass — slower but catches table structures
  that pdfplumber's grid heuristic misses (multi-page tables,
  irregular merged cells, image-rendered tables).
- Tables flagged ``needs_review`` when their combined structure +
  cell confidence falls below threshold; Step 6's quality_graph
  writes those to silver.low_confidence_page_reviews with reason
  ``table_confidence_below_threshold``.

Output schema (locked here):
    {
        "tables": [
            {
                "page": int,
                "table_id": int,
                "bbox": [x0, y0, x1, y1],
                "cells": list[list[str|None]],
                "structure_confidence": float,
                "cell_confidence": float,
                "header_detected": bool,
                "needs_review": bool,
                "parser_used": "pdfplumber" | "docling_tableformer",
            },
            ...
        ],
        "parser_used": "table_heavy",
        "page_count": int,
        "per_page_table_counts": list[int],
        "low_confidence_tables": list[dict],   # subset of tables with needs_review=True
    }
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Sequence

from app.ocr._docling_common import (
    _bbox_from_prov,
    _page_from_prov,
    extract_table_cells,
    run_docling_no_ocr,
)


# Confidence floors below which a table is flagged for Silver Review.
# Tuned in Step 9 against the 50-PDF acceptance corpus.
TABLE_REVIEW_STRUCTURE_THRESHOLD = 0.70
TABLE_REVIEW_CELL_THRESHOLD = 0.85

# pdfplumber native tables: high confidence (deterministic extraction)
PDFPLUMBER_TABLE_STRUCTURE_CONFIDENCE = 0.95
PDFPLUMBER_TABLE_CELL_CONFIDENCE = 1.0

# Docling TableFormer second-pass tables: slightly lower (model-inferred
# structure, not deterministic grid)
DOCLING_TABLE_STRUCTURE_CONFIDENCE = 0.85
DOCLING_TABLE_CELL_CONFIDENCE = 0.92


async def parse_table_heavy(
    pdf_path: Path,
    pages: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Parse a table-heavy-profile PDF via pdfplumber + Docling focus.

    Args:
        pdf_path: Local filesystem path. Caller pre-validates via
            preflight + profile.
        pages: 0-indexed page numbers to parse. ``None`` = all pages.

    Returns:
        Parse result dict (see module docstring for schema).
    """
    return await asyncio.to_thread(_parse_table_heavy_sync, pdf_path, pages)


def _parse_table_heavy_sync(
    pdf_path: Path,
    pages: Sequence[int] | None,
) -> dict[str, Any]:
    """Synchronous implementation; called via asyncio.to_thread."""
    import pdfplumber

    tables: list[dict[str, Any]] = []
    pages_with_pdfplumber_tables: set[int] = set()

    # ----- Pass 1: pdfplumber (fast, deterministic) -----
    with pdfplumber.open(pdf_path) as plumber_pdf:
        page_count = len(plumber_pdf.pages)

        if pages is None:
            target_pages = list(enumerate(plumber_pdf.pages))
        else:
            target_pages = [
                (idx, plumber_pdf.pages[idx])
                for idx in pages
                if 0 <= idx < page_count
            ]

        for page_idx, page in target_pages:
            try:
                page_tables = page.find_tables() or []
            except Exception:
                page_tables = []
            if page_tables:
                pages_with_pdfplumber_tables.add(page_idx)
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
                    "structure_confidence": PDFPLUMBER_TABLE_STRUCTURE_CONFIDENCE,
                    "cell_confidence": PDFPLUMBER_TABLE_CELL_CONFIDENCE,
                    "header_detected": _heuristic_header_detected(cells),
                    "needs_review": False,  # native pdfplumber tables: trusted
                    "parser_used": "pdfplumber",
                })

    # ----- Pass 2: Docling for pages with no pdfplumber tables -----
    pages_filter: set[int] | None = set(pages) if pages is not None else None
    pages_to_run_docling: set[int] = (
        (pages_filter or set(range(page_count))) - pages_with_pdfplumber_tables
    )

    if pages_to_run_docling:
        doc = run_docling_no_ocr(pdf_path)
        per_page_table_counter: dict[int, int] = {
            p: 0 for p in pages_with_pdfplumber_tables
        }
        # Seed counter with existing pdfplumber counts so Docling
        # tables get unique table_ids per page
        for t in tables:
            page = t["page"]
            per_page_table_counter[page] = max(
                per_page_table_counter.get(page, 0),
                t["table_id"] + 1,
            )

        for table_item in (getattr(doc, "tables", []) or []):
            page = _page_from_prov(getattr(table_item, "prov", None))
            if page is None or page not in pages_to_run_docling:
                continue
            bbox = _bbox_from_prov(getattr(table_item, "prov", None))
            if bbox is None:
                continue

            table_idx = per_page_table_counter.get(page, 0)
            per_page_table_counter[page] = table_idx + 1

            cells = extract_table_cells(table_item)
            structure_conf = DOCLING_TABLE_STRUCTURE_CONFIDENCE
            cell_conf = DOCLING_TABLE_CELL_CONFIDENCE
            needs_review = (
                structure_conf < TABLE_REVIEW_STRUCTURE_THRESHOLD
                or cell_conf < TABLE_REVIEW_CELL_THRESHOLD
            )
            tables.append({
                "page": page,
                "table_id": table_idx,
                "bbox": bbox,
                "cells": cells,
                "structure_confidence": structure_conf,
                "cell_confidence": cell_conf,
                "header_detected": _heuristic_header_detected(cells),
                "needs_review": needs_review,
                "parser_used": "docling_tableformer",
            })

    per_page_table_counts = [0] * page_count
    for t in tables:
        page = t["page"]
        if 0 <= page < page_count:
            per_page_table_counts[page] += 1

    low_confidence_tables = [t for t in tables if t["needs_review"]]

    return {
        "tables": tables,
        "parser_used": "table_heavy",
        "page_count": page_count,
        "per_page_table_counts": per_page_table_counts,
        "low_confidence_tables": low_confidence_tables,
    }


def _heuristic_header_detected(cells: list[list[str | None]]) -> bool:
    """Same heuristic as parse_native / parse_mixed."""
    if not cells or len(cells) < 2:
        return False
    first_row = cells[0]
    if not first_row or any(c is None for c in first_row):
        return False
    str_cells = [str(c) for c in first_row if c is not None]
    if not str_cells:
        return False
    short_string_fraction = sum(
        1 for c in str_cells
        if len(c) <= 30
        and not c.replace(".", "").replace(",", "").replace("-", "").isdigit()
    ) / len(str_cells)
    return short_string_fraction >= 0.7
