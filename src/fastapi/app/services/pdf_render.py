"""Stage 2 — PDF page rendering via pypdfium2.

§04p pipeline Stage 2 responsibilities:
  - Render full pages at configurable DPI (200–300 for VL input, 72–150 for
    thumbnails).
  - Crop rendered pages to a bbox given in PDF user-space coordinates.
  - Content-addressable LRU cache keyed on (pdf_id, page, bbox_tuple | None, dpi)
    so repeated agent calls for the same region are served from memory.

Threading model — PROCESS workers, not threads
-----------------------------------------------
PDFium (the C++ library underneath pypdfium2) maintains per-document state in
global structures that are not fully thread-safe.  §04p specifies process workers
explicitly.  We use concurrent.futures.ProcessPoolExecutor so each worker is a
separate OS process with its own memory space.  This eliminates the GIL and the
PDFium thread-safety concern simultaneously.

Pickling note
-------------
Functions submitted to ProcessPoolExecutor must be picklable.  Methods on class
instances are NOT reliably picklable in all Python versions.  All worker
functions in this module are defined at MODULE LEVEL (not inside the class) so
the multiprocessing start method can locate them by qualified name.  Only plain
bytes are passed as arguments (also picklable), so the serialization boundary
is safe.

Lifespan integration
--------------------
PdfRenderService is a singleton held on app.state.pdf_render_service.
Initialise it in the FastAPI lifespan startup hook:

    app.state.pdf_render_service = PdfRenderService()

Shut it down in the teardown hook:

    await app.state.pdf_render_service.shutdown()

Extension points for Phase 1.B
-------------------------------
Phase 1.B (text+layout via pdfminer.six / pdfplumber) will open PDF bytes
independently.  The render service has no coupling to text extraction — the
two stages share only the Bronze-stored bytes, not in-memory state.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CACHE_SIZE = 256  # LRU entries; overridden by env PDF_RENDER_CACHE_SIZE

# PDF points per inch — PDF user-space uses 72 points/inch.
_PDF_POINTS_PER_INCH = 72.0


# ---------------------------------------------------------------------------
# Module-level worker functions (must be picklable -> top-level definitions)
# ---------------------------------------------------------------------------
# These functions accept only plain bytes + primitives (all picklable).
# pypdfium2 document objects are opened INSIDE the worker so no unpicklable
# state crosses the process boundary.


def _render_full_page_worker(pdf_bytes: bytes, page_index: int, dpi: int) -> bytes:
    """Render a single page to PNG bytes.

    Runs inside a ProcessPoolExecutor worker process.  Must be a top-level
    function (picklable by name) — not a method or nested closure.

    Parameters
    ----------
    pdf_bytes:
        Raw bytes of the normalised PDF (from Bronze store).
    page_index:
        0-indexed page number (pypdfium2 uses 0-based).
    dpi:
        Render resolution in dots per inch.

    Returns
    -------
    PNG image bytes.
    """
    try:
        import pypdfium2 as pdfium  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "pypdfium2 is not installed. Run: uv pip install 'pypdfium2>=4.30'"
        ) from exc

    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        page = pdf[page_index]
        scale = dpi / _PDF_POINTS_PER_INCH
        bitmap = page.render(scale=scale, rotation=0)
        pil_image = bitmap.to_pil()
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        pdf.close()


def _render_crop_worker(
    pdf_bytes: bytes,
    page_index: int,
    bbox: tuple[float, float, float, float],
    dpi: int,
) -> bytes:
    """Render a cropped region of a page to PNG bytes.

    bbox is in PDF user-space coordinates (origin = bottom-left, y-up).
    pypdfium2 renders with a top-left origin, so we flip the y-axis using
    the page height before cropping.

    Runs inside a ProcessPoolExecutor worker process.
    """
    try:
        import pypdfium2 as pdfium  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "pypdfium2 is not installed. Run: uv pip install 'pypdfium2>=4.30'"
        ) from exc

    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        page = pdf[page_index]
        scale = dpi / _PDF_POINTS_PER_INCH

        # PDF user-space: origin = bottom-left, y-up.
        # pypdfium2 rendered bitmap: origin = top-left, y-down.
        page_height_pts = page.get_height()

        x0_pts, y0_pts, x1_pts, y1_pts = bbox  # y0 < y1 in PDF space

        # Render the full page first, then crop.
        bitmap = page.render(scale=scale, rotation=0)
        pil_image = bitmap.to_pil()

        # Convert PDF user-space coords to pixel coords (y-flip).
        px_left = int(x0_pts * scale)
        px_right = int(x1_pts * scale)
        px_top = int((page_height_pts - y1_pts) * scale)    # y1 = upper PDF edge
        px_bottom = int((page_height_pts - y0_pts) * scale)  # y0 = lower PDF edge

        # Clamp to image dimensions to guard against out-of-bounds bboxes.
        img_w, img_h = pil_image.size
        px_left = max(0, min(px_left, img_w - 1))
        px_right = max(px_left + 1, min(px_right, img_w))
        px_top = max(0, min(px_top, img_h - 1))
        px_bottom = max(px_top + 1, min(px_bottom, img_h))

        cropped = pil_image.crop((px_left, px_top, px_right, px_bottom))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        pdf.close()


# ---------------------------------------------------------------------------
# Simple OrderedDict-based LRU cache (no external dep required)
# ---------------------------------------------------------------------------

if TYPE_CHECKING:
    pass  # keep type checker happy without importing cachetools at check time


class _LRUCache:
    """Minimal LRU cache backed by an OrderedDict.

    Used when cachetools is not installed.  asyncio is single-threaded (one
    coroutine runs at a time), so no lock is needed.
    """

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._store: OrderedDict[int, bytes] = OrderedDict()

    def get(self, key: int) -> bytes | None:
        if key not in self._store:
            return None
        self._store.move_to_end(key)
        return self._store[key]

    def set(self, key: int, value: bytes) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        else:
            if len(self._store) >= self._maxsize:
                self._store.popitem(last=False)
            self._store[key] = value

    def __len__(self) -> int:
        return len(self._store)


def _make_cache(maxsize: int) -> _LRUCache:
    """Build an LRU cache, preferring cachetools if available."""
    try:
        from cachetools import LRUCache as _CachetoolsLRU  # noqa: PLC0415

        ct_cache = _CachetoolsLRU(maxsize=maxsize)

        # Wrap cachetools.LRUCache to expose the same get/set interface.
        class _CachetoolsAdapter(_LRUCache):
            def __init__(self) -> None:  # type: ignore[override]
                # Do not call super().__init__; we delegate to _ct.
                self._ct = ct_cache

            def get(self, key: int) -> bytes | None:  # type: ignore[override]
                return self._ct.get(key)

            def set(self, key: int, value: bytes) -> None:  # type: ignore[override]
                self._ct[key] = value

            def __len__(self) -> int:
                return len(self._ct)

        return _CachetoolsAdapter()
    except ImportError:
        logger.debug("cachetools not installed; using built-in OrderedDict LRU cache")
        return _LRUCache(maxsize=maxsize)


def _cache_key(pdf_id: str, page: int, bbox: tuple[float, ...] | None, dpi: int) -> int:
    """Stable integer cache key from (pdf_id, page, bbox, dpi).

    Uses MD5 for speed (not cryptographic — cache key only).  The key is
    deterministic within a process.  Different pdf_id values are guaranteed
    to produce different keys (pdf_id is the SHA-256 of the PDF bytes).
    """
    raw = f"{pdf_id}:{page}:{bbox!r}:{dpi}"
    digest = hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()  # noqa: S324
    return int(digest, 16)


# ---------------------------------------------------------------------------
# PdfRenderService singleton
# ---------------------------------------------------------------------------


class PdfRenderService:
    """Stage 2 render service — singleton held on app.state.pdf_render_service.

    Holds:
      - A ProcessPoolExecutor for pypdfium2 calls (process workers, not threads,
        per §04p PDFium thread-safety requirement).
      - An LRU cache (256 entries by default) keyed on
        (pdf_id, page, bbox_tuple | None, dpi).

    Usage in the FastAPI lifespan::

        app.state.pdf_render_service = PdfRenderService()
        yield
        await app.state.pdf_render_service.shutdown()

    Then in route handlers::

        service: PdfRenderService = request.app.state.pdf_render_service
        png_bytes = await service.render_page(pdf_bytes, pdf_id, page=1, dpi=200)
    """

    def __init__(self) -> None:
        cache_size = int(os.environ.get("PDF_RENDER_CACHE_SIZE", _DEFAULT_CACHE_SIZE))
        self._cache = _make_cache(cache_size)
        self._executor = ProcessPoolExecutor(max_workers=os.cpu_count() or 4)
        logger.info(
            "PdfRenderService ready: process_pool_workers=%d cache_size=%d",
            os.cpu_count() or 4,
            cache_size,
        )

    async def render_page(
        self,
        pdf_bytes: bytes,
        pdf_id: str,
        page: int,
        dpi: int,
    ) -> bytes:
        """Render a full page to PNG bytes.

        Parameters
        ----------
        pdf_bytes:
            Raw bytes of the normalised PDF (fetched from the Bronze store).
        pdf_id:
            SHA-256 hex of the PDF (cache key discriminator — prevents
            cross-PDF collisions when two PDFs share page number + DPI).
        page:
            1-indexed page number.
        dpi:
            Render resolution (72–300).

        Returns
        -------
        PNG image bytes (PNG magic bytes: \\x89PNG).
        """
        key = _cache_key(pdf_id, page, None, dpi)
        cached = self._cache.get(key)
        if cached is not None:
            logger.debug("render_page cache HIT pdf_id=%s page=%d dpi=%d", pdf_id[:8], page, dpi)
            return cached

        logger.debug("render_page cache MISS pdf_id=%s page=%d dpi=%d", pdf_id[:8], page, dpi)
        loop = asyncio.get_running_loop()
        png_bytes: bytes = await loop.run_in_executor(
            self._executor,
            _render_full_page_worker,
            pdf_bytes,
            page - 1,  # convert 1-indexed -> 0-indexed for pypdfium2
            dpi,
        )
        self._cache.set(key, png_bytes)
        return png_bytes

    async def crop_region(
        self,
        pdf_bytes: bytes,
        pdf_id: str,
        page: int,
        bbox: tuple[float, float, float, float],
        dpi: int,
    ) -> bytes:
        """Render and crop a region to PNG bytes.

        Parameters
        ----------
        bbox:
            (x0, y0, x1, y1) in PDF user-space points.  Origin = bottom-left,
            y increases upward.  The worker converts to pixel coordinates
            using the DPI scaling factor (dpi / 72.0).

        Returns
        -------
        Cropped PNG image bytes.
        """
        key = _cache_key(pdf_id, page, bbox, dpi)
        cached = self._cache.get(key)
        if cached is not None:
            logger.debug(
                "crop_region cache HIT pdf_id=%s page=%d bbox=%s dpi=%d",
                pdf_id[:8], page, bbox, dpi,
            )
            return cached

        logger.debug(
            "crop_region cache MISS pdf_id=%s page=%d bbox=%s dpi=%d",
            pdf_id[:8], page, bbox, dpi,
        )
        loop = asyncio.get_running_loop()
        png_bytes: bytes = await loop.run_in_executor(
            self._executor,
            _render_crop_worker,
            pdf_bytes,
            page - 1,  # 1-indexed -> 0-indexed
            bbox,
            dpi,
        )
        self._cache.set(key, png_bytes)
        return png_bytes

    async def shutdown(self) -> None:
        """Shut down the process pool gracefully.

        In-flight renders complete before the pool shuts down.  Call this in
        the FastAPI lifespan teardown hook before the app exits.
        """
        self._executor.shutdown(wait=True)
        logger.info("PdfRenderService process pool shut down")
