"""§04p mixed parser — Docling layout-first with per-region method dispatch.

**Master-plan §9.3 / §9.4 reference.** Path for PDFs classified as
``mixed`` — some pages have a text layer, some are scanned, all in one
document.

**Status:** Step 5 implementation (doc-phase 53).

Dispatch strategy:
- Docling runs with ``do_ocr=False`` and produces layout regions per
  page with text content for native pages, empty content for scanned
  pages.
- Pages where Docling returned zero text content are flagged for
  external OCR via parse_scanned. The flag is in the return dict's
  ``pages_needing_ocr`` field; the Hatchet orchestrator (Step 7) is
  responsible for calling parse_scanned on those pages.
- Tables come back as Docling table items with cell-level structure.

OCR is NOT invoked from within this module — the parse_scanned
dispatch happens at the orchestration layer per ADR-0002's
separation-of-concerns (and to avoid the RapidOCR cache-write
permission issue with Docling's built-in OCR).

Measured CPU latency baseline (2026-05-12 smoke-bench):
- ~11.8 sec/page (synthetic mixed PDF with 5 pages)
- Dominated by Docling's layout model + TableFormer inference on CPU

Output schema (locked here):
    {
        "passages": [
            {
                "page": int,
                "region": int,
                "bbox": [x0, y0, x1, y1],
                "source_method": "docling_text_region",
                "extraction_confidence": float,
                "text_content": str,
                "layout_label": str,
            },
            ...
        ],
        "tables": [
            {
                "page": int,
                "table_id": int,
                "bbox": [x0, y0, x1, y1],
                "cells": list[list[str|None]],
                "structure_confidence": float,
                "cell_confidence": float,
                "header_detected": bool,
                "parser_used": "docling_tableformer",
            },
            ...
        ],
        "layouts": [
            {
                "page": int,
                "region": int,
                "bbox": [x0, y0, x1, y1],
                "source_method": "docling_layout_default",
                "extraction_confidence": float,
                "layout_label": str,
                "has_text": bool,
            },
            ...
        ],
        "parser_used": "mixed_docling",
        "page_count": int,
        "per_page_layout_confidence": list[float],
        "per_page_text_region_counts": list[int],
        "pages_needing_ocr": list[int],   # 0-indexed pages with no Docling text
    }
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.ocr._docling_common import (
    _bbox_from_prov,
    _page_from_prov,
    extract_table_cells,
    normalize_label,
    run_docling_no_ocr,
)

# Docling-extracted text from native pages: high confidence (the text
# layer is authoritative). Scanned pages return empty text; Step 7
# routes those to parse_scanned which assigns its own per-line confidence.
MIXED_NATIVE_CONFIDENCE = 1.0
MIXED_TABLE_STRUCTURE_CONFIDENCE = 0.90
MIXED_TABLE_CELL_CONFIDENCE = 0.95


async def parse_mixed(
    pdf_path: Path,
    pages: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Parse a mixed-profile PDF via Docling layout-first dispatch.

    Args:
        pdf_path: Local filesystem path. Caller pre-validates via
            preflight + profile.
        pages: 0-indexed page numbers to parse. ``None`` = all pages.
            NOTE: Docling parses the full document; this argument
            filters the output, not the work. For per-page scoping
            in production, the Hatchet orchestrator splits documents
            BEFORE calling this function.

    Returns:
        Parse result dict (see module docstring for schema).
    """
    return await asyncio.to_thread(_parse_mixed_sync, pdf_path, pages)


def _parse_mixed_sync(
    pdf_path: Path,
    pages: Sequence[int] | None,
) -> dict[str, Any]:
    """Synchronous implementation; called via asyncio.to_thread."""
    doc = run_docling_no_ocr(pdf_path)

    pages_filter: set[int] | None = set(pages) if pages is not None else None

    passages: list[dict[str, Any]] = []
    layouts: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []

    # Track per-page region counters for stable 0-indexed region ids
    per_page_region_counter: dict[int, int] = {}
    per_page_text_counts: dict[int, int] = {}

    texts = getattr(doc, "texts", []) or []
    for item in texts:
        page = _page_from_prov(getattr(item, "prov", None))
        if page is None:
            continue
        if pages_filter is not None and page not in pages_filter:
            continue
        bbox = _bbox_from_prov(getattr(item, "prov", None))
        if bbox is None:
            continue

        label = normalize_label(getattr(item, "label", None))
        text = (getattr(item, "text", "") or "").strip()

        region_idx = per_page_region_counter.get(page, 0)
        per_page_region_counter[page] = region_idx + 1

        # Layout row (always — every detected region)
        layouts.append({
            "page": page,
            "region": region_idx,
            "bbox": bbox,
            "source_method": "docling_layout_default",
            "extraction_confidence": MIXED_NATIVE_CONFIDENCE,
            "layout_label": label,
            "has_text": bool(text),
        })

        # Passage row (only when we have text)
        if text:
            passages.append({
                "page": page,
                "region": region_idx,
                "bbox": bbox,
                "source_method": "docling_text_region",
                "extraction_confidence": MIXED_NATIVE_CONFIDENCE,
                "text_content": text,
                "layout_label": label,
            })
            per_page_text_counts[page] = per_page_text_counts.get(page, 0) + 1

    # Tables
    docling_tables = getattr(doc, "tables", []) or []
    per_page_table_counter: dict[int, int] = {}
    for table_item in docling_tables:
        page = _page_from_prov(getattr(table_item, "prov", None))
        if page is None:
            continue
        if pages_filter is not None and page not in pages_filter:
            continue
        bbox = _bbox_from_prov(getattr(table_item, "prov", None))
        if bbox is None:
            continue

        table_idx = per_page_table_counter.get(page, 0)
        per_page_table_counter[page] = table_idx + 1

        cells = extract_table_cells(table_item)
        tables.append({
            "page": page,
            "table_id": table_idx,
            "bbox": bbox,
            "cells": cells,
            "structure_confidence": MIXED_TABLE_STRUCTURE_CONFIDENCE,
            "cell_confidence": MIXED_TABLE_CELL_CONFIDENCE,
            "header_detected": _heuristic_header_detected(cells),
            "parser_used": "docling_tableformer",
        })

    page_count = _resolve_page_count(doc, pages_filter)
    per_page_layout_confidence = _per_page_array(
        layouts, page_count, MIXED_NATIVE_CONFIDENCE
    )
    per_page_text_region_counts = _per_page_array(
        passages, page_count, 0, count_only=True
    )

    # Pages with at least one Docling region but zero text → need OCR
    pages_with_layouts = {l["page"] for l in layouts}  # noqa: E741
    pages_with_text = {p["page"] for p in passages}
    pages_needing_ocr = sorted(pages_with_layouts - pages_with_text)
    # Also flag pages Docling returned absolutely nothing for (likely
    # full-page images or rasterized scans)
    if pages_filter is None:
        all_pages = set(range(page_count))
        pages_with_anything = pages_with_layouts | pages_with_text
        pages_needing_ocr = sorted(set(pages_needing_ocr) | (all_pages - pages_with_anything))

    return {
        "passages": passages,
        "tables": tables,
        "layouts": layouts,
        "parser_used": "mixed_docling",
        "page_count": page_count,
        "per_page_layout_confidence": per_page_layout_confidence,
        "per_page_text_region_counts": per_page_text_region_counts,
        "pages_needing_ocr": pages_needing_ocr,
    }


def _heuristic_header_detected(cells: list[list[str | None]]) -> bool:
    """Copy of parse_native's heuristic — first row has short non-numeric
    cells. Step 5 could replace with Docling's structured header
    detection if its public API exposes it.
    """
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


def _resolve_page_count(doc: Any, pages_filter: set[int] | None) -> int:
    """Best-effort page count from Docling's document."""
    if pages_filter is not None:
        return len(pages_filter)
    pages_attr = getattr(doc, "pages", None)
    if pages_attr is not None:
        try:
            return len(pages_attr)
        except TypeError:
            pass
    # Fall back to inspecting prov entries
    pages_seen = set()
    for item in (getattr(doc, "texts", None) or []):
        p = _page_from_prov(getattr(item, "prov", None))
        if p is not None:
            pages_seen.add(p)
    return max(pages_seen) + 1 if pages_seen else 0


def _per_page_array(
    items: list[dict[str, Any]],
    page_count: int,
    default: float,
    count_only: bool = False,
) -> list[float | int]:
    """Build a per-page array of length page_count.

    When count_only=True returns counts (int); otherwise returns the
    mean of `extraction_confidence` per page (float), with `default`
    for pages with no items.
    """
    if page_count == 0:
        return []
    if count_only:
        counts = [0] * page_count
        for item in items:
            page = item.get("page", 0)
            if 0 <= page < page_count:
                counts[page] += 1
        return counts

    sums = [0.0] * page_count
    counts = [0] * page_count
    for item in items:
        page = item.get("page", 0)
        if 0 <= page < page_count:
            sums[page] += float(item.get("extraction_confidence", default))
            counts[page] += 1
    return [
        round(sums[i] / counts[i], 4) if counts[i] > 0 else default
        for i in range(page_count)
    ]
