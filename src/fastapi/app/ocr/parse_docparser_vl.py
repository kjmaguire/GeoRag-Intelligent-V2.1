"""§04p PaddleOCR-VL doc-parser — ADR-0016 Phase 2 (Proposed).

Full-page, layout-aware document parsing via **PaddleOCR-VL-1.6** — the
end-to-end VLM document parser (96.3% OmniDocBench v1.6). This is the
parallel "Stage-6-style" parse path that sits *alongside* the Docling
``parse_mixed`` slot, not replacing the PP-OCRv5 regional-crop worker
(``parse_scanned``) which remains the right tool for per-bbox OCR.

ADR-0016 names this capability ``PaddleOCRVLParser``. It is implemented
here as a module-level async function — ``parse_docparser_vl`` — to match
the §04p parser interface shared by ``parse_native`` / ``parse_scanned`` /
``parse_mixed`` / ``parse_table_heavy``. That keeps it a drop-in for the
``app.ocr._orchestrator`` dispatch and for ``_persist.py``: a class would be
inconsistent with every other parser in this package.

**Flag-gated + additive** (ADR-0016 Phase 2). Selection is driven by
``settings.PDF_DOCPARSER_BACKEND`` (``docling`` default | ``paddleocr-vl``).
Nothing in this module runs unless that flag is flipped; promotion into the
production dispatch is gated on the shadow-run eval (ADR-0016 Phase 2 step 4),
which is deliberately NOT wired here.

Output schema mirrors ``parse_mixed`` so the existing persistence writers need
no change::

    {
        "passages":  [ {page, region, bbox, source_method,
                        extraction_confidence, text_content, layout_label}, ... ],
        "tables":    [ {page, table_id, bbox, cells, structure_confidence,
                        cell_confidence, header_detected, parser_used}, ... ],
        "layouts":   [ {page, region, bbox, source_method,
                        extraction_confidence, layout_label, has_text}, ... ],
        "markdown":  list[str],        # additive — per-page layout-aware Markdown
        "parser_used": "paddleocr_vl",
        "page_count": int,
        "per_page_layout_confidence": list[float],
        "per_page_text_region_counts": list[int],
        "pages_needing_ocr": [],       # VL recognises text itself → never defers OCR
    }

PaddleOCR-VL API surface consumed (paddleocr>=3.7, ``[doc-parser]`` extra)::

    from paddleocr import PaddleOCRVL
    pipeline = PaddleOCRVL(device="gpu:0" | "cpu", pipeline_version="v1.6")
    for res in pipeline.predict(pdf_path):
        res.json      # {input_path, page_index, width, height,
                      #  model_settings, parsing_res_list: [
                      #     {block_bbox, block_label, block_content,
                      #      block_id, block_order}, ... ]}
        res.markdown  # {"markdown_texts": str, "markdown_images": ...}
"""
from __future__ import annotations

import asyncio
import logging
import threading
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger(__name__)


# PaddleOCR-VL block_label → silver.ingest_layouts.layout_label CHECK enum.
# Mirrors app.ocr._docling_common._DOCLING_LABEL_MAP — anything unmapped
# falls through to "other" so the silver CHECK constraint is never violated.
# PaddleOCR-VL's layout model emits up to 23 categories (doc/paragraph title,
# text, header/footer, figure/image, formula, table, captions, …); keys are
# matched after lower-casing and collapsing whitespace to "_".
_PADDLEOCR_VL_LABEL_MAP = {
    "text": "text",
    "plain_text": "text",
    "abstract": "text",
    "content": "text",
    "sidebar_text": "text",
    "doc_title": "title",
    "document_title": "title",
    "title": "title",
    "paragraph_title": "section_header",
    "section_header": "section_header",
    "header": "page_header",
    "header_image": "page_header",
    "footer": "page_footer",
    "footer_image": "page_footer",
    "footnote": "footnote",
    "footnotes": "footnote",
    "table": "table",
    "table_caption": "caption",
    "table_title": "caption",
    "figure": "figure",
    "image": "figure",
    "chart": "figure",
    "figure_caption": "caption",
    "figure_title": "caption",
    "caption": "caption",
    "formula": "formula",
    "formula_number": "formula",
    "algorithm": "code",
    "code": "code",
    "list": "list_item",
    "list_item": "list_item",
    "reference": "footnote",
    "references": "footnote",
}

# PaddleOCR-VL does not surface a per-block recognition score in
# parsing_res_list, so — like the Docling parsers — we assign constant
# confidences. VLM recognition is strong but not deterministic, so these sit
# just below the pdfplumber/native floors and above the OCR-retry thresholds.
VL_TEXT_CONFIDENCE = 0.92
VL_TABLE_STRUCTURE_CONFIDENCE = 0.90
VL_TABLE_CELL_CONFIDENCE = 0.90

# Treat tables as their own layout class for the passage/layout split.
_TABLE_LABEL = "table"


# ---------------------------------------------------------------------------
# Public entry point — §04p parser interface
# ---------------------------------------------------------------------------

async def parse_docparser_vl(
    pdf_path: Path,
    pages: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Parse a PDF with PaddleOCR-VL-1.6. Returns the parse_mixed schema.

    Args:
        pdf_path: Local filesystem path. Caller pre-validates via
            preflight + profile.
        pages: 0-indexed page numbers to keep in the output. ``None`` = all.
            Like parse_mixed, this filters the output, not the inference —
            PaddleOCR-VL parses the whole document in one pass.

    Returns:
        Parse result dict (see module docstring for schema).
    """
    return await asyncio.to_thread(_parse_docparser_vl_sync, pdf_path, pages)


def _parse_docparser_vl_sync(
    pdf_path: Path,
    pages: Sequence[int] | None,
) -> dict[str, Any]:
    """Synchronous implementation; called via asyncio.to_thread."""
    pipeline = _get_vl_pipeline()
    results = pipeline.predict(str(pdf_path))

    pages_filter: set[int] | None = set(pages) if pages is not None else None

    passages: list[dict[str, Any]] = []
    layouts: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    markdown_by_page: dict[int, str] = {}

    per_page_region_counter: dict[int, int] = {}
    per_page_table_counter: dict[int, int] = {}
    pages_seen: set[int] = set()

    for enum_idx, res in enumerate(results):
        data = _result_json(res)
        page = data.get("page_index")
        page = int(page) if page is not None else enum_idx
        if pages_filter is not None and page not in pages_filter:
            continue
        pages_seen.add(page)

        md = _result_markdown(res)
        if md:
            markdown_by_page[page] = md

        for block in (data.get("parsing_res_list") or []):
            raw_label = block.get("block_label")
            label = normalize_vl_label(raw_label)
            bbox = _coerce_bbox(block.get("block_bbox"))
            if bbox is None:
                continue
            content = (block.get("block_content") or "").strip()

            region_idx = per_page_region_counter.get(page, 0)
            per_page_region_counter[page] = region_idx + 1

            # Layout row — every detected region.
            layouts.append({
                "page": page,
                "region": region_idx,
                "bbox": bbox,
                "source_method": "paddleocr_vl_layout",
                "extraction_confidence": VL_TEXT_CONFIDENCE,
                "layout_label": label,
                "has_text": bool(content),
            })

            if label == _TABLE_LABEL:
                table_idx = per_page_table_counter.get(page, 0)
                per_page_table_counter[page] = table_idx + 1
                cells = _cells_from_table_content(content)
                tables.append({
                    "page": page,
                    "table_id": table_idx,
                    "bbox": bbox,
                    "cells": cells,
                    "structure_confidence": VL_TABLE_STRUCTURE_CONFIDENCE,
                    "cell_confidence": VL_TABLE_CELL_CONFIDENCE,
                    "header_detected": _heuristic_header_detected(cells),
                    "parser_used": "paddleocr_vl",
                })
                continue

            # Passage row — any non-table region that carries text.
            if content:
                passages.append({
                    "page": page,
                    "region": region_idx,
                    "bbox": bbox,
                    "source_method": "paddleocr_vl",
                    "extraction_confidence": VL_TEXT_CONFIDENCE,
                    "text_content": content,
                    "layout_label": label,
                })

    page_count = (
        len(pages_filter)
        if pages_filter is not None
        else (max(pages_seen) + 1 if pages_seen else 0)
    )

    per_page_layout_confidence = _per_page_mean(
        layouts, page_count, VL_TEXT_CONFIDENCE
    )
    per_page_text_region_counts = _per_page_count(passages, page_count)
    markdown = [markdown_by_page.get(p, "") for p in range(page_count)]

    return {
        "passages": passages,
        "tables": tables,
        "layouts": layouts,
        "markdown": markdown,
        "parser_used": "paddleocr_vl",
        "page_count": page_count,
        "per_page_layout_confidence": per_page_layout_confidence,
        "per_page_text_region_counts": per_page_text_region_counts,
        # PaddleOCR-VL recognises text end-to-end, so it never defers a page
        # to the external OCR worker the way Docling's do_ocr=False path does.
        "pages_needing_ocr": [],
    }


# ---------------------------------------------------------------------------
# Pipeline singleton (lazy — keeps PaddleOCR-VL weights out of import time)
# ---------------------------------------------------------------------------

_pipeline_lock = threading.Lock()
_pipeline_instance: Any = None


def _get_vl_pipeline() -> Any:
    """Return a process-cached PaddleOCRVL pipeline, building it on first use.

    Lazy + module-global so the ~3-4 GB BF16 weights are only resolved inside
    the Hatchet ingest worker that actually flips PDF_DOCPARSER_BACKEND, never
    at import time (the import-boundary rule already keeps app.ocr out of the
    user-facing FastAPI process). Tests monkeypatch this seam to inject a fake
    pipeline, so the real model never has to load under PHPUnit/pytest.
    """
    global _pipeline_instance
    if _pipeline_instance is not None:
        return _pipeline_instance
    with _pipeline_lock:
        if _pipeline_instance is None:
            _pipeline_instance = _build_vl_pipeline()
    return _pipeline_instance


def _build_vl_pipeline() -> Any:
    """Instantiate PaddleOCRVL with the resolved device. Imported lazily."""
    from paddleocr import PaddleOCRVL  # noqa: PLC0415

    from app.ocr._paddleocr_gpu import paddleocr_use_gpu  # noqa: PLC0415

    device = "gpu:0" if paddleocr_use_gpu() else "cpu"
    log.info("parse_docparser_vl: building PaddleOCRVL pipeline on device=%s", device)
    return PaddleOCRVL(device=device, pipeline_version="v1.6")


# ---------------------------------------------------------------------------
# Result-object accessors (tolerant of attribute- or dict-style results)
# ---------------------------------------------------------------------------

def _result_json(res: Any) -> dict[str, Any]:
    """Pull the structured dict off a PaddleOCR-VL result object."""
    data = getattr(res, "json", None)
    if callable(data):
        data = data()
    if isinstance(data, dict):
        # Some PaddleX result objects nest the payload under a "res" key.
        return data.get("res", data) if "res" in data else data
    if isinstance(res, dict):
        return res
    return {}


def _result_markdown(res: Any) -> str:
    """Pull the layout-aware Markdown text off a PaddleOCR-VL result object."""
    md = getattr(res, "markdown", None)
    if isinstance(md, dict):
        return (md.get("markdown_texts") or "").strip()
    if isinstance(md, str):
        return md.strip()
    return ""


# ---------------------------------------------------------------------------
# Label + bbox + table-cell helpers (module-level for direct unit testing)
# ---------------------------------------------------------------------------

def normalize_vl_label(label: str | None) -> str:
    """Map a PaddleOCR-VL block_label to the silver schema enum."""
    if not label:
        return "other"
    key = "_".join(str(label).lower().split())
    return _PADDLEOCR_VL_LABEL_MAP.get(key, "other")


def _coerce_bbox(raw: Any) -> list[float] | None:
    """Coerce a PaddleOCR-VL block_bbox into [x0, y0, x1, y1] (top-left origin).

    Accepts a flat 4-vector or an Nx2 polygon (numpy array or nested list);
    for a polygon the axis-aligned min/max envelope is returned. Coordinates
    are pixel-space (top-left origin) — the same convention as parse_scanned,
    which also works on rendered page images.
    """
    if raw is None:
        return None
    try:
        flat = list(raw.tolist()) if hasattr(raw, "tolist") else list(raw)
    except TypeError:
        return None
    if not flat:
        return None

    # Flat 4-vector: [x0, y0, x1, y1].
    if all(isinstance(v, (int, float)) for v in flat):
        if len(flat) < 4:
            return None
        xs = [float(flat[0]), float(flat[2])]
        ys = [float(flat[1]), float(flat[3])]
    else:
        # Polygon: list of [x, y] points → axis-aligned envelope.
        try:
            xs = [float(p[0]) for p in flat]
            ys = [float(p[1]) for p in flat]
        except (TypeError, IndexError, ValueError):
            return None
        if not xs or not ys:
            return None

    return [
        round(min(xs), 3),
        round(min(ys), 3),
        round(max(xs), 3),
        round(max(ys), 3),
    ]


class _TableCellParser(HTMLParser):
    """Minimal HTML <table> → row-major cell grid. Spans are not expanded;
    each <td>/<th> contributes one cell in document order."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str | None]] = []
        self._current: list[str | None] | None = None
        self._cell_chunks: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "tr":
            self._current = []
        elif tag in ("td", "th"):
            self._cell_chunks = []

    def handle_data(self, data: str) -> None:
        if self._cell_chunks is not None:
            self._cell_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._current is not None:
            text = "".join(self._cell_chunks or []).strip()
            self._current.append(text or None)
            self._cell_chunks = None
        elif tag == "tr" and self._current is not None:
            self.rows.append(self._current)
            self._current = None


def _cells_from_table_content(content: str) -> list[list[str | None]]:
    """Best-effort cell grid from a PaddleOCR-VL table block_content string.

    PaddleOCR-VL emits table content as HTML (``<table>…</table>``); some
    builds emit a Markdown pipe-table. Parse whichever is present so the
    shadow-run "table row count" comparison (ADR-0016 Phase 2 step 4) has a
    grid to count. Returns ``[]`` when nothing parseable is found — the
    downstream schema treats an empty grid as "no structured cells".
    """
    if not content:
        return []
    if "<table" in content.lower():
        parser = _TableCellParser()
        try:
            parser.feed(content)
        except Exception:  # noqa: BLE001 — malformed HTML → no grid, not a crash
            return []
        return [r for r in parser.rows if r]
    if "|" in content:
        return _cells_from_markdown_table(content)
    return []


def _cells_from_markdown_table(content: str) -> list[list[str | None]]:
    """Parse a Markdown pipe-table into a cell grid, dropping the `---` rule."""
    rows: list[list[str | None]] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or "|" not in stripped:
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        # Separator row (---|:--:|---) carries no data.
        if cells and all(set(c) <= {"-", ":", " "} and c for c in cells):
            continue
        rows.append([c or None for c in cells])
    return rows


def _heuristic_header_detected(cells: list[list[str | None]]) -> bool:
    """Same first-row heuristic as parse_mixed / parse_table_heavy."""
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


def _per_page_mean(
    items: list[dict[str, Any]], page_count: int, default: float
) -> list[float]:
    """Mean of `extraction_confidence` per page; `default` for empty pages."""
    if page_count == 0:
        return []
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


def _per_page_count(items: list[dict[str, Any]], page_count: int) -> list[int]:
    """Count of items per page."""
    if page_count == 0:
        return []
    counts = [0] * page_count
    for item in items:
        page = item.get("page", 0)
        if 0 <= page < page_count:
            counts[page] += 1
    return counts
