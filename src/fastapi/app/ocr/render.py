"""§04p page rendering — pypdfium2 PDF-page-to-image.

**Master-plan §9.3 reference.** Renders a single PDF page to a PNG
byte string. Used by:
- parse_scanned (to feed PaddleOCR an image array; avoids fitz dep)
- Silver Review UI thumbnails (Step 8)

**Status:** Step 4 implementation (doc-phase 52).
"""
from __future__ import annotations

import asyncio
import contextlib
import io
from pathlib import Path


async def render_page(pdf_path: Path, page: int, scale: float = 2.0) -> bytes:
    """Render a single PDF page to a PNG byte string.

    Args:
        pdf_path: Local filesystem path. Caller pre-validates via preflight.
        page: 0-indexed page number.
        scale: pypdfium2 render scale. 2.0 = ~144 DPI (good default for
            OCR + review thumbnails). Step 4's parse_scanned uses this
            for OCR input; Step 8's UI may use a smaller value for
            thumbnails.

    Returns:
        PNG-encoded page image as bytes.

    Raises:
        FileNotFoundError: if pdf_path does not exist.
        IndexError: if `page` is out of range for the document.
        ValueError: if `scale` is non-positive.
    """
    if scale <= 0:
        raise ValueError(f"scale must be positive, got {scale}")
    return await asyncio.to_thread(_render_page_sync, pdf_path, page, scale)


def _render_page_sync(pdf_path: Path, page: int, scale: float) -> bytes:
    """Synchronous implementation; called via asyncio.to_thread."""
    import pypdfium2 as pdfium

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        if not (0 <= page < len(pdf)):
            raise IndexError(
                f"page {page} out of range for {pdf_path.name} ({len(pdf)} pages)"
            )

        bitmap = pdf[page].render(scale=scale)
        pil = bitmap.to_pil().convert("RGB")
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        # pypdfium2's PdfDocument holds a C resource handle; close
        # explicitly so the test harness doesn't accumulate file
        # descriptors across runs.
        with contextlib.suppress(Exception):
            pdf.close()
